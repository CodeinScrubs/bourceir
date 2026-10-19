"""داده ساختگی فقط در آزمون و آرشیو موقت؛ تطبیق رقم مجوز ثبت وجه نیست."""

import copy
import hashlib
import html
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from config import settings
from core import dividend_reconciliation as core
from core.dividend_evidence import parse_dividend_source
from ops import dividend_reconciliation as ops
from ops.codal_documents import capture_codal_document
from ops.dividend_evidence import capture_dividend_evidence
from tests.test_codal_documents import Response, provider
from tests.test_dividend_evidence import Source, row


def notice():
    return dict(tracing_no=1, symbol_fa='آزمون', company_name='آزمون',
        title='تصمیمات مجمع عمومی عادی سالیانه دوره ۱۲ ماهه منتهی به ۱۴۰۱/۱۲/۲۹',
        published_at='2023-07-21T10:00:00+03:30',
        url='https://codal.ir/Reports/Decision.aspx?LetterSerial=test')


def text(amount='۱۲۰'):
    return '\n'.join(['موضوع: تصمیمات مجمع عمومی عادی سالیانه',
        'صاحبان سهام برای سال (دوره) مالی منتهی به', '1401/12/29', 'شرکت آزمون - نماد: آزمون',
        'مجمع عمومی عادی سالیانه مورخ 1402/04/29', 'جدول کل شرکت (میلیون ریال)',
        'EarningsPerShareAfterTax', 'سود خالص هر سهم- ریال', '۹۹۹',
        'DividendPerShare', 'سود نقدی هر سهم (ریال)', amount])


def fact(body=None, meta=None):
    return core.primary_annual_dividend(text() if body is None else body, notice() if meta is None else meta,
                                        end_exclusive='2025-03-21')


def test_only_explicit_per_share_rial_is_used_not_eps_or_the_million_rial_table():
    value = fact()
    assert value['status'] == 'supported_primary_fact' and Decimal(value['dps_rial']) == 120
    assert value['meeting_date'] == '2023-07-20' and value['fiscal_year_end'] == '2023-03-20'


@pytest.mark.parametrize('mutation', ['duplicate', 'million', 'thousand', 'toman', 'no_serial', 'eps_only',
    'issuer', 'symbol', 'period', 'meeting', 'extraordinary', 'negative', 'blank', 'impossible_date', 'naive_time'])
def test_unsupported_or_ambiguous_primary_fact_fails_closed(mutation):
    body, meta = text(), notice()
    if mutation == 'duplicate':
        body += '\nDividendPerShare\nسود نقدی هر سهم (ریال)\n120'
    elif mutation in ('million', 'thousand', 'toman'):
        body = body.replace('سود نقدی هر سهم (ریال)', 'سود نقدی هر سهم (' +
            {'million': 'میلیون ریال', 'thousand': 'هزار ریال', 'toman': 'تومان'}[mutation] + ')')
    elif mutation == 'no_serial':
        body = body.replace('DividendPerShare', 'OtherAmount')
    elif mutation == 'eps_only':
        body = body.split('DividendPerShare')[0]
    elif mutation == 'issuer':
        meta['company_name'] = 'شرکت دیگر'
    elif mutation == 'symbol':
        meta['symbol_fa'] = 'دیگر'
    elif mutation == 'period':
        meta['title'] = meta['title'].replace('۱۴۰۱', '۱۴۰۰')
    elif mutation == 'meeting':
        body += '\nجلسه دیگری مورخ 1402/05/01'
    elif mutation == 'extraordinary':
        body = body.replace('موضوع: تصمیمات مجمع عمومی عادی سالیانه', 'موضوع: تصمیمات مجمع عمومی فوق العاده')
    elif mutation == 'negative':
        body = text('-120')
    elif mutation == 'blank':
        body = text('')
    elif mutation == 'impossible_date':
        body = body.replace('1402/04/29', '1402/13/29')
    else:
        meta['published_at'] = '2023-07-21T10:00:00'
    result = fact(body, meta)
    assert result['status'] == 'unsupported_primary_text' and result['dps_rial'] is None


def test_sealed_dates_are_checked_before_reading_the_amount(monkeypatch):
    body = text().replace('1401', '1404').replace('1402', '1405')
    meta = notice()
    meta.update(title=meta['title'].replace('۱۴۰۱', '۱۴۰۴'), published_at='2026-07-22T10:00:00+03:30')
    monkeypatch.setattr(core, '_amount', lambda _: pytest.fail('sealed amount must not be read'))
    result = fact(body, meta)
    assert result['status'] == 'withheld_by_cutoff' and result['dps_rial'] is None


@pytest.mark.parametrize('amount', ['0', '۰٫۰۰', '1,234.50'])
def test_zero_and_exact_decimal_amounts_are_not_unknown_or_rounded(amount):
    assert Decimal(fact(text(amount))['dps_rial']) == Decimal(amount.replace(',', '').replace('۰٫۰۰', '0.00'))


def test_conflicting_and_unknown_revisions_remain_in_the_matching_denominator():
    rows = parse_dividend_source((row('120') + row('100') + row('')).encode(), end_exclusive='2025-03-21')['rows']
    result = core.reconcile_dividend(fact(), rows)
    assert result['matching_row_count'] == 3 and result['unknown_amount_rows'] == 1
    assert result['status'] == 'source_amount_conflict' and not result['cross_source_value_corroborated']
    assert result['matching_rows'][-1]['agrees'] is None


@pytest.mark.parametrize('amount,status,corroborated', [('120.00', 'corroborated_values', True),
                                                      ('', 'source_amount_incomplete', False)])
def test_same_value_duplicates_are_not_summed_and_unknowns_are_not_dropped(amount, status, corroborated):
    rows = parse_dividend_source((row('120') + row(amount)).encode(), end_exclusive='2025-03-21')['rows']
    result = core.reconcile_dividend(fact(), rows)
    assert result['matching_row_count'] == 2 and result['status'] == status
    assert result['cross_source_value_corroborated'] is corroborated
    for flag in ('economic_entitlements_verified', 'settlement_verified', 'feature_eligible',
                 'accepted_for_signals', 'historical_identity_verified', 'source_completeness_verified'):
        assert result[flag] is False


@pytest.mark.parametrize('close,synthetic,economic', [(99, 110, 109), (81, 90, 91), (90, 100, 100)])
def test_cash_claim_face_value_does_not_follow_the_post_event_stock_price(close, synthetic, economic):
    result = core.compare_cash_dividend_marks(prior_close_rial=100, ex_reference_rial=90, ex_close_rial=close, dps_rial=10)
    assert Decimal(result['synthetic_gross_mark_rial']) == synthetic
    assert Decimal(result['shares_plus_receivable_gross_mark_rial']) == economic
    assert result['cash_only_model_quantity_per_original_share'] == '1'
    assert 'actual_share_quantity_per_original_share' not in result
    assert result['spendable_dividend_cash_rial'] is None and result['strategy_return'] is None
    assert not result['settlement_verified'] and not result['fees_included']


def test_unexplained_reference_gap_does_not_create_a_cash_only_value():
    result = core.compare_cash_dividend_marks(prior_close_rial=100, ex_reference_rial=85, ex_close_rial=99, dps_rial=10)
    assert result['reference_gap_matches_dps'] is False and result['synthetic_gross_mark_rial'] is None
    assert result['shares_plus_receivable_gross_mark_rial'] is None
    assert result['cash_only_model_quantity_per_original_share'] is None


@pytest.mark.parametrize('bad', [True, float('nan'), float('inf'), -1, 0, 'unknown', .5, 2**63])
def test_invalid_prices_never_become_a_known_mark(bad):
    with pytest.raises(ValueError):
        core.compare_cash_dividend_marks(prior_close_rial=100, ex_reference_rial=bad, ex_close_rial=99, dps_rial=10)


@pytest.fixture
def archives(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path / 'data')
    monkeypatch.setattr(settings, 'DB_PATH', tmp_path / 'must-not-exist.db')
    meta = notice()
    body = '<html><body>' + ''.join('<p>' + html.escape(line) + '</p>' for line in text('100').splitlines()) + '</body></html>'
    api, _ = provider(Response(body.encode('utf-8')))
    doc = capture_codal_document(meta['url'], provider=api)
    scope = [dict(ins_code='123', symbol_fa='آزمون', instrument_type_code=300)]
    native = capture_dividend_evidence(Source(), scope)
    notice_path = settings.DATA_DIR / 'private' / 'notice.json'
    notice_path.write_text(json.dumps(meta), encoding='utf-8')
    return dict(notice_path=notice_path, document=doc['folder'], study=native['folder'])


def test_real_archive_contracts_recompute_without_opening_a_database_or_network(archives, monkeypatch, capsys):
    monkeypatch.setattr('requests.sessions.Session.request', lambda *a, **k: pytest.fail('offline reconciliation'))
    result = ops.compare_archived_dividend(**archives)
    assert result['facts']['cross_source_value_corroborated'] and not settings.DB_PATH.exists()
    assert ops.load_dividend_comparison(result['folder']) == {k: v for k, v in result.items() if k != 'folder'}
    assert ops.main(['review', '--study', result['folder']]) == 0
    assert not json.loads(capsys.readouterr().out)['facts']['settlement_verified']


def test_rehashing_a_forged_financial_promotion_does_not_pass_review(archives):
    result = ops.compare_archived_dividend(**archives)
    folder = Path(result['folder'])
    payload = json.loads((folder / 'result.json').read_bytes())
    payload['facts']['settlement_verified'] = True
    (folder / 'result.json').write_text(json.dumps(payload), encoding='utf-8')
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    manifest['files']['result.json'] = hashlib.sha256((folder / 'result.json').read_bytes()).hexdigest()
    (folder / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError, match='بازحساب'):
        ops.load_dividend_comparison(folder)


def test_metadata_for_a_different_official_url_cannot_label_the_captured_document(archives):
    meta = copy.deepcopy(notice())
    meta['url'] += '-other'
    archives['notice_path'].write_text(json.dumps(meta), encoding='utf-8')
    with pytest.raises(ValueError, match='همان پیوند'):
        ops.compare_archived_dividend(**archives)


def test_redirected_cli_output_is_utf8_even_under_an_ascii_locale(archives):
    result = ops.compare_archived_dividend(**archives)
    env = dict(os.environ, PYTHONIOENCODING='ascii', PYTHONUTF8='0',
               BRS_DATA_DIR=str(settings.DATA_DIR), BRS_DB_PATH=str(settings.DB_PATH))
    process = subprocess.run([sys.executable, '-m', 'ops.dividend_reconciliation', 'review', '--study', result['folder']],
        cwd=settings.BASE_DIR, env=env, capture_output=True, timeout=30)
    assert process.returncode == 0, process.stderr.decode('utf-8', errors='replace')
    payload = json.loads(process.stdout.decode('utf-8'))
    assert payload['facts']['primary']['symbol'] == 'آزمون' and not settings.DB_PATH.exists()
