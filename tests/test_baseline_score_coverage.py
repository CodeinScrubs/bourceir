"""Scores without inputs remain unknown rather than becoming full-market ranks."""

import numpy as np
import pandas as pd
import pytest

from research import portfolio_run
from research.baselines import (
    STRATEGIES,
    Strategy,
    decision_score_inputs,
    eligible_strategy_score,
    pick_top,
    score_coverage,
)
from research.walkforward import TimeFold


def rule(name):
    return next(item for item in STRATEGIES if item.name == name)


def test_buy_queue_separates_unknown_from_known_nonmatches():
    panel = pd.DataFrame(dict(date=['2024-01-01'] * 5, locked_up=[0, 1, 1, np.nan, 2],
                              lock_up_streak=[np.nan, 3, np.nan, 4, 4]))
    coverage = score_coverage(panel, rule('buy_queue'))
    assert coverage['known_nonmatching_rows'] == 1
    assert coverage['known_score_rows'] == 1
    assert coverage['unknown_score_rows'] == 3
    assert coverage['days_with_known_score_and_unknown'] == 1
    assert 'not a verified complete historical universe' in coverage['ranking_scope']


def test_breakout_distinguishes_negative_return_from_missing_inputs():
    panel = pd.DataFrame(dict(date=['2024-01-01'] * 5, symbol=list('abcde'), value=[100] * 5,
                              ret_1d=[-1, np.nan, 1, 1, np.inf], value_ratio_20d=[np.nan, 3, np.nan, 2, 4]))
    coverage = score_coverage(panel, rule('volume_breakout'))
    assert coverage['known_nonmatching_rows'] == 1
    assert coverage['known_score_rows'] == 1
    assert coverage['unknown_score_rows'] == 3
    # The current rule can produce a finite score from an infinite return;
    # expose this inconsistency rather than treating the row as verified.
    assert coverage['finite_score_with_unknown_inputs'] == 1
    scores = eligible_strategy_score(panel, rule('volume_breakout'))
    assert pd.isna(scores.iloc[-1])
    assert pick_top(panel, scores, 1).symbol.tolist() == ['d']


def test_simple_score_does_not_coerce_boolean_or_text_to_observations():
    panel = pd.DataFrame(dict(date=['2024-01-01'] * 4, score=[1.0, None, True, '4']))
    coverage = score_coverage(panel, Strategy('custom', 'test', lambda p: p.score))
    assert coverage['known_score_rows'] == 1
    assert coverage['unknown_score_rows'] == 3


def test_training_score_cannot_read_future_outcome_columns(monkeypatch):
    dates = pd.date_range('2023-01-01', periods=130).strftime('%Y-%m-%d').tolist()
    panel = pd.DataFrame(dict(date=dates, symbol='الف', value=1_000, entry_date=dates,
        label_status_2d='complete', exit_date_2d=dates, net_ret_2d=.01))
    fold = TimeFold(dates[0], dates[120], dates[125], dates[-1], 5)
    assert 'net_ret_2d' not in decision_score_inputs(panel)
    monkeypatch.setattr(portfolio_run, 'STRATEGIES', [Strategy('leaky', 'test', lambda p: p.net_ret_2d)])
    with pytest.raises(AttributeError):
        portfolio_run.select_from_past(panel, fold, (2,), 1, calendar=dates)
