"""Finite denominator, causal tapes and chronological cash checks for the ablation."""

from dataclasses import replace

import pandas as pd
import pytest

from backtest.evidence import audit_daily_replay
from backtest.portfolio import PortfolioConfig, replay_portfolio
from backtest.prepared import prepare_portfolio_prices
from config import settings
from features.daily import compute_daily_features
from research.capacity_ablation import CAPITAL_RIAL, diagnostics, paired_results, selection_tape
from tests.test_portfolio import history


def test_full_denominator_keeps_unknown_pairs_and_rejects_missing_or_duplicate_paths():
    records = [dict(fold=f, horizon=h, scenario=s, arm=a, diagnostics=dict(net_modeled_return=.1))
               for f in range(5) for h in (2, 1) for s in ('normal','stress') for a in ('control','filtered')]
    records[0]['diagnostics']['net_modeled_return'] = None
    result = paired_results(records)
    assert result['primary_complete_pairs'] == 9 and result['primary_unknown_pairs'] == 1
    assert result['pairs'][0]['paired_difference'] is None
    assert result['successful_subset_average'] is None and not result['prospective_candidate']
    for altered in (records[:-1], [*records[:-1], records[0]]):
        with pytest.raises(ValueError, match='forty'):
            paired_results(altered)


def test_tapes_preserve_rank_native_identity_and_horizon_censoring():
    rows = [dict(fold=0, decision_date='2024-01-01', planned_primary_exit='2024-01-04',
                 control_top5=['b','a'], filtered_top5=['a']),
            dict(fold=0, decision_date='2024-01-02', planned_primary_exit=None,
                 control_top5=['a'], filtered_top5=[])]
    primary = selection_tape(rows, 0, 2, dict(a='1',b='2'))
    secondary = selection_tape(rows, 0, 1, dict(a='1',b='2'))
    assert primary['control'].symbol.tolist() == ['2','1']
    assert primary['filtered'].symbol.tolist() == ['1']
    assert len(secondary['control']) == 3


def test_five_session_shared_cash_entry_before_exit_and_next_session_reuse():
    prices, days = history(5, ('a','b'))
    # Flat VWAP and a binding 5% participation cap. All synthetic candles
    # remain in tests only.
    prices[['volume','value']] = [1e9,1e11]
    prices[['low_raw','high_raw']] = [99.,101.]
    selections = pd.DataFrame(dict(date=[days[0], days[1], days[2], days[3]],
                                   symbol=['a','b','b','b'], rank=[0]*4))
    cfg = PortfolioConfig(initial_capital_rial=CAPITAL_RIAL, horizon=2, max_positions=1,
                          participation=.05, slippage_bps=0, max_loss_pct=.1)
    replay = replay_portfolio(prices, selections, days, cfg, entry_allocation_fraction=1.)
    events = replay.executions.to_dict('records')
    assert [(e['date'],e['side'],e['symbol']) for e in events] == [
        (days[1],'buy','a'), (days[3],'sell','a'), (days[4],'buy','b')]
    buy_fee, sell_fee = settings.FEES['buy'], settings.FEES['sell']
    quantity = 50_000_000
    remaining = CAPITAL_RIAL-quantity*100*(1+buy_fee)
    assert events[0]['quantity'] == quantity
    assert replay.equity.cash_rial.iloc[1] == pytest.approx(remaining)
    assert replay.equity.cash_rial.iloc[2] == pytest.approx(remaining)
    assert replay.equity.cash_rial.iloc[3] == pytest.approx(remaining+quantity*100*(1-sell_fee))
    prepared = prepare_portfolio_prices(prices)
    audit = audit_daily_replay(replay, prepared, selections, days)
    summary = diagnostics(replay, audit, prepared, days)
    assert summary['full_model_path'] and summary['terminal_modeled_liquidation_cash_rial'] is None
    assert summary['same_fill_path_gross_return_before_explicit_fees'] is None
    missing = prices.loc[~((prices.symbol=='a') & (prices.date==days[2]))]
    invalid = replay_portfolio(missing, selections, days, replace(cfg), entry_allocation_fraction=1.)
    checked = audit_daily_replay(invalid, missing, selections, days)
    stats = diagnostics(invalid, checked, prepare_portfolio_prices(missing), days)
    assert not stats['full_model_path'] and stats['net_modeled_return'] is None
    assert stats['mean_capital_utilization'] is None


def test_momentum_is_twenty_instrument_rows_and_does_not_skip_invalid_rows():
    prices, _ = history(23, ('a',))
    # Remove a calendar date. Instrument-row horizon extends without inventing
    # a zero return. A present but invalid reference poisons the rolling score.
    sparse = prices.drop(index=5).copy()
    score = compute_daily_features(sparse)
    assert pd.isna(score.ret_20d.iloc[19]) and score.ret_20d.iloc[20] == 0
    sparse.loc[sparse.index[-2], 'ref_price_raw'] = 0
    assert pd.isna(compute_daily_features(sparse).ret_20d.iloc[-1])


def test_five_slots_share_cash_and_all_sale_proceeds_wait_until_next_session():
    names = tuple('abcde')
    prices, days = history(5, names)
    prices[['volume','value']] = [1e9,1e11]
    selections = pd.DataFrame([dict(date=day,symbol=name,rank=rank)
        for day in days[:4] for rank,name in enumerate(names)])
    cfg = PortfolioConfig(initial_capital_rial=CAPITAL_RIAL, horizon=2, max_positions=5,
                          participation=.05, slippage_bps=0, max_loss_pct=.1)
    replay = replay_portfolio(prices, selections, days, cfg, entry_allocation_fraction=1.)
    buy_fee, sell_fee = settings.FEES['buy'], settings.FEES['sell']
    quantity = int((CAPITAL_RIAL/5)//(100*(1+buy_fee)))
    buy_cost = 5*quantity*100*(1+buy_fee)
    sale_net = 5*quantity*100*(1-sell_fee)
    cash_after_entry = CAPITAL_RIAL-buy_cost
    cash_after_sales = cash_after_entry+sale_net
    next_quantity = int((cash_after_sales/5)//(100*(1+buy_fee)))
    assert replay.equity.positions.tolist() == [0,5,5,0,5]
    assert replay.equity.cash_rial.tolist() == pytest.approx([
        CAPITAL_RIAL,cash_after_entry,cash_after_entry,cash_after_sales,
        cash_after_sales-5*next_quantity*100*(1+buy_fee)])
    assert replay.executions.groupby(['date','side']).size().to_dict() == {
        (days[1],'buy'):5,(days[3],'sell'):5,(days[4],'buy'):5}
