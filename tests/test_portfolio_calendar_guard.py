"""Calendar conflicts must stop replay, not create cash returns or hide actions."""

from dataclasses import replace
from decimal import ROUND_FLOOR, Decimal

import pandas as pd
import pytest

from backtest.portfolio import PortfolioConfig, replay_portfolio
from backtest.prepared import prepare_portfolio_prices
from config import settings
from tests.test_label_calendar_contract import prices

pytest_plugins = ['tests.test_flow_research']


def selection(day='2024-01-01'):
    return pd.DataFrame([dict(date=day, symbol='x', rank=0)])


def config(**kwargs):
    return PortfolioConfig(initial_capital_rial=10000, max_positions=1, horizon=1, **kwargs)


@pytest.mark.parametrize('damage', ['flat', 'action', 'extreme'])
def test_replay_rejects_off_calendar_raw_rows_before_financial_result(damage):
    calendar = ['2024-01-01', '2024-01-02', '2024-01-04']
    frame = prices(['2024-01-01', '2024-01-02', '2024-01-03', '2024-01-04'])
    if damage == 'action':
        frame.loc[2:, ['open_raw', 'low_raw', 'high_raw', 'close_raw', 'close_final_raw',
                       'ref_price_raw', 'value']] /= 2
    elif damage == 'extreme':
        frame.loc[2, ['low_raw', 'high_raw']] = [1., 1000.]
    with pytest.raises(ValueError, match='تقویم'):
        replay_portfolio(frame, selection(), calendar, config())


def test_replay_rejects_off_calendar_selection_instead_of_silent_idle_cash():
    calendar = ['2024-01-01', '2024-01-02', '2024-01-04']
    with pytest.raises(ValueError, match='تقویم'):
        replay_portfolio(prices(calendar), selection('2024-01-03'), calendar, config())


@pytest.mark.parametrize('day', ['2023-12-31', '2024-01-05'])
def test_unbounded_replay_never_silently_drops_a_decision_outside_calendar(day):
    calendar = ['2024-01-01', '2024-01-02', '2024-01-04']
    with pytest.raises(ValueError, match='تقویم'):
        replay_portfolio(prices(calendar), selection(day), calendar, config())


@pytest.mark.parametrize('raw_or_prepared', ['raw', 'prepared'])
def test_guard_applies_equally_to_immutable_prepared_prices(raw_or_prepared):
    calendar = ['2024-01-01', '2024-01-02', '2024-01-04']
    frame = prices(['2024-01-01', '2024-01-02', '2024-01-03', '2024-01-04'])
    source = prepare_portfolio_prices(frame) if raw_or_prepared == 'prepared' else frame
    with pytest.raises(ValueError, match='تقویم'):
        replay_portfolio(source, selection(), calendar, config())


def test_a_prepared_snapshot_with_missing_grouped_rows_is_rejected():
    calendar = ['2024-01-01', '2024-01-02', '2024-01-03']
    prepared = prepare_portfolio_prices(prices(calendar))
    with pytest.raises(ValueError, match='ردیف'):
        replay_portfolio(replace(prepared, row_count=prepared.row_count+1), selection(), calendar, config())


@pytest.mark.parametrize('bad_date', [None, '2024-1-02', '2024-01-02T00:00:00'])
def test_preparation_cannot_drop_or_normalize_invalid_date_rows(bad_date):
    frame = prices(['2024-01-01', '2024-01-02', '2024-01-03'])
    frame.loc[1, 'date'] = bad_date
    with pytest.raises(ValueError, match='تاریخ'):
        prepare_portfolio_prices(frame)


@pytest.mark.parametrize('kind', ['unit', 'sized'])
@pytest.mark.parametrize('raw_or_prepared', ['raw', 'prepared'])
def test_dip_training_outcomes_reject_calendar_conflicts_before_labelling(kind, raw_or_prepared):
    from research.dip_outcomes import dip_attempt_outcomes
    from research.sized_dip_outcomes import sized_dip_attempt_outcomes
    from tests.test_dip_outcomes import sample

    frame, selected, days = sample()
    calendar = [day for i, day in enumerate(days) if i != 2]
    source = prepare_portfolio_prices(frame) if raw_or_prepared == 'prepared' else frame
    with pytest.raises(ValueError, match='تقویم'):
        if kind == 'unit':
            dip_attempt_outcomes(source, selected, calendar)
        else:
            sized_dip_attempt_outcomes(source, selected, calendar, budget_rial=50_000)


@pytest.mark.parametrize('source', ['prices', 'selections'])
@pytest.mark.parametrize('invalid', ['2024-1-02', '2024-01-02T00:00:00', None])
def test_noncanonical_input_dates_are_rejected(source, invalid):
    calendar = ['2024-01-01', '2024-01-02', '2024-01-03']
    frame, selected = prices(calendar), selection()
    target = frame if source == 'prices' else selected
    target.loc[0, 'date'] = invalid
    with pytest.raises(ValueError, match='تاریخ'):
        replay_portfolio(frame, selected, calendar, config())


@pytest.mark.parametrize('boundary', ['start', 'end'])
def test_noncanonical_window_boundaries_are_rejected(boundary):
    calendar = ['2024-01-01', '2024-01-02', '2024-01-03']
    with pytest.raises(ValueError, match='تاریخ'):
        replay_portfolio(prices(calendar), selection(), calendar, config(), **{boundary: '2024-1-2'})


def test_warmup_and_rows_after_explicit_end_do_not_change_completed_replay():
    calendar = ['2024-01-01', '2024-01-02', '2024-01-03', '2024-01-05']
    frame = prices(calendar)
    original = replay_portfolio(frame, selection(), calendar, config(), end='2024-01-04')
    extended = pd.concat([prices(['2023-12-31']), frame, prices(['2024-01-04'])], ignore_index=True)
    changed = replay_portfolio(extended, pd.concat([selection(), selection('2024-01-04')]),
                               calendar, config(), end='2024-01-04')
    pd.testing.assert_frame_equal(original.executions, changed.executions)
    pd.testing.assert_frame_equal(original.equity, changed.equity)
    assert original.metrics == changed.metrics


def test_off_calendar_warmup_before_explicit_start_remains_supported():
    calendar = ['2024-01-02', '2024-01-03', '2024-01-04']
    result = replay_portfolio(prices(['2024-01-01', *calendar]), selection(calendar[0]),
                              calendar, config(), start=calendar[0])
    assert result.metrics['status'] == 'complete'
    assert result.metrics['executions'] == 2


def test_conflict_is_rejected_before_corporate_book_mutation(monkeypatch):
    from backtest.cash_dividends import CashDividendBook

    monkeypatch.setattr(CashDividendBook, 'start', lambda *args: pytest.fail('book mutated before validation'))
    calendar = ['2024-01-01', '2024-01-02', '2024-01-04']
    with pytest.raises(ValueError, match='تقویم'):
        replay_portfolio(prices(['2024-01-01', '2024-01-02', '2024-01-03', '2024-01-04']),
                         selection(), calendar, config(), cash_dividends=CashDividendBook([]))


@pytest.mark.parametrize('exit_price', [95, 100, 110])
@pytest.mark.parametrize('stress', [False, True])
def test_closed_cash_ledger_matches_independent_decimal_arithmetic(exit_price, stress):
    """A hand-computable one-lot case: no actions, queue, slippage or capacity cap."""
    calendar = ['2024-01-01', '2024-01-02', '2024-01-03']
    frame = prices(calendar)
    frame['volume'] = 1_000_000
    frame['value'] = 100_000_000
    frame.loc[2, ['open_raw', 'low_raw', 'high_raw', 'close_raw', 'close_final_raw', 'value']] = [
        exit_price, exit_price-1, exit_price+1, exit_price, exit_price, exit_price*1_000_000]
    result = replay_portfolio(frame, selection(), calendar, config(slippage_bps=0, stress=stress))
    capital = Decimal('10000')
    buy, sell = (Decimal(str(settings.FEES[name])) for name in ('buy', 'sell'))
    entry, exit_ = Decimal(101 if stress else 100), Decimal(exit_price-1 if stress else exit_price)
    quantity = (capital / (entry*(1+buy))).to_integral_value(rounding=ROUND_FLOOR)
    cash = capital - quantity*entry*(1+buy) + quantity*exit_*(1-sell)
    fees = quantity*(entry*buy + exit_*sell)
    assert result.metrics['corporate_action_events'] == 0
    assert result.metrics['open_positions'] == 0
    assert result.executions.quantity.tolist() == [float(quantity), float(quantity)]
    assert result.metrics['final_equity_rial'] == pytest.approx(float(cash), abs=1e-8)
    assert result.metrics['fees_rial'] == pytest.approx(float(fees), abs=1e-8)
    assert result.metrics['realized_net_rial'] == pytest.approx(float(cash-capital), abs=1e-8)


def test_row_permutation_preserves_replay_and_inputs():
    calendar = ['2024-01-01', '2024-01-02', '2024-01-03']
    frame, selected = prices(calendar), selection()
    before = frame.copy(deep=True)
    original = replay_portfolio(frame, selected, calendar, config())
    changed = replay_portfolio(frame.iloc[::-1], selected, calendar, config())
    pd.testing.assert_frame_equal(frame, before)
    pd.testing.assert_frame_equal(original.executions, changed.executions)
    pd.testing.assert_frame_equal(original.equity, changed.equity)


@pytest.mark.parametrize('runner', ['expectancy', 'flow_v4'])
def test_research_runner_does_not_prefilter_away_a_calendar_conflict(db, flow_baseline, monkeypatch, runner):
    from features.store import build_feature_store
    from research import expectancy_run, flow_v4_run
    from research.run import run_research

    _, root = flow_baseline
    monkeypatch.setitem(settings.RESEARCH_CONFIG, 'horizons', [1, 2])
    build_feature_store(db)
    source = run_research(db, random_trials=1)
    module = expectancy_run if runner == 'expectancy' else flow_v4_run
    original = module.market_calendar

    def conflicting_calendar(frame):
        days = original(frame)
        return days[days != days[110]]

    monkeypatch.setattr(module, 'market_calendar', conflicting_calendar)
    arguments = dict(source_id=source['experiment_id'], min_train_sessions=104, test_sessions=30, replications=1000)
    with pytest.raises(ValueError, match='تقویم'):
        if runner == 'expectancy':
            module.run_expectancy(db, trials_attempted=100, **arguments)
        else:
            module.run_flow_v4(db, **arguments)
    assert not (root / 'research' / ('latest-expectancy.json' if runner == 'expectancy' else 'latest-flow-v4.json')).exists()
    assert not (root / 'research' / 'holdout-unsealed.json').exists()
