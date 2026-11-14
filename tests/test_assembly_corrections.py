"""Synthetic notices test graph integrity and prevent fallback to superseded DPS."""

from copy import deepcopy

import pytest

from core.assembly_corrections import resolve_assembly_corrections
from tests.test_assembly_dividend import notice, text


def document(tracing, parent=None, amount='120', *, summary=True):
    meta = notice(summary=summary, correction=parent is not None)
    meta.update(tracing_no=tracing, published_at=f'2023-07-{tracing+20:02d}T13:00:00+03:30')
    body = text(summary=summary, correction=parent is not None, amount=amount)
    if parent is not None:
        body = body.replace('( 10 )', f'( {parent} )')
    return dict(notice=meta, text=body, capture_status='captured')


def resolve(rows, expected=None):
    return resolve_assembly_corrections(rows, expected_tracing_numbers=(
        [r['notice']['tracing_no'] for r in rows] if expected is None else expected), end_exclusive='2025-03-21')


def test_three_revisions_supersede_zero_and_keep_all_evidence_order_independent():
    rows = [document(1, amount='0'), document(2, 1, '188'), document(3, 2, '188')]
    for supplied in (rows, rows[::-1]):
        r = resolve(supplied)
        assert r['status'] == 'resolved_observed_chains'
        assert r['groups'][0]['effective_tracing_numbers'] == [3]
        assert r['groups'][0]['dps_rial'] == '188'
        assert [n['superseded_by'] for n in r['notices']] == [[2], [3], []]
        assert r['settlement_verified'] is r['entitlement_verified'] is False


def test_missing_original_amount_can_be_explicitly_corrected_with_same_structural_identity():
    rows = [document(1, amount=''), document(2, 1, '188')]
    result = resolve(rows)
    assert result['status'] == 'resolved_observed_chains'
    assert result['groups'][0]['dps_rial'] == '188'
    assert result['notices'][0]['fact']['dps_rial'] is None


@pytest.mark.parametrize('mutation', ['missing_latest', 'latest_unavailable', 'latest_blank', 'missing_parent',
    'fork', 'wrong_symbol', 'other_meeting', 'other_period', 'other_form', 'bad_time',
    'unmarked_correction', 'duplicate_conflict', 'bad_latest_reference', 'missing_identity'])
def test_incomplete_or_conflicting_chain_never_promotes_an_older_number(mutation):
    rows = [document(1, amount='0'), document(2, 1, '188'), document(3, 2, '190')]
    expected = [1, 2, 3]
    last = rows[-1]
    if mutation == 'missing_latest':
        rows.pop()
    elif mutation == 'latest_unavailable':
        last.update(text=None, capture_status='error')
    elif mutation == 'latest_blank':
        rows[-1] = document(3, 2, '')
    elif mutation == 'missing_parent':
        rows.pop(1)
        expected = [1, 3]
    elif mutation == 'fork':
        rows[-1] = document(3, 1, '190')
    elif mutation == 'wrong_symbol':
        last['notice']['symbol_fa'] = 'دیگر'
    elif mutation == 'other_meeting':
        last['text'] = last['text'].replace('مورخ 1402/04/29', 'مورخ 1402/04/30')
    elif mutation == 'other_period':
        last['text'] = last['text'].replace('1401/12/29', '1400/12/29')
        last['notice']['title'] = last['notice']['title'].replace('۱۴۰۱', '۱۴۰۰')
    elif mutation == 'other_form':
        rows[-1] = document(3, 2, '190', summary=False)
    elif mutation == 'bad_time':
        last['notice']['published_at'] = rows[1]['notice']['published_at']
    elif mutation == 'unmarked_correction':
        last['notice']['title'] = last['notice']['title'].replace('(اصلاحیه)', '')
    elif mutation == 'duplicate_conflict':
        other = deepcopy(last)
        other['text'] = other['text'].replace('190', '191')
        rows.append(other)
    elif mutation == 'bad_latest_reference':
        last['text'] = last['text'].replace('( 2 )', '( 3 )')
    else:
        rows[0]['text'] = rows[0]['text'].replace('نماد: آزمون', 'نماد: ناشناس')
    result = resolve(rows, expected)
    assert result['status'] == 'unresolved' and result['reasons']
    assert all(g['dps_rial'] is None and not g['observed_chain_resolved'] for g in result['groups'])


def test_independent_summary_and_decision_roots_need_agreement():
    rows = [document(1, amount='120'), document(2, amount='120', summary=False)]
    assert resolve(rows)['groups'][0]['dps_rial'] == '120'
    rows[1] = document(2, amount='121', summary=False)
    assert resolve(rows)['status'] == 'unresolved'


@pytest.mark.parametrize('linked', [False, True])
def test_same_symbol_and_amount_cannot_merge_different_issuers(linked):
    rows = [document(1, amount='188'), document(2, 1 if linked else None, '188')]
    rows[1]['notice']['company_name'] = 'شرکت دیگر'
    rows[1]['text'] = rows[1]['text'].replace('شرکت نمونه - نماد:', 'شرکت شرکت دیگر - نماد:')
    result = resolve(rows)
    assert result['status'] == 'unresolved'
    assert all(group['dps_rial'] is None for group in result['groups'])


def test_exact_duplicates_are_deduplicated_without_changing_the_expected_denominator():
    row = document(1)
    r = resolve([row, deepcopy(row)], [1])
    assert r['status'] == 'resolved_observed_chains'
    assert r['expected_notices'] == len(r['notices']) == 1
    assert r['duplicate_identical_records'] == 1


@pytest.mark.parametrize('ids', [[True], [0], [1, 1]])
def test_expected_inventory_must_have_unique_positive_native_tracing_numbers(ids):
    with pytest.raises(ValueError):
        resolve([], ids)


def test_sealed_notice_is_unresolved_before_its_amount_can_be_used():
    r = resolve_assembly_corrections([document(1)], expected_tracing_numbers=[1], end_exclusive='2023-07-21')
    assert r['status'] == 'unresolved' and r['groups'] == []
