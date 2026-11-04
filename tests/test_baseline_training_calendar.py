"""Missing sessions must remain unknown during historical baseline selection."""

import pandas as pd
import pytest

from research import portfolio_run
from research.baselines import Strategy
from research.walkforward import TimeFold, preceding_price_observations


def _panel(dates):
    clock = pd.to_datetime(dates)
    return pd.DataFrame(dict(date=dates, symbol='الف', value=1_000., score=1.,
        entry_date=(clock + pd.Timedelta(days=1)).strftime('%Y-%m-%d'), entry_executable=True,
        label_status_2d='complete', exit_date_2d=(clock + pd.Timedelta(days=2)).strftime('%Y-%m-%d'),
        net_ret_2d=.01))


def _setup(monkeypatch):
    days = pd.date_range('2023-01-01', periods=150).strftime('%Y-%m-%d').tolist()
    monkeypatch.setattr(portfolio_run, 'STRATEGIES', [Strategy('fixed', 'fixed', lambda p: p.score)])
    return days, TimeFold(days[0], days[120], days[125], days[145], 5)


def test_missing_eligible_days_remain_unknown_and_can_disqualify_a_baseline(monkeypatch):
    days, fold = _setup(monkeypatch)
    panel = _panel(days)
    selected, candidates = portfolio_run.select_from_past(panel, fold, (2,), 1, calendar=days)
    assert selected['strategy'] == 'fixed'
    assert candidates[0]['scored_days'] == 120 and candidates[0]['missing_panel_days'] == 0

    # Seven missing days are 5.83% of the original calendar, not a zero return
    # or a reason to reduce the denominator to the 113 observed sessions.
    panel = panel.loc[~panel.date.isin(days[10:17])]
    selected, candidates = portfolio_run.select_from_past(panel, fold, (2,), 1, calendar=days)
    assert selected is None
    assert candidates[0]['observed_days'] == 113
    assert candidates[0]['missing_panel_days'] == 7
    assert candidates[0]['scored_days'] == 120
    assert candidates[0]['unknown_fraction'] == pytest.approx(9 / 120)  # plus two exits after the boundary
    assert candidates[0]['mean_cohort'] == pytest.approx(.01)
    assert candidates[0]['selectable'] is False


def test_missing_days_below_gate_are_reported_without_inventing_cash(monkeypatch):
    days, fold = _setup(monkeypatch)
    panel = _panel(days).loc[lambda p: ~p.date.isin(days[20:23])]
    chosen, details = portfolio_run.select_from_past(panel, fold, (2,), 1, calendar=days)
    assert chosen['strategy'] == 'fixed'
    assert details[0]['unknown_fraction'] == pytest.approx(5 / 120)
    assert details[0]['mean_cohort'] == pytest.approx(.01)
    # A mutation in the later test period cannot affect training selection.
    panel.loc[panel.date.ge(fold.test_start), 'net_ret_2d'] = -100
    assert portfolio_run.select_from_past(panel, fold, (2,), 1, calendar=days)[0] == chosen


def test_blocked_label_with_numeric_return_stays_unknown_in_training(monkeypatch):
    days, fold = _setup(monkeypatch)
    panel = _panel(days)
    panel.loc[:6, 'label_status_2d'] = 'entry_blocked'
    panel.loc[:6, 'entry_executable'] = False
    panel.loc[:6, 'exit_date_2d'] = ''
    # The contradictory numeric returns are not erased into known idle cash.
    chosen, details = portfolio_run.select_from_past(panel, fold, (2,), 1, calendar=days)
    assert chosen is None and details[0]['unknown_fraction'] == pytest.approx(9 / 120)


@pytest.mark.parametrize('kind', ['unsorted', 'duplicate', 'invalid', 'missing_training_date'])
def test_invalid_or_conflicting_calendar_rejected(monkeypatch, kind):
    days, fold = _setup(monkeypatch)
    panel = _panel(days)
    bad = days.copy()
    if kind == 'unsorted':
        bad[0], bad[1] = bad[1], bad[0]
    elif kind == 'duplicate':
        bad.insert(1, bad[0])
    elif kind == 'invalid':
        bad[0] = '2023-1-1'
    else:
        bad.remove(days[20])
    with pytest.raises(ValueError, match='تقویم'):
        portfolio_run.select_from_past(panel, fold, (2,), 1, calendar=bad)


def test_training_calendar_is_required(monkeypatch):
    days, fold = _setup(monkeypatch)
    with pytest.raises(TypeError):
        portfolio_run.select_from_past(_panel(days), fold, (2,), 1)


def test_preceding_observation_uses_latest_date_even_when_rows_are_unsorted():
    prices = pd.DataFrame([
        dict(symbol='الف', date='2023-01-03', value=3),
        dict(symbol='ب', date='2023-01-02', value=2),
        dict(symbol='الف', date='2023-01-01', value=1),
        dict(symbol='الف', date='2023-01-05', value=5),
        dict(symbol='ب', date='2023-01-01', value=1),
    ])
    original = prices.copy(deep=True)
    actual = preceding_price_observations(prices, '2023-01-05').set_index('symbol')
    assert actual.loc['الف', 'date'] == '2023-01-03'
    assert actual.loc['ب', 'date'] == '2023-01-02'
    pd.testing.assert_frame_equal(prices, original)


def test_preceding_observation_rejects_ambiguous_duplicate_history():
    prices = pd.DataFrame([
        dict(symbol='الف', date='2023-01-01', value=1),
        dict(symbol='الف', date='2023-01-01', value=2),
    ])
    with pytest.raises(ValueError, match='تکراری'):
        preceding_price_observations(prices, '2023-01-02')
