"""Synthetic report revisions exercise both publication claims and actual receipt."""

import copy
import hashlib
import json

import pytest

from core.codal_monthly_vintage import FALSE_FLAGS, monthly_version, select_monthly_version
from tests.test_codal_monthly_sales import source
from tests.test_codal_revenue import PERIOD, TIMES, find


def bound(*, correction=False, invalid=False):
    data = source()
    trace = 456 if correction else 123
    published = '1403/07/03 09:00:00' if correction else '1403/07/02 09:00:00'
    observed = '2024-09-25T10:00:00+03:30' if correction else TIMES['observed_at']
    data['report'].update(tracing_no=trace, claimed_publish_time_jalali=published)
    if correction:
        find(data, 'Q7')['raw_value'] = find(data, 'Q9')['raw_value'] = '110'
        find(data, 'Q20')['raw_value'] = '173'
    if invalid:
        find(data, 'Q20')['raw_value'] = '999'
    body = json.dumps(data, ensure_ascii=False).encode()
    url = 'https://codal.ir/Reports/MonthlyActivity.aspx?LetterSerial='+str(trace)
    text = 'شرکت:\nناشر آزمون\nنماد:\nآزمون\n'
    if correction:
        text += 'این اطلاعیه اصلاحیه اطلاعیه شماره ( 123 ) می باشد\n'
    else:
        text += 'این اطلاعیه نسخه جدیدتری به شماره ( 456 ) دارد\n'
    manifest = dict(snapshot_id='CODALTABLE-'+str(trace), expected_tracing_no=trace,
        source_url=url, effective_url=url, requested_at=observed, observed_at=observed,
        archived_at=observed, available_at=observed,
        files_sha256={'tables.json': hashlib.sha256(body).hexdigest(),
                      'report.txt': hashlib.sha256(text.encode()).hexdigest()})
    letter = dict(tracing_no=trace, symbol_fa='آزمون', company_name='ناشر آزمون', letter_code='ن-30',
        source_estimate=False, url=url, title=f'گزارش فعالیت ماهانه دوره 1 ماهه منتهی به {PERIOD}'+(' (اصلاحیه)' if correction else ''),
        source_published_at='2024-09-24T09:00:00+03:30' if correction else '2024-09-23T09:00:00+03:30',
        native_metadata_sha256=hashlib.sha256(b'synthetic native metadata').hexdigest())
    return dict(manifest=manifest, data=data), letter, body, text


def version(**kwargs):
    loaded, letter, body, text = bound(**kwargs)
    return monthly_version(loaded, letter, source_bytes=body, visible_text=text)


def test_known_codal_plus_redirect_spelling_preserves_exact_native_and_source_identity():
    loaded, letter, body, text = bound()
    letter['url'] = loaded['manifest']['source_url'] = 'https://codal.ir/Reports/Decision.aspx?LetterSerial=Ab%2BCd%3D%3D'
    loaded['manifest']['effective_url'] = 'https://codal.ir/Reports/MonthlyActivity.aspx?LetterSerial=AbOOObOOOCd%3D%3D'
    result = monthly_version(loaded, letter, source_bytes=body, visible_text=text)
    assert result['effective_serial_representation'] == 'codal_plus_redirect_spelling'
    assert result['tracing_no'] == 123
    assert all(result[k] is False for k in FALSE_FLAGS)


@pytest.mark.parametrize('mutation', ['source_changed', 'effective_other', 'reverse_collision', 'tracing_changed'])
def test_redirect_spelling_is_not_a_general_serial_or_report_identity_bypass(mutation):
    loaded, letter, body, text = bound()
    prefix = 'https://codal.ir/Reports/MonthlyActivity.aspx?LetterSerial='
    letter['url'] = loaded['manifest']['source_url'] = prefix+'Ab%2BCd%3D%3D'
    loaded['manifest']['effective_url'] = prefix+'AbOOObOOOCd%3D%3D'
    if mutation == 'source_changed':
        loaded['manifest']['source_url'] = loaded['manifest']['effective_url']
    elif mutation == 'effective_other':
        loaded['manifest']['effective_url'] = prefix+'OtherOOObOOOCd%3D%3D'
    elif mutation == 'reverse_collision':
        letter['url'] = loaded['manifest']['source_url'] = prefix+'AbOOObOOOCd%3D%3D'
        loaded['manifest']['effective_url'] = prefix+'Ab%2BCd%3D%3D'
    else:
        letter['tracing_no'] = 999
    with pytest.raises(ValueError):
        monthly_version(loaded, letter, source_bytes=body, visible_text=text)


def test_original_and_correction_cannot_be_conflated_before_the_later_publication():
    versions = [version(), version(correction=True)]
    before = select_monthly_version(versions, decision_at='2024-09-23T12:00:00+03:30', clock='source_claim')
    after = select_monthly_version(versions, decision_at='2024-09-24T12:00:00+03:30', clock='source_claim')
    assert before['selected_tracing_no'] == 123 and before['total_revenue_rial'] == '163000000'
    assert after['selected_tracing_no'] == 456 and after['total_revenue_rial'] == '173000000'
    assert before['future_versions_excluded'] == 1 and after['future_versions_excluded'] == 0
    assert versions[0]['later_revision_banner_tracing_no'] == 456
    assert versions[1]['corrects_tracing_no'] == 123
    assert all(v['revision_banners_are_model_inputs'] is False for v in versions)
    assert all(v[k] is False for v in [*versions, before, after] for k in FALSE_FLAGS)


def test_actual_availability_does_not_inherit_the_earlier_publication_claim():
    versions = [version(), version(correction=True)]
    before = select_monthly_version(versions, decision_at='2024-09-23T09:30:00+03:30')
    pending = select_monthly_version(versions, decision_at='2024-09-24T12:00:00+03:30')
    after = select_monthly_version(versions, decision_at='2024-09-25T12:00:00+03:30')
    assert before['status'] == 'not_available' and before['total_revenue_rial'] is None
    assert pending['selected_tracing_no'] == 123
    assert after['selected_tracing_no'] == 456


def test_a_later_recapture_of_original_must_not_replace_a_known_correction():
    original, correction = version(), version(correction=True)
    original['observed_at'] = original['available_at'] = '2024-09-26T10:00:00+03:30'
    selected = select_monthly_version([original, correction], decision_at='2024-09-27T12:00:00+03:30')
    assert selected['selected_tracing_no'] == 456
    assert selected['selected_at'] == correction['available_at']


@pytest.mark.parametrize('clock', ['observed', 'source_claim'])
def test_an_unusable_latest_correction_blocks_silent_fallback_to_an_older_number(clock):
    versions = [version(), version(correction=True, invalid=True)]
    selected = select_monthly_version(versions, decision_at='2024-09-26T12:00:00+03:30', clock=clock)
    assert selected['status'] == 'unusable' and selected['selected_tracing_no'] == 456
    assert selected['total_revenue_rial'] is None


def test_equal_publication_times_are_ambiguous_even_with_different_capture_times():
    original, correction = version(), version(correction=True)
    correction['source_published_at_claim'] = original['source_published_at_claim']
    selected = select_monthly_version([original, correction], decision_at='2024-09-26T12:00:00+03:30')
    assert selected['status'] == 'ambiguous_latest'
    assert selected['selected_tracing_no'] is selected['total_revenue_rial'] is None


@pytest.mark.parametrize('mutation', ['cells', 'loaded_data', 'visible_text', 'url', 'issuer', 'period', 'published',
    'metadata_hash', 'availability', 'estimated', 'not_monthly', 'self_revision', 'missing_revision_banner'])
def test_source_identity_time_and_byte_bindings_cannot_be_forged(mutation):
    loaded, letter, body, text = bound(correction=True)
    if mutation == 'cells':
        body += b' '
    elif mutation == 'loaded_data':
        loaded['data']['report']['tracing_no'] = 999
    elif mutation == 'visible_text':
        text += 'changed'
    elif mutation == 'url':
        letter['url'] = letter['url'].replace('456', '789')
    elif mutation == 'issuer':
        letter['company_name'] = 'ناشر دیگر'
    elif mutation == 'period':
        letter['title'] = letter['title'].replace(PERIOD, '1403/05/31')
    elif mutation == 'published':
        letter['source_published_at'] = '2024-09-24T09:01:00+03:30'
    elif mutation == 'metadata_hash':
        letter['native_metadata_sha256'] = None
    elif mutation == 'availability':
        loaded['manifest']['available_at'] = '2000-01-01T00:00:00+03:30'
    elif mutation == 'estimated':
        letter['source_estimate'] = True
    elif mutation == 'not_monthly':
        letter['title'] = letter['title'].replace('1 ماهه', '3 ماهه')
    else:
        text = text.replace('( 123 )', '( 456 )') if mutation == 'self_revision' else text.split('این اطلاعیه')[0]
        loaded['manifest']['files_sha256']['report.txt'] = hashlib.sha256(text.encode()).hexdigest()
    with pytest.raises(ValueError):
        monthly_version(loaded, letter, source_bytes=body, visible_text=text)


@pytest.mark.parametrize('mutation', ['issuer', 'period', 'duplicate', 'flag', 'usability', 'backdated', 'clock'])
def test_selector_rejects_unbound_mixed_or_upgraded_versions(mutation):
    versions = [version(), version(correction=True)]
    clock = 'observed'
    if mutation == 'issuer':
        versions[1]['symbol_fa'] = 'نماد دیگر'
    elif mutation == 'period':
        versions[1]['period_end_iso'] = '2024-08-21'
    elif mutation == 'duplicate':
        versions.append(copy.deepcopy(versions[0]))
    elif mutation == 'flag':
        versions[1]['accepted_for_features'] = True
    elif mutation == 'usability':
        versions[1]['status'] = 'unusable'
    elif mutation == 'backdated':
        versions[1]['available_at'] = '2000-01-01T00:00:00+03:30'
    else:
        clock = 'prefer_profitable_version'
    with pytest.raises(ValueError):
        select_monthly_version(versions, decision_at='2024-09-26T12:00:00+03:30', clock=clock)
