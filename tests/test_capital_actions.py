"""متن مصنوعی فقط در آزمون و آرشیو موقت؛ حق سهامدار از مبلغ ثبت ساخته نمی‌شود."""

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from config import settings
from core.capital_actions import primary_capital_registration
from core.text import to_fa_digits
from ops.capital_actions import create_capital_study, review_capital_study
from ops.codal_documents import capture_codal_document

TEXT = '''مشخصات
آگهی ثبت افزایش سرمایه
شرکت صنعتی آزمایش - نماد: آزمون
موضوع: آگهی ثبت افزایش سرمایه
با عنایت به مجوز افزایش سرمایه به شماره مجوز DPM-IOP-02A-123 مورخ 1402/11/17
بر اساس تصمیمات مجمع عمومی فوق العاده مورخ
1402/12/02
افزایش سرمایۀ شرکت از مبلغ
1,000
میلیون ریال به‌مبلغ
1,350
میلیون ریال (
از محل سود انباشته مبلغ
350
میلیون ریال ،
میلیون ریال و از آورده نقدی با سلب حق تقدم از سهامداران فعلی مبلغ
میلیون ریال )
در در تاریخ
1402/12/23
در مرجع ثبت شرکت ها به ثبت رسیده است.
اطلاعات نمایش داده شده با اطلاعات امضا شده مطابقت دارد
'''
URL = 'https://codal.ir/Reports/Decision.aspx?LetterSerial=TEST&let=28'
CUTOFF = '2025-03-21'


def notice(**changes):
    result = dict(tracing_no=123, symbol_fa='آزمون', company_name='صنعتی آزمایش',
                  title='آگهی ثبت افزایش سرمایه', published_at='2024-03-13T12:30:00+03:30', url=URL)
    result.update(changes)
    return result


def facts(text=TEXT, metadata=None):
    return primary_capital_registration(text, notice() if metadata is None else metadata, end_exclusive=CUTOFF)


def test_declared_million_rial_is_exact_but_capital_ratio_does_not_prove_share_credit_or_cash_zero():
    result = facts()
    assert result['status'] == 'supported_primary_fact'
    assert (result['capital_before_rial'], result['capital_after_rial'], result['increase_rial']) == (
        1_000_000_000, 1_350_000_000, 350_000_000)
    assert result['retained_earnings_rial'] == 350_000_000
    assert result['waived_preemption_cash_rial'] is None
    assert result['unassigned_after_known_funding_rial'] == 0
    assert result['retained_equals_increase'] is True
    assert result['capital_multiplier'] == dict(numerator=27, denominator=20)
    assert result['increase_ratio'] == dict(numerator=7, denominator=20)
    assert result['license_date'] == '2024-02-06' and result['meeting_date'] == '2024-02-21'
    assert result['registration_date'] == '2024-03-13'
    flags = ('external_authenticity_verified', 'historical_identity_verified', 'original_publication_time_verified',
             'funding_completeness_verified', 'shareholder_entitlement_verified', 'shares_credited_verified',
             'tradability_verified', 'feature_eligible', 'accepted_for_signals', 'profitability_proven')
    assert all(result[key] is False for key in flags)
    assert not any(key in result for key in ('shares', 'share_quantity', 'credit_date', 'trade_date'))


def test_arabic_letters_persian_digits_and_group_separators_preserve_facts():
    converted = to_fa_digits(TEXT).replace(',', '٬').replace('صنعتی', 'صنعتي')
    assert facts(converted) == facts()


@pytest.mark.parametrize('kind', ['zero', 'missing', 'mixed'])
def test_explicit_zero_unknown_and_known_cash_funding_are_different(kind):
    text = TEXT
    if kind == 'zero':
        text = text.replace('فعلی مبلغ\nمیلیون', 'فعلی مبلغ\n0\nمیلیون')
    elif kind == 'missing':
        text = text.replace('\n350\nمیلیون', '\nمیلیون')
    else:
        text = text.replace('\n350\nمیلیون', '\n250\nمیلیون').replace('فعلی مبلغ\nمیلیون', 'فعلی مبلغ\n100\nمیلیون')
    result = facts(text)
    assert result['status'] == 'supported_primary_fact'
    assert result['waived_preemption_cash_rial'] == (0 if kind == 'zero' else None if kind == 'missing' else 100_000_000)
    assert result['retained_equals_increase'] == (True if kind == 'zero' else None if kind == 'missing' else False)
    assert result['unassigned_after_known_funding_rial'] == (None if kind == 'missing' else 0)
    assert result['funding_completeness_verified'] is False


@pytest.mark.parametrize('old,new', [('1,000', '0'), ('1,000', '1,000'), ('0', '1,350'),
                                  ('1,000', '999'), ('1,000', '1,35'), ('1,000', '1e10'),
                                  ('1,000', 'NaN'), ('1,000', '10000000000000000000000000000000000')])
def test_invalid_or_nonincreasing_capital_cannot_create_a_ratio(old, new):
    result = facts(TEXT.replace('1,000\n', old + '\n').replace('1,350\n', new + '\n'))
    assert result['status'] == 'unsupported_primary_text'
    assert result['capital_before_rial'] is None and result['capital_multiplier'] is None


@pytest.mark.parametrize('change', [
    ('میلیون ریال', 'تومان'), ('میلیون ریال', 'هزار ریال'),
    ('صنعتی آزمایش - نماد: آزمون', 'صنعتی دیگری - نماد: آزمون'),
    ('صنعتی آزمایش - نماد: آزمون', 'صنعتی آزمایش - نماد: دیگر'),
    ('موضوع: آگهی ثبت افزایش سرمایه', 'موضوع: پیشنهاد افزایش سرمایه'),
    ('1402/11/17', '1402/12/24'), ('1402/12/02', '1402/12/24'),
    ('1402/12/23', '1402/13/23'), ('\n350\nمیلیون', '\n351\nمیلیون'),
])
def test_wrong_units_names_dates_or_excess_funding_leave_financial_fields_unknown(change):
    result = facts(TEXT.replace(*change))
    assert result['status'] == 'unsupported_primary_text'
    assert result['increase_rial'] is None and result['retained_earnings_rial'] is None


@pytest.mark.parametrize('change', [
    ('1402/11/17', '1404/01/01'), ('1402/12/02', '1404/01/01'), ('1402/12/23', '1404/01/01'),
])
def test_future_stated_dates_are_withheld_before_financial_interpretation(monkeypatch, change):
    monkeypatch.setattr('core.capital_actions._rial', lambda *_: pytest.fail('withheld amount interpreted'))
    result = facts(TEXT.replace(*change).replace('1,350', 'not-a-number'))
    assert result['status'] == 'withheld_by_cutoff' and result['capital_after_rial'] is None


def test_future_publication_and_timezone_boundary_are_withheld_before_amounts(monkeypatch):
    monkeypatch.setattr('core.capital_actions._rial', lambda *_: pytest.fail('withheld amount interpreted'))
    metadata = notice()
    metadata['published_at'] = '2025-03-20T21:00:00+00:00'
    assert facts(metadata=metadata)['status'] == 'withheld_by_cutoff'


@pytest.mark.parametrize('metadata', [dict(tracing_no=True), dict(tracing_no=0), dict(title='پیشنهاد هیئت مدیره'),
                                    dict(published_at='2024-03-13T12:30:00'), dict(published_at='2024-03-12T12:30:00'),
                                    dict(symbol_fa=''), dict(company_name=None)])
def test_invalid_notice_cannot_supply_registration_facts(metadata):
    provided = notice()
    provided.update(metadata)
    assert facts(metadata=provided)['status'] == 'unsupported_primary_text'


def test_duplicate_amount_or_funding_is_not_resolved_by_choosing_the_agreeing_text():
    assert facts(TEXT + TEXT)['status'] == 'unsupported_primary_text'
    result = facts(TEXT + '\nاز محل سود انباشته مبلغ 350 میلیون ریال')
    assert result['status'] == 'unsupported_primary_text' and result['increase_rial'] is None


def test_sub_rial_amount_is_not_rounded_into_a_valid_capital_event():
    text = TEXT.replace('1,000', '0.0000001').replace('1,350', '0.0000002').replace('350\n', '0.0000001\n')
    assert facts(text)['status'] == 'unsupported_primary_text'


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)

    class Captured:
        def get_document(self, url):
            assert url == URL
            return dict(status='captured', source_url=url, effective_url=url,
                requested_at='2024-03-14T10:00:00+03:30', observed_at='2024-03-14T10:00:01+03:30',
                response_sha256='1'*64, response_bytes=1000, format='text', content=TEXT.encode(), links=[])

    document = capture_codal_document(URL, provider=Captured())
    metadata = tmp_path / 'private' / 'notice.json'
    metadata.write_text(json.dumps(dict(notice(), extra_public_field='not copied'), ensure_ascii=False), encoding='utf-8')
    return dict(notice_path=metadata, document=document['folder'], root=tmp_path/'private'/'capital-actions')


def test_study_is_readable_without_original_source_or_database_or_network(source, monkeypatch):
    monkeypatch.setattr(socket, 'create_connection', lambda *_a, **_k: pytest.fail('network called'))
    monkeypatch.setattr('database.db.DatabaseManager.__init__', lambda *_a, **_k: pytest.fail('database opened'))
    result = create_capital_study(**source)
    folder = Path(result['folder'])
    assert result['facts'] == facts()
    assert 'extra_public_field' not in json.loads((folder/'notice.json').read_bytes())
    assert result['available_at'] >= json.loads((Path(source['document'])/'manifest.json').read_bytes())['available_at']
    assert Path(source['document']).resolve().is_relative_to(Path(source['root']).parent.resolve())
    shutil.rmtree(source['document'])
    Path(source['notice_path']).unlink()
    assert review_capital_study(folder)['facts'] == facts()


def rehash(folder, name):
    data = json.loads((folder/'manifest.json').read_bytes())
    data[name+'_sha256'] = hashlib.sha256((folder/(name+'.json')).read_bytes()).hexdigest()
    (folder/'manifest.json').write_text(json.dumps(data), encoding='utf-8')


@pytest.mark.parametrize('kind', ['amount', 'entitlement', 'time', 'code', 'missing_input', 'path', 'cutoff'])
def test_tampered_or_rehashed_output_cannot_promote_money_rights_or_time(source, kind):
    folder = Path(create_capital_study(**source)['folder'])
    if kind in ('amount', 'entitlement', 'time'):
        result = json.loads((folder/'result.json').read_bytes())
        if kind == 'amount': result['facts']['capital_after_rial'] += 1
        elif kind == 'entitlement': result['facts']['shares_credited_verified'] = True
        else: result['available_at'] = '2020-01-01T00:00:00+03:30'
        (folder/'result.json').write_text(json.dumps(result), encoding='utf-8')
        rehash(folder, 'result')
    elif kind == 'code': (folder/'code/core/capital_actions.py').write_text('# changed', encoding='utf-8')
    else:
        plan = json.loads((folder/'plan.json').read_bytes())
        if kind == 'missing_input': plan['inputs'].pop('notice.json')
        elif kind == 'path': plan['inputs']['../notice.json'] = '0'*64
        else: plan['cutoff'] = '2030-01-01'
        (folder/'plan.json').write_text(json.dumps(plan), encoding='utf-8')
        rehash(folder, 'plan')
    with pytest.raises((ValueError, OSError)):
        review_capital_study(folder)


def test_notice_link_mismatch_and_future_cutoff_reject_before_publication(source):
    metadata = json.loads(Path(source['notice_path']).read_bytes())
    metadata['url'] = 'https://codal.ir/Reports/Decision.aspx?LetterSerial=OTHER'
    Path(source['notice_path']).write_text(json.dumps(metadata), encoding='utf-8')
    with pytest.raises(ValueError): create_capital_study(**source)
    with pytest.raises(ValueError): create_capital_study(**source, end_exclusive='2030-01-01')


def test_cli_review_prints_utf8_under_incompatible_stdout_locale_without_creating_database(source):
    folder = create_capital_study(**source)['folder']
    missing = Path(source['root']).parent / 'never-opened.db'
    environment = dict(os.environ, BRS_DATA_DIR=str(Path(source['root']).parent.parent),
                       BRS_DB_PATH=str(missing), PYTHONIOENCODING='cp1252')
    completed = subprocess.run([sys.executable, '-m', 'ops.capital_actions', 'review', '--study', folder],
                               capture_output=True, env=environment, timeout=30, check=True)
    decoded = json.loads(completed.stdout.decode('utf-8'))
    assert decoded['facts']['symbol'] == 'آزمون' and not missing.exists()
