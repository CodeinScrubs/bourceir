"""Explicit, opt-in probes of the owner's external ZIP; not a CI dependency.

Set BRS_TSE_V4_REVIEW_ROOT to the safely extracted archive and run this file
alone. Synthetic bars below are tests only and never enter BRS market storage.
Passing tests reproduce upstream failures; they do not approve that project.
"""

import importlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def upstream(monkeypatch):
    path = Path(os.environ['BRS_TSE_V4_REVIEW_ROOT']).resolve()
    monkeypatch.syspath_prepend(str(path))
    names = ('backtest_models', 'backtester', 'iran_queue_sim', 'deep_models', 'run_tse_pattern_report',
             'portfolio_manager', 'iran_filters', 'meta_labeling', 'interpret_results', 'preflight_validator')
    old = dict(sys.modules)
    modules = {name: importlib.import_module(name) for name in names}
    try:
        yield SimpleNamespace(**modules)
    finally:
        for name in set(sys.modules)-set(old):
            module_path = getattr(sys.modules[name], '__file__', None)
            if module_path and Path(module_path).resolve().is_relative_to(path):
                sys.modules.pop(name)


def bars(n=4):
    return pd.DataFrame(dict(open=100., high=100., low=100., close=100., atr=1., volume=1000.),
                        index=pd.date_range('2024-01-01', periods=n))


def signal():
    return pd.DataFrame([dict(pattern_id='probe', pattern_name_fa='test', category='test',
        direction='bullish', status='broken', detected_index=0)])


def test_terminal_exit_returns_an_unfillable_sell_queue_bar(upstream, record_property):
    queue = np.array([False, True, True])
    exit_bar, delay = upstream.iran_queue_sim.find_fillable_exit_bar(1, 3, queue)
    assert exit_bar == 2 and queue[exit_bar]
    record_property('observed', f'exit_index={exit_bar}; still_sell_locked=True; delay={delay}')


def test_slippage_execution_can_leave_observed_trade_range(upstream, record_property):
    df = bars()
    bt = upstream.backtester.TripleBarrierBacktester(upstream.backtest_models.BacktestConfig(max_holding_bars=1))
    trade = bt.run(df, signal()).iloc[0]
    assert trade.entry_price > df.high.iloc[int(trade.entry_index)]
    assert trade.exit_price < df.low.iloc[int(trade.exit_index)]
    record_property('observed', f'flat_trade=100; entry={trade.entry_price}; exit={trade.exit_price}')


def test_future_dividend_rewrites_previous_loaded_prices(upstream, tmp_path, record_property):
    df = bars(5).reset_index().rename(columns={'index':'date'})
    df['dps'] = [0., 0., 0., 0., 10.]
    df.loc[4, ['open','high','low','close']] = 90.
    full, prefix = tmp_path/'full.csv', tmp_path/'prefix.csv'
    df.to_csv(full, index=False)
    df.iloc[:4].to_csv(prefix, index=False)
    a = upstream.run_tse_pattern_report.load_tse_csv(full)
    b = upstream.run_tse_pattern_report.load_tse_csv(prefix)
    assert a.close.iloc[0] == 90. and b.close.iloc[0] == 100.
    record_property('observed', 'past_close_with_future_DPS=90; prefix_only_close=100')


def test_rl_environment_ignores_supplied_open_prices(upstream, record_property):
    cls = upstream.deep_models.TSETradingEnv
    closes, queues = np.array([100., 105., 110.]), np.zeros(3, dtype=bool)
    a = cls(np.zeros((3,2)), np.array([100.,104.,109.]), closes, queues, queues)
    b = cls(np.zeros((3,2)), np.array([100.,101.,102.]), closes, queues, queues)
    outputs = []
    for env in (a, b):
        values=[]
        for action in (2,0):
            _, reward, done, info = env.step(action)
            values.append((reward, done, info))
        outputs.append(values)
    assert outputs[0] == outputs[1]
    record_property('observed', 'different entry/exit opens produce identical rewards and equity')


def test_portfolio_hides_drawdown_while_a_position_is_open(upstream, record_property):
    df=bars(4)
    df.loc[df.index[2], ['open','high','low','close']] = 50.
    trade=dict(symbol='test',pattern_id='probe',entry_time=df.index[1],exit_time=df.index[3],
        entry_price=100.,exit_price=100.,net_return=0.,stop_loss=90.,barrier_hit='time_exit')
    cfg=upstream.portfolio_manager.PortfolioConfig(initial_capital=10000.,allowed_patterns=None,
        min_adx=0.,max_position_pct=1.,max_risk_per_trade=1.)
    result=upstream.portfolio_manager.PortfolioSimulator(cfg).simulate({'test':df},pd.DataFrame([trade]))
    assert result['summary']['max_drawdown'] == 0.
    assert df.close.iloc[2]/df.close.iloc[1]-1 == -.5
    record_property('observed', 'observed held-price fall=-50%; portfolio reported max_drawdown=0')


def test_absent_tablo_and_index_become_neutral_values(upstream, record_property):
    result=upstream.iran_filters.compute_iran_market_features(bars())
    assert result.iran_buyer_power.eq(1.).all() and result.iran_index_bull.all()
    assert result.iran_real_inflow_ratio.eq(0.).all()
    record_property('observed', 'missing buyer_power=1; inflow=0; missing index_bull=True')


def test_missing_oof_predictions_receive_full_sample_label_mean(upstream, record_property):
    rows=[]
    for i in range(12):
        rows.append(dict(pattern_id='probe',pattern_name_fa='test',entry_time='2024-01-02',exit_time='2024-01-10',
            entry_index=1,exit_index=9,net_return=.1 if i<6 else -.1,label=int(i<6),
            barrier_hit='time_exit',delayed_exit_bars=0))
    frame=pd.DataFrame(rows)
    model=upstream.meta_labeling.MetaLabelingAnalyzer()
    a=model.fit_predict_oof(frame)['trades_with_meta'].meta_prob
    frame.loc[11, 'label']=1
    b=upstream.meta_labeling.MetaLabelingAnalyzer().fit_predict_oof(frame)['trades_with_meta'].meta_prob
    assert a.eq(.5).all() and b.eq(7/12).all()
    record_property('observed', 'no admissible CV fold; claimed OOF probabilities change from .5 to 7/12 with a held-out label')


def test_nonfinite_statistics_can_be_labeled_deploy_ready(upstream, record_property):
    status,_=upstream.interpret_results.classify_candidate(120,np.nan,np.nan,np.nan,np.nan,np.nan)
    assert status == 'DEPLOY_READY'
    record_property('observed', status+' despite all nonfinite performance statistics')


def test_preflight_can_pass_without_any_eligible_symbol(upstream, tmp_path, record_property):
    df=bars(8).reset_index().rename(columns={'index':'date'})
    df.to_csv(tmp_path/'short.csv', index=False)
    report=upstream.preflight_validator.run_preflight_audit(tmp_path,min_bars_per_symbol=120)
    assert report['passed'] and report['summary']['valid_symbols'] == 0
    record_property('observed', 'passed=True with valid_symbols=0')


def test_no_data_argument_implicitly_generates_demo_markets(upstream, record_property):
    frames=upstream.run_tse_pattern_report._collect_symbol_frames(None,None,False)
    assert len(frames)==4 and 'FOOLAD' in frames
    record_property('observed', 'demo=False and no input still creates four synthetic symbol frames')
