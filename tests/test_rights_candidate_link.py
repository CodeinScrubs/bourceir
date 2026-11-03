"""Linkage tests use synthetic data only and never classify a financial result."""

import json

import pytest

from ops import rights_candidate_link as workflow
from research.rights_candidate_link import candidate_links


@pytest.fixture
def inventory():
    return {'source_start': '2012-03-20', 'end_date_exclusive': '2025-03-21',
            'candidate_rows': [
                {'ins_code': '123', 'parents': [{'parent_symbol': 'دانا'}]},
                {'ins_code': '124', 'parents': [{'parent_symbol': 'دانا'}, {'parent_symbol': 'دیگر'}]},
                {'ins_code': '125', 'parents': [{'parent_symbol': 'دیگر'}]},
            ], 'candidate_starts': [
                {'ins_code': '123', 'date': '2018-03-18', 'left_censored': True},
                {'ins_code': '123', 'date': '2018-10-02', 'left_censored': False},
                {'ins_code': '124', 'date': '2017-10-02', 'left_censored': False},
                {'ins_code': '125', 'date': '2018-02-20', 'left_censored': False},
            ]}


def notice(**updates):
    return {'tracing_no': 1, 'symbol': 'دانا', 'meeting_date': '2018-02-18',
            'study_id': 'RIGHTS-a'} | updates


def test_links_preserve_all_same_symbol_rows_including_ambiguous_and_before_meeting(inventory):
    result = candidate_links(inventory, [notice()])
    links = result['notices'][0]['same_symbol_starts']
    assert [(row['ins_code'], row['days_from_meeting']) for row in links] == [
        ('124', -139), ('123', 28), ('123', 226)]
    assert links[0]['parent_count'] == 2
    assert links[1]['left_censored'] is True
    assert result['links_establish_event_identity'] is False
    assert result['x_total_capital_events'] is None
    assert result['y_rights_events'] is None
    assert result['z_discount_events'] is None
    assert result['a_failed_target_events'] is None


def test_duplicate_studies_are_one_notice_with_all_provenance(inventory):
    result = candidate_links(inventory, [notice(), notice(study_id='RIGHTS-b')])
    assert result['notice_count'] == 1
    assert result['notices'][0]['source_study_ids'] == ['RIGHTS-a', 'RIGHTS-b']


def test_conflicting_duplicate_tracing_fails(inventory):
    with pytest.raises(ValueError, match='Conflicting'):
        candidate_links(inventory, [notice(), notice(meeting_date='2019-02-18')])


def test_absent_matching_symbol_is_retained(inventory):
    result = candidate_links(inventory, [notice(symbol='ناموجود')])
    assert result['notice_count'] == 1
    assert result['notices'][0]['same_symbol_starts'] == []


def test_duplicate_starts_rejected(inventory):
    inventory['candidate_starts'].append(inventory['candidate_starts'][0])
    with pytest.raises(ValueError, match='unique'):
        candidate_links(inventory, [notice()])


def test_heldout_and_unknown_starts_rejected(inventory):
    inventory['candidate_starts'][0]['date'] = '2025-03-21'
    with pytest.raises(ValueError, match='window'):
        candidate_links(inventory, [notice()])
    inventory['candidate_starts'][0] = {'ins_code': '999', 'date': '2018-01-01', 'left_censored': False}
    with pytest.raises(ValueError, match='identity'):
        candidate_links(inventory, [notice()])


def test_cannot_expand_window_into_sealed_period(inventory):
    inventory['end_date_exclusive'] = '2025-03-22'
    with pytest.raises(ValueError, match='window'):
        candidate_links(inventory, [notice()])


def test_workflow_requires_successful_reviews_before_linking(tmp_path, monkeypatch, inventory):
    cohort = tmp_path / 'cohort'
    registration = tmp_path / 'registration'
    cohort.mkdir()
    registration.mkdir()
    (cohort / 'inventory.json').write_text(json.dumps({'inventory': inventory}), encoding='utf-8')
    (registration / 'manifest.json').write_text('{}', encoding='utf-8')
    (registration / 'notice.json').write_text('{"tracing_no": 1}', encoding='utf-8')
    calls = []
    monkeypatch.setattr(workflow, 'read_private_report', lambda path: json.loads(path.read_text(encoding='utf-8')))
    monkeypatch.setattr(workflow, 'review_inventory', lambda _: calls.append('cohort'))
    monkeypatch.setattr(workflow, 'review_rights_study', lambda _: (calls.append('registration') or
        {'facts': {'status': 'supported_primary_fact', 'symbol': 'دانا', 'meeting_date': '2018-02-18'}}))
    result = workflow.review_links(cohort, [registration])
    assert calls == ['cohort', 'registration']
    assert result['notice_count'] == 1
    assert result['accepted_for_signals'] is False

    def reject(_):
        raise ValueError('bad archived fingerprint')

    monkeypatch.setattr(workflow, 'review_rights_study', reject)
    with pytest.raises(ValueError, match='fingerprint'):
        workflow.review_links(cohort, [registration])


def test_workflow_rejects_notice_changed_during_review(tmp_path, monkeypatch, inventory):
    cohort, registration = tmp_path / 'cohort', tmp_path / 'registration'
    cohort.mkdir()
    registration.mkdir()
    (cohort / 'inventory.json').write_text(json.dumps({'inventory': inventory}), encoding='utf-8')
    (registration / 'manifest.json').write_text('{}', encoding='utf-8')
    notice_path = registration / 'notice.json'
    notice_path.write_text('{"tracing_no": 1}', encoding='utf-8')
    monkeypatch.setattr(workflow, 'read_private_report', lambda path: json.loads(path.read_text(encoding='utf-8')))
    monkeypatch.setattr(workflow, 'review_inventory', lambda _: None)

    def mutate(_):
        notice_path.write_text('{"tracing_no": 2}', encoding='utf-8')
        return {'facts': {'status': 'supported_primary_fact'}}

    monkeypatch.setattr(workflow, 'review_rights_study', mutate)
    with pytest.raises(ValueError, match='changed'):
        workflow.review_links(cohort, [registration])
