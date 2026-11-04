"""Shared-capital surge research must keep unknowns, dates, cash and source identity."""

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from backtest.portfolio import replay_portfolio
from config import settings
from research.surge_portfolio import (
    ALLOCATION,
    COMPARISON_FAMILY,
    fixed_contract,
    mode_specs,
    portfolio_config,
    ranked_decisions,
    relevant_prices,
    summarize_portfolio,
)
from tests.test_portfolio import config, history, picks


def inputs():
    prices,days = history(160,('a','b','c','d','e','f','g'))
    features = prices[['date','symbol']].copy()
    features['ret_1'] = 0.
    flags = features[['date','symbol']].copy()
    flags['momentum'] = pd.array([True]*len(flags),dtype='boolean')
    for i,symbol in enumerate(('a','b','c','d','e','f','g')):
        prices.loc[prices.symbol.eq(symbol),'value'] = 100000.+i*1000
    return prices,features,flags,days


def test_exact_fixed_family_all_scenarios_and_random_controls_are_declared():
    modes = mode_specs()
    assert len(modes)==len({m['name'] for m in modes})==164
    assert sum(m['hypothesis'] is not None and m['random_seed'] is None for m in modes)==40
    assert COMPARISON_FAMILY==600
    contract = fixed_contract(10000000)
    assert contract['disclosed_trial_lower_bound_after_this_run']==737
    assert contract['historical_project_trial_count'] is None
    assert contract['models_fitted']==0 and not contract['holdout_opened']
    assert not contract['accepted_for_signals'] and contract['probability_of_future_profit'] is None
    for h in (20,60):
        cfg = portfolio_config(10000000,h,True)
        assert cfg.horizon==h-1 and cfg.max_loss_pct==.045 and cfg.participation==.05 and cfg.stress


def test_liquidity_ranking_keeps_candidates_beyond_the_first_five_without_future_information():
    prices,features,flags,days = inputs()
    at = features.date.eq(days[10])
    chosen,coverage = ranked_decisions(prices,features.loc[at],flags.loc[at],days,
        start=days[0],end=days[-1],horizon=20,hypothesis='momentum')
    assert chosen.symbol.tolist()==list('gfedcba') and coverage['candidate_rows']==7
    future = prices.copy()
    future.loc[future.date.gt(days[10]),'value'] *= 100
    altered,_ = ranked_decisions(future,features.loc[at],flags.loc[at],days,
        start=days[0],end=days[-1],horizon=20,hypothesis='momentum')
    pd.testing.assert_frame_equal(chosen,altered)


def test_random_control_matches_date_and_count_but_never_samples_unknown_flags():
    prices,features,flags,days = inputs()
    flags.loc[flags.symbol.eq('a'),'momentum'] = pd.NA
    flags.loc[flags.symbol.isin(['b','c','d','e']),'momentum'] = False
    outputs = []
    for seed in (None,41,97,173):
        chosen,coverage = ranked_decisions(prices,features,flags,days,
            start=days[0],end=days[-1],horizon=60,hypothesis='momentum',random_seed=seed)
        assert 'a' not in set(chosen.symbol) and coverage['unknown_flags']==159
        assert not chosen.duplicated(['date','symbol']).any()
        outputs.append(chosen.groupby('date').size())
    for counts in outputs[1:]:
        pd.testing.assert_series_equal(counts,outputs[0])
    first,_ = ranked_decisions(prices,features,flags,days,start=days[0],end=days[-1],horizon=60,
        hypothesis='momentum',random_seed=41)
    reordered,_ = ranked_decisions(prices.sample(frac=1,random_state=1),features,flags,days,
        start=days[0],end=days[-1],horizon=60,hypothesis='momentum',random_seed=41)
    pd.testing.assert_frame_equal(first,reordered)


def test_tail_decisions_are_excluded_before_nominal_horizon_crosses_boundary():
    prices,features,flags,days = inputs()
    chosen,coverage = ranked_decisions(prices,features,flags,days,
        start=days[0],end=days[-1],horizon=60,hypothesis='momentum')
    assert chosen.date.max()==days[-62]
    assert coverage['tail_excluded_rows']==60*7
    assert coverage['census_rows']==159*7


def test_unknown_current_bar_stays_in_coverage_but_cannot_be_a_candidate_or_control():
    prices,features,flags,days = inputs()
    features.loc[features.symbol.eq('g'),'ret_1'] = np.nan
    for seed in (None,41):
        chosen,coverage = ranked_decisions(prices,features,flags,days,
            start=days[0],end=days[-1],horizon=20,hypothesis='momentum',random_seed=seed)
        assert 'g' not in set(chosen.symbol)
        assert coverage['invalid_current_rows']==159 and coverage['true_flags']==159*7


@pytest.mark.parametrize('change',['holdout','flag_order','plain_bool','date_gap','unregistered_rule','unregistered_seed'])
def test_unregistered_or_incoherent_inputs_fail_closed(change):
    prices,features,flags,days = inputs()
    rule,seed = 'momentum',None
    if change=='holdout':
        prices.loc[0,'date'] = settings.RESEARCH_CONFIG['holdout_start']
    elif change=='flag_order':
        flags = flags.iloc[::-1]
    elif change=='plain_bool':
        flags['momentum'] = True
    elif change=='date_gap':
        days = [days[0],days[2],days[1],*days[3:]]
    elif change=='unregistered_rule':
        rule = 'best-after-looking'
    else:
        seed = 42
    with pytest.raises(ValueError):
        ranked_decisions(prices,features,flags,days,start=days[0],end=days[-1],horizon=20,
            hypothesis=rule,random_seed=seed)


def test_pruning_retains_preceding_reference_and_does_not_change_portfolio():
    prices,days = history(12,('a','b','unused'))
    prices.loc[prices.symbol.eq('a') & prices.date.ge(days[4]),['low_raw','high_raw','open_raw','close_raw','close_final_raw']] *= .5
    prices.loc[prices.symbol.eq('a') & prices.date.gt(days[4]),'ref_price_raw'] = 50.
    prices.loc[prices.symbol.eq('a') & prices.date.ge(days[4]),'value'] *= .5
    selected = picks(days[3:7],('a','b'))
    pruned = relevant_prices(prices,selected,start=days[3],end=days[-1])
    assert set(pruned.symbol)=={'a','b'} and pruned.date.min()==days[2]
    full = replay_portfolio(prices,selected,days,config(),start=days[3],end=days[-1],
        loss_floor_exit=True,entry_allocation_fraction=ALLOCATION)
    small = replay_portfolio(pruned,selected,days,config(),start=days[3],end=days[-1],
        loss_floor_exit=True,entry_allocation_fraction=ALLOCATION)
    for kind in ('equity','executions','decisions'):
        pd.testing.assert_frame_equal(getattr(full,kind),getattr(small,kind))
    assert full.metrics==small.metrics and full.assumptions==small.assumptions


def test_fees_cash_capacity_and_headline_return_are_independently_reconciled():
    prices,days = history(400)
    cfg = config()
    replay = replay_portfolio(prices,picks(days[:300]),days,cfg,loss_floor_exit=True,entry_allocation_fraction=.30)
    audit,rolling = summarize_portfolio(replay,prices,days,cfg)
    assert audit['cash_fees_capacity_reconciled'] and audit['buys']>0
    assert audit['audited_model_total_return']==replay.metrics['total_return']
    assert audit['rolling_12month']['windows']==len(rolling)>0
    assert audit['rolling_12month']['below_30pct_model_windows']>0
    assert audit['rolling_12month']['economic_target_verified'] is False


@pytest.mark.parametrize('change',['cash','fee','capacity','return','calendar'])
def test_rehashed_financial_claim_cannot_evade_independent_arithmetic(change):
    prices,days = history(6)
    cfg = config()
    replay = replay_portfolio(prices,picks(days[:1]),days,cfg,entry_allocation_fraction=.30)
    if change=='cash':
        replay.equity.loc[1,'cash_rial'] += 100
    elif change=='fee':
        replay.executions.loc[0,'fee_rial'] = 0.
    elif change=='capacity':
        prices.loc[prices.date.eq(days[1]),'volume'] = 1.
    elif change=='return':
        replay.metrics['total_return'] = .99
    else:
        replay.equity = replay.equity.iloc[1:]
    with pytest.raises(ValueError):
        summarize_portfolio(replay,prices,days,cfg)


def test_stale_unknown_and_synthetic_action_paths_block_uncertainty_without_deleting_days():
    prices,days = history(8,('a',))
    prices = prices.loc[~(prices.date.eq(days[2]))]
    prices.loc[prices.date.ge(days[4]),['low_raw','high_raw','open_raw','close_raw','close_final_raw','ref_price_raw']] *= .5
    prices.loc[prices.date.ge(days[4]),'value'] *= .5
    cfg = config(horizon=6)
    replay = replay_portfolio(prices,picks(days[:1],('a',)),days,cfg,entry_allocation_fraction=.30)
    audit,_ = summarize_portfolio(replay,prices,days,cfg)
    assert audit['observed_sessions']==8 and audit['stale_mark_days']==1
    assert 'stale_mark_days' in audit['computational_uncertainty_blockers']
    assert 'synthetic_corporate_actions' in audit['computational_uncertainty_blockers']
    assert not audit['economic_profit_verified']
    damaged = replace(replay,equity=replay.equity.iloc[:-1].copy(),metrics=dict(replay.metrics))
    damaged.metrics['total_return'] = None
    damaged.metrics['status'] = 'invalid_data'
    damaged.equity.loc[damaged.equity.index[-1],'equity_rial'] = np.nan
    audit,_ = summarize_portfolio(damaged,prices,days,cfg)
    assert audit['audited_model_total_return'] is None
    assert 'incomplete_or_unknown_curve' in audit['computational_uncertainty_blockers']
