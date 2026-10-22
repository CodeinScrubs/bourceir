"""Fictional issuer data only here and in pytest's temporary archive."""

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
from core.rights_evidence import FALSE_FLAGS, primary_rights_registration
from core.text import to_fa_digits
from ops.codal_documents import capture_codal_document
from ops.rights_registration import create_rights_study, review_rights_study

TEXT = '''مشخصات
آگهی ثبت افزایش سرمایه
شرکت صنعت ابزار - نماد: آزمون
موضوع: آگهی ثبت افزایش سرمایه
بر اساس تصمیمات مجمع عمومی فوق العاده مورخ
1402/12/02
افزایش سرمایۀ شرکت از مبلغ
1,000
میلیون ریال به‌مبلغ
1,500
میلیون ریال (
از محل مطالبات و آورده نقدی مبلغ
400
میلیون ریال ،
از محل سود انباشته مبلغ
100
میلیون ریال و از آورده نقدی با سلب حق تقدم از سهامداران فعلی مبلغ
میلیون ریال )
در در تاریخ
1402/12/23
در مرجع ثبت شرکت ها به ثبت رسیده است.
'''
LICENSE = 'با عنایت به مجوز افزایش سرمایه به شماره مجوز DPM-TEST-123 مورخ 1402/11/17\n'
URL = 'https://codal.ir/Reports/Decision.aspx?LetterSerial=RIGHTSTEST&let=28'
CUTOFF = '2025-03-21'


def notice(**changes):
    return dict(dict(tracing_no=321, symbol_fa='آزمون', company_name='صنعت ابزار',
        title='آگهی ثبت افزایش سرمایه', published_at='2024-03-13T12:30:00+03:30', url=URL), **changes)


def facts(text=TEXT, metadata=None):
    return primary_rights_registration(text, notice() if metadata is None else metadata, end_exclusive=CUTOFF)


def test_missing_license_is_unknown_and_combined_funding_is_not_holder_cash():
    result = facts()
    assert result['status'] == 'supported_primary_fact'
    assert result['license_date'] is None and result['license_status'] == 'not_in_primary_text'
    assert result['meeting_date'] == '2024-02-21' and result['registration_date'] == '2024-03-13'
    assert result['capital_before_rial'] == 1_000_000_000
    assert result['capital_after_rial'] == 1_500_000_000
    assert result['increase_rial'] == 500_000_000
    assert result['claims_and_cash_rial'] == 400_000_000
    assert result['retained_earnings_rial'] == 100_000_000
    assert result['waived_preemption_cash_rial'] is None
    assert result['unassigned_after_known_funding_rial'] == 0
    assert result['capital_multiplier'] == dict(numerator=3, denominator=2)
    assert result['increase_ratio'] == dict(numerator=1, denominator=2)
    assert result['claims_cash_equals_increase'] is False
    assert all(result[k] is False for k in FALSE_FLAGS)
    assert all(result[k] is None for k in ('holder_cash_required_rial', 'holder_claims_used_rial',
                                         'ex_date', 'holder_credit_date'))
    # The earlier interpreter/contract is not silently broadened.
    assert primary_capital_registration(TEXT, notice(), end_exclusive=CUTOFF)['status'] == 'unsupported_primary_text'


def test_present_license_and_source_normalization_preserve_dates_and_amounts():
    result = facts(LICENSE+TEXT)
    assert result['status'] == 'supported_primary_fact'
    assert result['license_date'] == '2024-02-06' and result['license_status'] == 'stated_in_primary_text'
    converted = to_fa_digits(LICENSE+TEXT).replace(',', '٬').replace('شرکت', 'شركت')
    assert facts(converted) == result


def test_all_combined_funding_does_not_establish_zero_holder_payment_or_full_evidence():
    text = TEXT.replace('400\n', '500\n').replace('100\nمیلیون', '\nمیلیون')
    result = facts(text)
    assert result['status'] == 'supported_primary_fact'
    assert result['claims_cash_equals_increase'] is True
    assert result['retained_earnings_rial'] is None
    assert result['holder_cash_required_rial'] is None
    assert result['funding_completeness_verified'] is False


@pytest.mark.parametrize('value,expected', [('0', 0), ('', None), ('50', 50_000_000)])
def test_explicit_zero_blank_and_partly_unassigned_funding_are_distinct(value, expected):
    result = facts(TEXT.replace('100\nمیلیون', value+'\nمیلیون'))
    assert result['status'] == 'supported_primary_fact'
    assert result['retained_earnings_rial'] == expected
    assert result['unassigned_after_known_funding_rial'] == 100_000_000-(expected or 0)
    assert result['funding_completeness_verified'] is False


@pytest.mark.parametrize('before,after', [
    ('1,000', '1,000'), ('0', '1,500'), ('1,000', '999'), ('1,00', '1,500'),
    ('1e3', '1,500'), ('1,000', 'NaN'), ('100000000000000', '150000000000000'),
    ('0.0000001', '0.0000002'),
])
def test_invalid_amounts_never_make_a_financial_fact(before, after):
    result = facts(TEXT.replace('1,000\n', before+'\n').replace('1,500\n', after+'\n'))
    assert result['status'] == 'unsupported_primary_text'
    assert result['capital_before_rial'] is None and result['claims_and_cash_rial'] is None


@pytest.mark.parametrize('change', [
    ('میلیون ریال', 'تومان'), ('میلیون ریال', 'هزار ریال'),
    ('صنعت ابزار - نماد: آزمون', 'صنعت دیگر - نماد: آزمون'),
    ('صنعت ابزار - نماد: آزمون', 'صنعت ابزار - نماد: دیگر'),
    ('موضوع: آگهی ثبت افزایش سرمایه', 'موضوع: پیشنهاد افزایش سرمایه'),
    ('1402/12/02', '1402/12/24'), ('1402/12/23', '1402/13/23'),
    ('400\n', '401\n'), ('400\n', '-400\n'), ('400\n', 'abc\n'),
    ('400\n', '\n'), ('400\n', '0\n'),
])
def test_wrong_identity_units_dates_or_funding_leave_money_unknown(change):
    result = facts(TEXT.replace(*change))
    assert result['status'] == 'unsupported_primary_text'
    assert result['increase_rial'] is None and result['claims_and_cash_rial'] is None


@pytest.mark.parametrize('prefix', [
    LICENSE.replace('1402/11/17', '1402/12/24'),
    LICENSE.replace('1402/11/17', 'نامعلوم'),
    LICENSE+LICENSE,
    'مجوز افزایش سرمایه اعلام شده است\n',
    'مجوزافزایشسرمایه اعلام شده است\n',
    'شماره مجوز DPM-TEST-123 مورخ نامعلوم\n',
    LICENSE+'شماره مجوز دیگری اعلام شده است\n',
])
def test_present_bad_or_duplicate_license_is_not_treated_as_missing(prefix):
    result = facts(prefix+TEXT)
    assert result['status'] == 'unsupported_primary_text' and result['capital_before_rial'] is None


def test_repeated_identity_amount_or_funding_cannot_be_resolved_by_choosing_first():
    for text in (TEXT+TEXT, TEXT+'\nاز محل مطالبات و آورده نقدی مبلغ 400 میلیون ریال'):
        result = facts(text)
        assert result['status'] == 'unsupported_primary_text' and result['increase_rial'] is None


@pytest.mark.parametrize('metadata', [
    dict(tracing_no=True), dict(tracing_no=0), dict(company_name=None), dict(symbol_fa=''),
    dict(published_at='2024-03-13T12:30:00'), dict(published_at='2024-03-12T12:30:00+03:30'),
    dict(title='مجوز افزایش سرمایه'),
])
def test_malformed_notice_is_rejected(metadata):
    assert facts(metadata=notice(**metadata))['status'] == 'unsupported_primary_text'


@pytest.mark.parametrize('change', [
    ('1402/12/02', '1404/01/01'), ('1402/12/23', '1404/01/01'),
])
def test_cutoff_is_checked_before_any_amount_parsing(monkeypatch, change):
    monkeypatch.setattr('core.rights_evidence._rial', lambda *_: pytest.fail('withheld amounts interpreted'))
    result = facts(TEXT.replace(*change).replace('1,500', 'bad'))
    assert result['status'] == 'withheld_by_cutoff' and result['increase_rial'] is None


def test_future_license_and_tehran_publication_boundary_withhold_amounts(monkeypatch):
    monkeypatch.setattr('core.rights_evidence._rial', lambda *_: pytest.fail('withheld amounts interpreted'))
    assert facts(LICENSE.replace('1402/11/17', '1404/01/01')+TEXT)['status'] == 'withheld_by_cutoff'
    result = facts(metadata=notice(published_at='2025-03-20T21:00:00+00:00'))
    assert result['status'] == 'withheld_by_cutoff'


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)

    class Provider:
        def get_document(self, url):
            assert url == URL
            return dict(status='captured', source_url=url, effective_url=url,
                requested_at='2024-03-14T10:00:00+03:30', observed_at='2024-03-14T10:00:01+03:30',
                response_sha256='1'*64, response_bytes=1000, format='text', content=TEXT.encode(), links=[])

    document = capture_codal_document(URL, provider=Provider())
    metadata = tmp_path/'private'/'notice.json'
    metadata.write_text(json.dumps(dict(notice(), ignored_extra='not copied'), ensure_ascii=False), encoding='utf-8')
    return dict(notice_path=metadata, document=document['folder'], root=tmp_path/'private/rights-registrations')


def test_study_is_self_contained_and_reviews_without_original_db_or_network(source, monkeypatch):
    monkeypatch.setattr(socket, 'create_connection', lambda *_a, **_k: pytest.fail('network opened'))
    monkeypatch.setattr('database.db.DatabaseManager.__init__', lambda *_a, **_k: pytest.fail('DB opened'))
    result = create_rights_study(**source)
    folder = Path(result['folder'])
    assert result['facts'] == facts() and result['artifacts_verified']
    assert result['semantic_code_matches_current'] and result['capture_code_matches_current']
    assert 'ignored_extra' not in json.loads((folder/'notice.json').read_bytes())
    shutil.rmtree(source['document'])
    Path(source['notice_path']).unlink()
    assert review_rights_study(folder)['facts'] == facts()


def rehash(folder, field):
    manifest = json.loads((folder/'manifest.json').read_bytes())
    manifest[field+'_sha256'] = hashlib.sha256((folder/(field+'.json')).read_bytes()).hexdigest()
    (folder/'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')


@pytest.mark.parametrize('kind', ['amount', 'entitlement', 'time', 'code', 'missing_input', 'path', 'cutoff', 'read_flag'])
def test_rehashed_tampering_cannot_promote_money_rights_or_time(source, kind):
    folder = Path(create_rights_study(**source)['folder'])
    if kind in ('amount', 'entitlement', 'time'):
        result = json.loads((folder/'result.json').read_bytes())
        if kind == 'amount':
            result['facts']['claims_and_cash_rial'] += 1
        elif kind == 'entitlement':
            result['facts']['holder_entitlement_verified'] = True
        else:
            result['available_at'] = '2000-01-01T00:00:00+03:30'
        (folder/'result.json').write_text(json.dumps(result), encoding='utf-8')
        rehash(folder, 'result')
    else:
        plan = json.loads((folder/'plan.json').read_bytes())
        if kind == 'code':
            file = folder/'code/core/rights_evidence.py'
            file.write_text('raise RuntimeError("archived code must never run")\n', encoding='utf-8')
            plan['code']['core/rights_evidence.py'] = hashlib.sha256(file.read_bytes()).hexdigest()
        elif kind == 'missing_input':
            del plan['inputs']['notice.json']
        elif kind == 'path':
            plan['inputs']['../outside'] = '1'*64
        elif kind == 'cutoff':
            plan['cutoff'] = '2025-03-22'
        else:
            plan['database_read'] = True
        (folder/'plan.json').write_text(json.dumps(plan), encoding='utf-8')
        rehash(folder, 'plan')
    with pytest.raises(ValueError):
        review_rights_study(folder)


def test_cli_offline_review_recomputes_the_same_fact(source):
    folder = create_rights_study(**source)['folder']
    missing = Path(source['root']).parent.parent/'must-not-exist.db'
    environment = dict(os.environ, BRS_DATA_DIR=str(Path(source['root']).parent.parent),
                       BRS_DB_PATH=str(missing), PYTHONIOENCODING='cp1252')
    child = subprocess.run([sys.executable, '-m', 'ops.rights_registration', 'review', '--study', folder],
        env=environment, capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert child.returncode == 0, child.stderr
    reviewed = json.loads(child.stdout)
    assert reviewed['facts'] == facts() and reviewed['artifacts_verified']
    assert not missing.exists()


@pytest.mark.parametrize('kind', ['future', 'before_source'])
def test_rehashed_study_clock_cannot_be_future_or_before_its_source(source, kind):
    folder = Path(create_rights_study(**source)['folder'])
    plan = json.loads((folder/'plan.json').read_bytes())
    result = json.loads((folder/'result.json').read_bytes())
    if kind == 'future':
        plan['registered_at'] = '2998-12-31T23:59:00+03:30'
        result['created_at'] = result['available_at'] = '2999-01-01T00:00:00+03:30'
    else:
        plan['registered_at'] = '2000-01-01T00:00:00+03:30'
    (folder/'plan.json').write_text(json.dumps(plan), encoding='utf-8')
    (folder/'result.json').write_text(json.dumps(result), encoding='utf-8')
    rehash(folder, 'plan')
    rehash(folder, 'result')
    with pytest.raises(ValueError):
        review_rights_study(folder)


def test_public_output_path_and_post_holdout_cutoff_are_rejected_before_copy(source, tmp_path):
    invalid = dict(source, root=tmp_path/'public')
    with pytest.raises(ValueError):
        create_rights_study(**invalid)
    assert not (tmp_path/'public').exists()
    with pytest.raises(ValueError):
        create_rights_study(**source, end_exclusive='2025-03-22')
