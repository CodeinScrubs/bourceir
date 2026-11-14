import numpy as np
import pandas as pd
import pytest

from research.owner_opportunity import OpportunityRule, causal_scores, decision_tape, fixed_rules


def prices():
    days = pd.date_range('2022-01-01', periods=100).strftime('%Y-%m-%d').tolist()
    frame = pd.DataFrame(dict(symbol='S', date=days, close_final_raw=102., ref_price_raw=100.,
                              high_raw=103., low_raw=99., volume=10_000, value=1e11))
    return frame, days


def test_raw_compounding_and_positive_gate():
    frame, days = prices()
    result = causal_scores(frame, days)
    assert result.positive_momentum20.iloc[18:19].isna().all()
    assert result.positive_momentum20.iloc[19] == pytest.approx(1.02**20-1)
    assert result.positive_momentum60.iloc[59] == pytest.approx(1.02**60-1)
    frame['close_final_raw'] = 98.
    assert causal_scores(frame, days).positive_momentum20.isna().all()


def test_future_changes_and_backward_adjustments_do_not_change_past():
    frame, days = prices()
    first = causal_scores(frame.iloc[:75], days[:75])
    frame.loc[75:, 'close_final_raw'] = 150.
    frame['adj_close'] = np.arange(len(frame))*1000
    frame['adjustment_factor'] = 9.
    second = causal_scores(frame, days)
    pd.testing.assert_frame_equal(first, second.iloc[:75].reset_index(drop=True))


def test_missing_session_does_not_compress_rolling_history():
    frame, days = prices()
    result = causal_scores(frame.drop(index=30), days)
    assert result.positive_momentum20.iloc[30:50].isna().all()
    assert np.isfinite(result.positive_momentum20.iloc[50])
    assert result.positive_momentum60.iloc[30:90].isna().all()


@pytest.mark.parametrize('mode', ['duplicate', 'off_calendar', 'bad_key'])
def test_rejects_ambiguous_input(mode):
    frame, days = prices()
    if mode == 'duplicate':
        frame = pd.concat([frame, frame.iloc[:1]])
    elif mode == 'off_calendar':
        days = days[1:]
    else:
        frame.loc[0, 'symbol'] = None
    with pytest.raises(ValueError):
        causal_scores(frame, days)


def test_tape_uses_decision_day_only_and_reserves_exit_tail():
    frame, days = prices()
    scores = causal_scores(frame, days)
    rule = OpportunityRule('fixed', 'positive_momentum20', 20)
    tape, horizon, slots = decision_tape(scores, rule, days)
    assert (horizon, slots) == (20, 5)
    assert tape.date.max() == days[-22]
    assert tape.date.min() == days[19]
    assert tape['rank'].eq(0).all()


def test_controls_and_budget_are_fixed():
    rules = fixed_rules()
    assert len(rules) == len({r.name for r in rules}) == 16
    assert sum(r.dip_rule is not None for r in rules) == 3
    frame, days = prices()
    scores = causal_scores(frame, days)
    cash, _, _ = decision_tape(scores, rules[-2], days)
    assert cash.empty
    hold, horizon, slots = decision_tape(scores, rules[-1], days)
    assert hold.date.tolist() == [days[0]] and (horizon, slots) == (98, 1)


def test_random_control_is_order_and_future_invariant():
    frame, days = prices()
    scores = causal_scores(pd.concat([frame, frame.assign(symbol='T')]), days)
    rule = OpportunityRule('random', 'random97', 20)
    a, _, _ = decision_tape(scores, rule, days)
    b, _, _ = decision_tape(scores.sample(frac=1, random_state=7), rule, days)
    pd.testing.assert_frame_equal(a, b)


def test_pullback_condition_has_no_unknown_fallback():
    frame, days = prices()
    frame.loc[95:, 'close_final_raw'] = 99.
    result = causal_scores(frame, days)
    assert result.pullback_in_uptrend.iloc[:95].isna().all()
    assert result.pullback_in_uptrend.iloc[-1] > 0
