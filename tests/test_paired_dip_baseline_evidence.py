"""A paired attempt's resolution time must not masquerade as an execution date."""

import numpy as np
import pandas as pd
import pytest

from research import portfolio_run
from research.baselines import Strategy
from research.dip_expectancy_run import _past_choice
from research.walkforward import TimeFold


def fixture(monkeypatch):
    dates = pd.date_range('2023-01-01', periods=150).strftime('%Y-%m-%d').tolist()
    clock = pd.to_datetime(dates)
    train = pd.DataFrame(dict(date=dates, symbol='a', value=1000., score=1.))
    targets = pd.DataFrame(dict(date=dates, symbol='a', state='complete',
        resolved_at=(clock + pd.Timedelta(days=2)).strftime('%Y-%m-%d'), floor_net=.02))
    fold = TimeFold(dates[0], dates[120], dates[125], dates[145], 5)
    monkeypatch.setattr(portfolio_run, 'STRATEGIES', [Strategy('fixed', 'fixed', lambda p: p.score)])
    return dates, train, targets, fold


def test_paired_attempt_uses_resolution_before_boundary_without_faking_execution_dates(monkeypatch):
    dates, train, targets, fold = fixture(monkeypatch)
    best, diagnostics = _past_choice(train, targets, fold, dates)
    assert best['strategy'] == 'fixed' and best['mean_cohort'] == pytest.approx(.02 / 5)
    assert diagnostics[0]['unknown_fraction'] == pytest.approx(2 / 120)
    # A positive paired attempt may represent one filled scenario and one blocked scenario;
    # only its explicit aggregate result and resolution time enter this comparison.
    changed = targets.copy()
    changed.loc[changed.date.ge(fold.test_start), 'floor_net'] = -999.
    assert _past_choice(train, changed, fold, dates)[0] == best


def test_unresolved_or_nonchronological_attempts_cannot_boost_training(monkeypatch):
    dates, train, targets, fold = fixture(monkeypatch)
    targets.loc[:6, 'resolved_at'] = targets.loc[:6, 'date']
    best, diagnostics = _past_choice(train, targets, fold, dates)
    assert best is None and diagnostics[0]['unknown_fraction'] == pytest.approx(9 / 120)


def test_blocked_attempt_requires_zero_floor(monkeypatch):
    dates, train, targets, fold = fixture(monkeypatch)
    targets.loc[0, ['state', 'floor_net']] = ['entry_blocked', 0.]
    best, _ = _past_choice(train, targets, fold, dates)
    assert best is not None
    targets.loc[0, 'floor_net'] = .1
    with pytest.raises(ValueError, match='zero'):
        _past_choice(train, targets, fold, dates)
    targets.loc[0, 'floor_net'] = np.nan
    with pytest.raises(ValueError, match='zero'):
        _past_choice(train, targets, fold, dates)


def test_scoring_cannot_read_resolution_time(monkeypatch):
    dates, train, targets, fold = fixture(monkeypatch)
    monkeypatch.setattr(portfolio_run, 'STRATEGIES', [Strategy('leaky', 'leaky', lambda p: p.resolved_at)])
    with pytest.raises(AttributeError):
        _past_choice(train, targets, fold, dates)
