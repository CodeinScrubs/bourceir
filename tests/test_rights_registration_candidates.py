import json

import pytest

from ops import rights_registration_candidates as candidate_ops
from ops.rights_registration_candidates import _source_files
from research.rights_registration_candidates import link_notices


def inventory():
    return {'source_start': '2012-01-01', 'end_date_exclusive': '2020-01-01',
            'candidate_rows': [{'ins_code': '11', 'parents': [{'parent_symbol': 'كگل'}]},
                               {'ins_code': '22', 'parents': [{'parent_symbol': 'کگل'},
                                                              {'parent_symbol': 'دیگر'}]}],
            'candidate_starts': [{'ins_code': '11', 'date': '2014-01-01', 'left_censored': True},
                                 {'ins_code': '11', 'date': '2018-01-01', 'left_censored': False},
                                 {'ins_code': '22', 'date': '2013-01-01', 'left_censored': False}]}


def notice(number, symbol, source='one'):
    return {'tracing_no': number, 'symbol_fa': symbol, 'source_published_at': '2015-01-01T10:00:00+03:30',
            'native_metadata_sha256': str(number) * 64, 'source_folder': source, 'title': 'آگهی',
            'url': 'https://codal.ir/Reports/Decision.aspx?LetterSerial=test',
            'company_name': 'ناشر آزمون', 'letter_code': 'ن-۶۷'}


def test_many_to_many_links_preserve_censored_and_negative_lags():
    result = link_notices(inventory(), [notice(1, 'کگل'), notice(2, 'ناشناخته')])
    matched, unmatched = result['notices']
    assert result['version'] == 'rights-registration-candidates-v2'
    assert result['unique_notices'] == 2
    assert result['notices_with_same_symbol_start'] == 1
    assert result['notices_without_same_symbol_start'] == 1
    assert len(matched['same_symbol_starts']) == 3
    assert matched['url'] == 'https://codal.ir/Reports/Decision.aspx?LetterSerial=test'
    assert matched['same_symbol_starts'][0]['days_after_publication'] < 0
    assert matched['same_symbol_starts'][1]['left_censored'] is True
    assert matched['same_symbol_starts'][0]['parent_count'] == 2
    assert not unmatched['same_symbol_starts']
    assert result['legal_events_count'] is None and result['target_failures_count'] is None
    assert not result['same_symbol_establishes_event_identity'] and not result['no_match_proves_no_right']


def test_duplicate_notice_keeps_all_sources_without_double_counting():
    result = link_notices(inventory(), [notice(1, 'کگل', 'a'), notice(1, 'کگل', 'b')])
    assert result['unique_notices'] == 1
    assert result['notices'][0]['sources'] == ['a', 'b']


@pytest.mark.parametrize('change', ['digest', 'symbol', 'date', 'title', 'url', 'company'])
def test_conflicting_same_trace_is_refused(change):
    second = notice(1, 'کگل', 'b')
    values = {'digest': ('native_metadata_sha256', 'b' * 64), 'symbol': ('symbol_fa', 'دیگر'),
              'date': ('source_published_at', '2016-01-01T10:00:00+03:30'), 'title': ('title', 'دیگر'),
              'url': ('url', 'https://codal.ir/Reports/Decision.aspx?LetterSerial=other'),
              'company': ('company_name', 'ناشر دیگر')}
    field, value = values[change]
    second[field] = value
    with pytest.raises(ValueError):
        link_notices(inventory(), [notice(1, 'کگل'), second])


def test_notice_links_must_be_public_codal_documents():
    row = notice(1, 'کگل')
    row['url'] = 'https://example.com/Reports/Decision.aspx?LetterSerial=test'
    with pytest.raises(ValueError):
        link_notices(inventory(), [row])


def test_duplicate_start_and_unknown_parent_fail_closed():
    data = inventory()
    data['candidate_starts'].append(data['candidate_starts'][0])
    with pytest.raises(ValueError):
        link_notices(data, [notice(1, 'کگل')])
    data = inventory()
    data['candidate_starts'][0]['ins_code'] = '33'
    with pytest.raises(ValueError):
        link_notices(data, [notice(1, 'کگل')])


def test_source_snapshot_refuses_active_or_unexpected_files(tmp_path):
    (tmp_path / 'plan.json').write_text('{}', encoding='utf-8')
    (tmp_path / 'page-001.json').write_text('{}', encoding='utf-8')
    snapshot = _source_files(tmp_path)
    assert set(snapshot) == {'plan.json', 'page-001.json'}
    (tmp_path / '.lock').write_text('', encoding='utf-8')
    with pytest.raises(ValueError, match='active'):
        _source_files(tmp_path)
    (tmp_path / '.lock').unlink()
    (tmp_path / 'unexpected.json').write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='Unexpected'):
        _source_files(tmp_path)


@pytest.mark.parametrize(('mode', 'end', 'inventory_end', 'contaminated'), [
    ('pre_holdout', '2024-01-01', '2020-01-01', False),
    ('descriptive_retrospective', '2026-01-01', '2020-01-01', True),
    ('pre_holdout', '2024-01-01', '2026-01-01', True),
])
def test_index_carries_source_mode_and_holdout_contamination(
        tmp_path, monkeypatch, mode, end, inventory_end, contaminated):
    monkeypatch.setattr(candidate_ops.settings, 'DATA_DIR', tmp_path)
    inventory_folder = tmp_path/'private'/'inventory'
    inventory_folder.mkdir(parents=True)
    source_inventory = inventory()
    source_inventory['end_date_exclusive'] = inventory_end
    (inventory_folder/'inventory.json').write_text(json.dumps({'inventory': source_inventory}), encoding='utf-8')
    source = tmp_path/'private'/'source'
    source.mkdir()
    (source/'plan.json').write_text('{}', encoding='utf-8')
    plan = dict(start='2023-01-01', end_exclusive=end, cutoff=end, mode=mode,
                holdout_start='2025-03-21')
    monkeypatch.setattr(candidate_ops, 'review_inventory', lambda _folder: {'status': 'reviewed'})
    monkeypatch.setattr(candidate_ops, 'review_codal', lambda _folder: dict(
        plan=plan, pages_attempted=0, stop_reason=None, document_rows_observed=0))
    result = candidate_ops.build_index(inventory_folder, [str(source)])
    assert result['notice_sources'][0]['mode'] == mode
    assert result['holdout_contaminated_for_rights_hypothesis'] is contaminated
    assert result['independent_holdout_for_rights_hypothesis'] is False
