"""Whole-day composition and provenance; synthetic prices exist only in tests."""

from copy import deepcopy

import pandas as pd
import pytest

from core.daily_publications import parse_publication
from core.dated_history import FIELDS, compose_history
from tests.conftest import FOOLAD_INS
from tests.test_daily_publications import DAY, observation, price

AT = '2024-01-03T15:00:00+03:30'
REGISTERED = '2024-01-03T14:30:00+03:30'


def publication(*, native=None, identity='a'):
    source = observation(native=native)
    return dict(archive_id='DAYPUB-'+identity*32, file='observation-00000.json', record_sha256='b'*64,
        archive_registered_at='2024-01-03T13:59:00+03:30',
        archive_completed_at='2024-01-03T14:01:00+03:30',
        attempted_at='2024-01-03T13:59:30+03:30', completed_at='2024-01-03T14:00:30+03:30',
        observation=source, parsed=parse_publication(source, FOOLAD_INS, DAY, 'price'))


def history(*, current=False):
    row = publication()['parsed']['row'].copy()
    row['date'] = DAY if current else '2024-01-02'
    return pd.DataFrame([row])


def compose(frame, receipts, **kwargs):
    return compose_history(frame, receipts, **(dict(ins_code=FOOLAD_INS, instrument_type_code=300,
        session_date=DAY, observed_at=AT, registered_at=REGISTERED) | kwargs))


def test_whole_missing_day_is_appended_without_mutating_history_or_refreshing_receipt():
    frame = history()
    original = frame.copy(deep=True)
    derived, facts = compose(frame, [publication()])
    pd.testing.assert_frame_equal(frame, original)
    assert derived.date.tolist() == ['2024-01-02', DAY]
    assert derived.iloc[-1].to_dict() == publication()['parsed']['row']
    assert facts['status'] == 'appended_whole_dated_candle'
    assert facts['source_initialization_baseline'] is True
    assert facts['references'][0]['original_received_at'] == '2024-01-03T14:00:00+03:30'
    assert facts['references'][0]['source_age_seconds'] == 3600
    assert facts['cache_consumed_at'] == AT
    assert facts['accepted_for_signals'] is facts['profitability_proven'] is False


def test_two_agreeing_publications_keep_one_row_and_both_vintages():
    frame = history(current=True)
    derived, facts = compose(frame, [publication(), publication(identity='c')])
    pd.testing.assert_frame_equal(derived, frame)
    assert facts['status'] == 'matching_publications' and len(facts['references']) == 2


@pytest.mark.parametrize('change', [dict(priceFirst=101), dict(priceMax=102), dict(priceMin=98),
    dict(pDrCotVal=101), dict(pClosing=97), dict(priceYesterday=98),
    dict(qTotTran5J=101, qTotCap=10100), dict(qTotCap=10050), dict(zTotTran=11)])
def test_every_raw_field_conflict_quarantines_day_without_field_repair(change):
    frame = pd.concat([history(), history(current=True)], ignore_index=True)
    original = frame.copy(deep=True)
    derived, facts = compose(frame, [publication(native=price() | change)])
    assert derived.date.tolist() == ['2024-01-02']
    assert facts['status'] == 'conflicting_publications'
    pd.testing.assert_frame_equal(frame, original)


def test_different_cached_vintages_are_conflict_not_newest_wins():
    later = publication(native=price() | dict(pClosing=97), identity='c')
    later['observation']['requested_at'] = later['observation']['received_at'] = '2024-01-03T14:00:10+03:30'
    derived, facts = compose(history(), [publication(), later])
    assert derived.date.tolist() == ['2024-01-02']
    assert facts['status'] == 'conflicting_publications' and len(facts['references']) == 2


def test_invalid_native_day_is_not_repaired_by_a_valid_dated_publication():
    frame = history(current=True)
    frame.loc[0, 'open_raw'] = float('nan')
    derived, facts = compose(frame, [publication()])
    assert derived.empty and facts['status'] == 'conflicting_publications'
    assert pd.isna(frame.loc[0, 'open_raw'])


def test_zero_activity_makes_no_candle_and_conflicts_with_positive_observation():
    zero = publication(native=price() | dict(qTotTran5J=0, qTotCap=0, zTotTran=0))
    derived, facts = compose(None, [zero])
    assert derived is None and facts['status'] == 'zero_activity_no_candle'
    derived, facts = compose(history(current=True), [zero])
    assert derived.empty and facts['status'] == 'conflicting_publications'
    derived, facts = compose(history(), [zero, publication(identity='c')])
    assert derived.date.tolist() == ['2024-01-02'] and facts['status'] == 'conflicting_publications'


def test_missing_cache_preserves_unknown_and_existing_native_history():
    assert compose(None, [])[1]['status'] == 'cache_missing'
    derived, facts = compose(history(current=True), [])
    pd.testing.assert_frame_equal(derived, history(current=True))
    assert facts['status'] == 'history_only' and facts['references'] == []


def test_a_missing_history_allows_only_one_valid_whole_candle_without_inventing_past():
    derived, facts = compose(None, [publication()])
    assert len(derived) == 1 and derived.date.tolist() == [DAY]
    assert set(derived) == {'date', *FIELDS}
    assert facts['full_session_coverage_verified'] is False


def test_old_missing_values_remain_unknown_when_today_is_appended():
    frame = history()
    frame.loc[0, 'open_raw'] = float('nan')
    derived, _ = compose(frame, [publication()])
    assert pd.isna(derived.loc[0, 'open_raw'])


@pytest.mark.parametrize('field,value', [
    ('archive_registered_at', '2024-01-03T14:01:00+03:30'),
    ('archive_completed_at', '2024-01-03T15:01:00+03:30'),
    ('attempted_at', '2024-01-03T14:00:01+03:30'),
    ('completed_at', '2024-01-03T13:59:50+03:30'),
    ('archive_completed_at', '2024-01-03T14:01:00'),
    ('record_sha256', 'bad'), ('archive_id', '../account'), ('file', '../brs.db')])
def test_clock_or_origin_tampering_is_rejected(field, value):
    item = publication()
    item[field] = value
    with pytest.raises(ValueError):
        compose(history(), [item])


@pytest.mark.parametrize('field,value', [('source', 'other'), ('ins_code', '1'), ('price_unit', 'Toman'),
    ('url', 'https://example.org'), ('received_at', '2024-01-04T14:00:00+03:30')])
def test_native_receipt_is_validated_again_at_composition(field, value):
    item = publication()
    item['observation'][field] = value
    with pytest.raises(ValueError):
        compose(history(), [item])


def test_changed_derived_price_is_not_accepted_without_unchanged_native_bytes():
    item = publication()
    item['parsed']['row']['close_final_raw'] = 101
    with pytest.raises(ValueError):
        compose(history(), [item])


def test_duplicate_vintage_is_not_counted_twice():
    with pytest.raises(ValueError):
        compose(history(), [publication(), deepcopy(publication())])


def test_future_registration_wrong_scope_and_optional_age_policy_fail_closed():
    for change in (dict(registered_at='2024-01-03T16:00:00+03:30'), dict(instrument_type_code=301),
                   dict(instrument_type_code=True), dict(session_date='2024-01-04'),
                   dict(max_source_age_seconds=3599), dict(max_source_age_seconds=True)):
        with pytest.raises(ValueError):
            compose(history(), [publication()], **change)
    _, facts = compose(history(), [publication()], max_source_age_seconds=3600,
                       registered_at='2024-01-03T13:00:00+03:30')
    assert facts['source_initialization_baseline'] is False


def test_future_duplicate_and_incomplete_history_keys_fail_before_composition():
    for frame in (history().assign(date='2024-01-04'), pd.concat([history(), history()]),
                  history().drop(columns=['trade_count'])):
        with pytest.raises(ValueError):
            compose(frame, [publication()])
