"""Synthetic-only tests for training cutoff/holdout and event maturity."""

import pandas as pd
import pytest

from research.annual_training import asof_annual_training_labels


# Synthetic weekday sessions: Jan 1 is session zero, Jan 15 session ten,
# and Dec 18 is precisely session 252. No exchange holiday claim.
CALENDAR = pd.bdate_range('2024-01-01', '2024-12-31').strftime('%Y-%m-%d').tolist()
END = CALENDAR[252]


def rows(*, triple='hit', triple_at='2024-01-15', observed='2024-12-31'):
    root = dict(symbol='آزمون', date='2024-01-01', horizon_sessions=252,
                nominal_end_date=END, observed_through=observed)
    return pd.DataFrame([
        dict(root, target='double', threshold=2.0, status='hit', outcome=True,
             resolved_at='2024-01-03', first_hit_date='2024-01-03', first_hit_session=2.0),
        dict(root, target='triple', threshold=3.0, status=triple,
             outcome=True if triple == 'hit' else False,
             resolved_at=triple_at if triple == 'hit' else END,
             first_hit_date=triple_at if triple == 'hit' else None,
             first_hit_session=10.0 if triple == 'hit' else float('nan')),
    ])


def view(outcomes, cutoff='2024-01-10', holdout='2025-03-21', calendar=CALENDAR):
    return asof_annual_training_labels(
        outcomes, train_cutoff=cutoff, holdout_start=holdout, calendar=calendar
    )


def test_early_double_hit_is_not_allowed_to_train_before_full_horizon():
    result = view(rows()).set_index('target')
    assert result['training_label'].isna().all()
    assert result['training_state'].eq('unknown_asof').all()
    assert result.eligible_for_training.tolist() == [False, False]
    assert result.columns.tolist() == ['symbol', 'date', 'training_label',
        'eligible_for_training', 'training_state']


def test_future_winner_and_future_nonwinner_look_identical_asof():
    winner = view(rows(triple='hit'))
    nonwinner = view(rows(triple='complete_no_hit'))
    pd.testing.assert_frame_equal(winner, nonwinner)


def test_label_resolving_on_cutoff_day_is_not_ready():
    result = view(rows(), cutoff='2024-01-03').set_index('target')
    assert result.training_label.isna().all()
    assert result.training_state.eq('unknown_asof').all()


def test_both_early_hit_and_no_hit_only_train_after_exact_horizon_endpoint():
    on_endpoint = view(rows(triple='complete_no_hit'), cutoff=END).set_index('target')
    assert on_endpoint.training_label.isna().all()
    after_endpoint = view(rows(triple='complete_no_hit'), cutoff='2024-12-19').set_index('target')
    assert after_endpoint.loc['double', 'training_label'] == True
    assert after_endpoint.loc['double', 'training_state'] == 'resolved_hit'
    assert after_endpoint.loc['triple', 'training_label'] == False
    assert after_endpoint.loc['triple', 'training_state'] == 'resolved_no_hit'


def test_original_unknown_is_preserved_not_declared_loss():
    frame = rows()
    frame['outcome'] = frame['outcome'].astype(object)
    frame.loc[1, 'status'] = 'missing_observation'
    frame.loc[1, 'outcome'] = None
    frame.loc[1, 'resolved_at'] = None
    frame.loc[1, 'first_hit_date'] = None
    frame.loc[1, 'first_hit_session'] = float('nan')
    result = view(frame).set_index('target')
    assert pd.isna(result.loc['triple', 'training_label'])
    assert not result.loc['triple', 'eligible_for_training']


def test_future_anchor_cannot_enter_training_even_when_it_is_in_snapshot():
    frame = rows()
    frame['date'] = '2024-02-01'
    frame['nominal_end_date'] = None  # fewer than 252 future sessions in this snapshot
    frame['resolved_at'] = ['2024-02-05', '2024-02-15']
    frame['first_hit_date'] = frame['resolved_at']
    assert view(frame).empty


@pytest.mark.parametrize('corruption', [
    'missing_target', 'duplicate_target', 'wrong_name', 'bad_no_hit', 'invalid_date',
    'sealed_observed', 'sealed_anchor', 'future_cutoff', 'unknown_with_truth',
    'false_hit', 'inconsistent_hit_date', 'different_snapshot',
    'triple_without_double', 'reversed_first_hits', 'different_endpoints',
])
def test_fail_closed_on_corrupt_or_unavailable_data(corruption):
    frame = rows()
    kwargs = dict(cutoff='2024-01-10', holdout='2025-03-21')
    if corruption == 'missing_target':
        frame = frame.iloc[:1]
    elif corruption == 'duplicate_target':
        frame = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)
    elif corruption == 'wrong_name':
        frame.loc[0, 'target'] = 'triple'
    elif corruption == 'bad_no_hit':
        frame.loc[1, 'status'] = 'complete_no_hit'
    elif corruption == 'invalid_date':
        frame.loc[0, 'date'] = '2024-1-1'
    elif corruption == 'sealed_observed':
        frame['observed_through'] = '2025-03-21'
    elif corruption == 'sealed_anchor':
        frame['date'] = '2025-03-21'
        frame['observed_through'] = '2025-03-21'
    elif corruption == 'future_cutoff':
        kwargs['cutoff'] = '2025-03-21'
    elif corruption == 'unknown_with_truth':
        frame.loc[1, 'status'] = 'right_censored'
    elif corruption == 'false_hit':
        frame.loc[0, 'outcome'] = False
    elif corruption == 'inconsistent_hit_date':
        frame.loc[0, 'first_hit_date'] = '2024-01-04'
    elif corruption == 'different_snapshot':
        frame.loc[1, 'observed_through'] = '2024-11-20'
    elif corruption == 'triple_without_double':
        frame.loc[0, 'status'] = 'complete_no_hit'
        frame.loc[0, 'outcome'] = False
        frame.loc[0, 'resolved_at'] = frame.loc[0, 'nominal_end_date']
        frame.loc[0, 'first_hit_date'] = None
        frame.loc[0, 'first_hit_session'] = float('nan')
    elif corruption == 'reversed_first_hits':
        frame.loc[1, 'first_hit_session'] = 1.0
    else:
        frame.loc[1, 'nominal_end_date'] = '2024-11-30'
    with pytest.raises(ValueError):
        view(frame, **kwargs)


def test_rejects_after_snapshot_cutoff():
    with pytest.raises(ValueError, match='snapshot'):
        view(rows(observed='2024-12-31'), cutoff='2025-01-01')


def test_rejects_outcome_columns_with_duplicate_names():
    frame = rows()
    frame.columns = frame.columns[:-1].tolist() + [frame.columns[-2]]
    with pytest.raises(ValueError):
        view(frame)


def test_fake_short_endpoint_cannot_early_mature_both_labels():
    frame = rows(triple='complete_no_hit')
    frame['nominal_end_date'] = '2024-01-20'
    frame.loc[frame.target.eq('triple'), 'resolved_at'] = '2024-01-20'
    # Without independent session-index verification, this returns two apparently
    # mature labels in February instead of waiting until the true December endpoint.
    with pytest.raises(ValueError, match='252nd future session'):
        view(frame, cutoff='2024-02-01')


def test_wrong_hit_session_date_is_rejected_even_if_hit_and_resolution_match():
    frame = rows()
    frame.loc[0, 'first_hit_date'] = '2024-01-04'
    frame.loc[0, 'resolved_at'] = '2024-01-04'
    with pytest.raises(ValueError, match='First hit date'):
        view(frame)


def test_off_calendar_decision_anchor_is_rejected():
    frame = rows()
    frame['date'] = '2024-01-06'  # Saturday in the synthetic weekday calendar
    with pytest.raises(ValueError, match='Decision anchor'):
        view(frame)


@pytest.mark.parametrize('calendar', [
    CALENDAR[::-1],
    CALENDAR[:-1] + CALENDAR[-2:],
    CALENDAR + ['2025-03-21'],
])
def test_invalid_registered_calendar_rejected(calendar):
    with pytest.raises(ValueError, match='Registered sessions'):
        view(rows(), calendar=calendar)


def test_registered_calendar_must_not_extend_beyond_observation():
    frame = rows(observed='2024-12-19')
    with pytest.raises(ValueError, match='beyond the observed snapshot'):
        view(frame)


def test_early_hits_with_no_full_endpoint_remain_unavailable_for_training():
    frame = rows()
    frame['nominal_end_date'] = None
    truncated = CALENDAR[:240]
    result = view(frame, cutoff='2024-11-29', calendar=truncated)
    assert len(result) == 2
    assert result.training_label.isna().all()
    assert not result.eligible_for_training.any()


@pytest.mark.parametrize('corruption', [
    'double_no_hit_triple_unknown',
    'triple_no_hit_double_unknown',
    'unequal_unknown_path_failures',
])
def test_shared_price_path_rejects_impossible_paired_statuses(corruption):
    frame = rows(triple='complete_no_hit')
    frame['outcome'] = frame['outcome'].astype(object)
    if corruption == 'double_no_hit_triple_unknown':
        frame.loc[0, ['status', 'outcome', 'resolved_at']] = ['complete_no_hit', False, END]
        frame.loc[0, ['first_hit_date', 'first_hit_session']] = [None, float('nan')]
        frame.loc[1, ['status', 'outcome', 'resolved_at']] = ['missing_observation', None, None]
    elif corruption == 'triple_no_hit_double_unknown':
        frame.loc[0, ['status', 'outcome', 'resolved_at']] = ['missing_observation', None, None]
        frame.loc[0, ['first_hit_date', 'first_hit_session']] = [None, float('nan')]
    else:
        frame.loc[0, ['status', 'outcome', 'resolved_at']] = ['missing_observation', None, None]
        frame.loc[0, ['first_hit_date', 'first_hit_session']] = [None, float('nan')]
        frame.loc[1, ['status', 'outcome', 'resolved_at']] = ['invalid_observation', None, None]
    with pytest.raises(ValueError):
        view(frame, cutoff='2024-12-19')


def test_same_unknown_status_on_both_targets_stays_unresolved():
    frame = rows()
    frame['outcome'] = frame['outcome'].astype(object)
    for index in (0, 1):
        frame.loc[index, ['status', 'outcome', 'resolved_at']] = ['missing_observation', None, None]
        frame.loc[index, ['first_hit_date', 'first_hit_session']] = [None, float('nan')]
    result = view(frame, cutoff='2024-12-19')
    assert result.training_label.isna().all()
    assert not result.eligible_for_training.any()


def test_same_full_path_no_hit_for_both_targets_stays_eligible():
    frame = rows(triple='complete_no_hit')
    frame.loc[0, ['status', 'outcome', 'resolved_at']] = ['complete_no_hit', False, END]
    frame.loc[0, ['first_hit_date', 'first_hit_session']] = [None, float('nan')]
    result = view(frame, cutoff='2024-12-19')
    assert result.eligible_for_training.all()
    assert result.training_label.eq(False).all()
