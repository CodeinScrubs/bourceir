"""Opt-in v6 claim checks: passing probes describe behavior, not profitability.

Use BRS_TSE_V6_REVIEW_ROOT for the owner's safely extracted archive. This file
is deliberately outside pytest's test_*.py collection and uses temporary inputs.
"""

import ast
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
    path = Path(os.environ['BRS_TSE_V6_REVIEW_ROOT']).resolve()
    monkeypatch.syspath_prepend(str(path))
    names = ('backtest_models', 'backtester', 'iran_queue_sim', 'deep_models',
             'run_tse_pattern_report', 'portfolio_manager', 'iran_filters',
             'meta_labeling', 'interpret_results', 'preflight_validator', 'risk_metrics')
    old = dict(sys.modules)
    modules = {name: importlib.import_module(name) for name in names}
    try:
        yield SimpleNamespace(**modules, source_root=path)
    finally:
        for name in set(sys.modules)-set(old):
            module_path = getattr(sys.modules[name], '__file__', None)
            if module_path and Path(module_path).resolve().is_relative_to(path):
                sys.modules.pop(name)


def bars(n=5):
    return pd.DataFrame(dict(open=100., high=101., low=49., close=100., atr=1., volume=10000.,
                             is_buy_queue=False, is_sell_queue=False),
                        index=pd.date_range('2024-01-01', periods=n))


def signal():
    return pd.DataFrame([dict(pattern_id='probe', pattern_name_fa='test', category='test',
        direction='bullish', status='broken', detected_index=0, stop_loss=97., target_price=110.)])


def trade(frame, symbol='a', entry=1, exit=4, net_return=0.):
    return dict(symbol=symbol, pattern_id='probe', entry_time=frame.index[entry], exit_time=frame.index[exit],
                entry_price=100., stop_loss=80., net_return=net_return, barrier_hit='time_exit')


def simulate(upstream, frames, rows, **kwargs):
    config = dict(initial_capital=10000., allowed_patterns=None, min_adx=0.,
                  max_position_pct=1., max_risk_per_trade=1.)
    config.update(kwargs)
    return upstream.portfolio_manager.PortfolioSimulator(
        upstream.portfolio_manager.PortfolioConfig(**config)).simulate(frames, pd.DataFrame(rows))


def test_nonfinite_performance_is_now_rejected(upstream, record_property):
    status, _ = upstream.interpret_results.classify_candidate(120, np.nan, np.nan, np.nan, np.nan, np.nan)
    assert status == 'REJECT_FALSE_POSITIVE'
    record_property('verdict', 'FIX_CONFIRMED: nonfinite performance rejected')


@pytest.mark.parametrize('count', [np.nan, np.inf])
def test_nonfinite_trade_count_can_still_pass_deployment_gate(upstream, count, record_property):
    status, _ = upstream.interpret_results.classify_candidate(count, .65, 2., -.05, .95, 40.)
    assert status == 'DEPLOY_READY'
    record_property('observed', f'REMAINING: trades={count}; finite favorable metrics => {status}')


def test_empty_scope_and_missing_input_fixes(upstream, tmp_path, record_property):
    df = bars(8).reset_index().rename(columns={'index':'date'})
    df.to_csv(tmp_path/'short.csv', index=False)
    report = upstream.preflight_validator.run_preflight_audit(tmp_path, min_bars_per_symbol=120)
    assert not report['passed'] and report['summary']['valid_symbols'] == 0
    with pytest.raises(ValueError, match='No symbol CSVs'):
        upstream.run_tse_pattern_report._collect_symbol_frames(None, None, False)
    record_property('verdict', 'FIX_CONFIRMED: zero eligible symbols and missing non-demo input fail')


def test_slippage_is_clipped_for_valid_flat_bars(upstream, record_property):
    frame = bars()
    frame[['open','high','low','close']] = 100.
    row = upstream.backtester.TripleBarrierBacktester(
        upstream.backtest_models.BacktestConfig(max_holding_bars=1, slippage_bps=50.)).run(frame, signal()).iloc[0]
    assert row.entry_price == row.exit_price == 100.
    record_property('verdict', 'FIX_CONFIRMED: valid flat-bar fills no longer leave OHLC')


def test_terminal_unfilled_exit_flag_still_becomes_realized_cash(upstream, record_property):
    frame = bars()
    for i, price in enumerate([100.,100.,95.,90.,85.]):
        frame.iloc[i, frame.columns.get_indexer(['open','high','low','close'])] = price
    frame.loc[frame.index[2]:, 'is_sell_queue'] = True
    bt = upstream.backtester.TripleBarrierBacktester(upstream.backtest_models.BacktestConfig(slippage_bps=0.))
    rows = bt.run(frame, signal())
    assert rows.barrier_hit.tolist() == ['unfilled_sell_queue']
    summary = bt.summarize_results(rows)
    result = simulate(upstream, {'a':frame}, rows.assign(symbol='a').to_dict('records'))
    assert summary['filled_trades'] == 1 and result['summary']['executed_trades'] == 1
    assert result['executed_trades'].barrier_hit.tolist() == ['unfilled_sell_queue']
    assert result['equity_curve'].iloc[-1].open_positions == 0
    assert np.isfinite(rows.net_return.iloc[0]) and np.isfinite(result['summary']['final_equity'])
    record_property('observed', 'PARTIAL: unfilled_sell_queue flag added; summary counts filled; '
                    f'portfolio books exit cash={result["summary"]["final_equity"]}; open_positions=0')


def test_gpu_rule_input_generator_keeps_numeric_trapped_exit_returns(upstream, record_property):
    miner = importlib.import_module('exhaustive_backtester')
    frame = bars(70)
    frame['low'] = 99.
    frame.loc[frame.index[62]:,['open','high','low','close']] = 95.
    frame.loc[frame.index[62]:,'is_sell_queue'] = True
    panel, outcomes = miner.precompute_full_grid({'a':frame},pd.DataFrame(),trail_mults=(3.5,))
    returns, exits = outcomes[3.5]
    assert int(exits[0]) == 69 and frame.is_sell_queue.iloc[exits[0]]
    assert np.isfinite(returns[0]) and len(panel) == len(returns)
    record_property('observed', 'REMAINING: GPU/long-run precompute_full_grid still emits finite return '
                    f'{returns[0]:.8f} at locked terminal exit index={exits[0]}, without the new censoring flag')


def test_daily_mtm_now_captures_an_intermediate_dip(upstream, record_property):
    frame = bars()
    frame.loc[frame.index[2], 'close'] = 50.
    result = simulate(upstream, {'a':frame}, [trade(frame)])
    assert result['summary']['max_drawdown'] == -.5
    record_property('verdict', 'FIX_CONFIRMED: valid intermediate held-price fall=-50%; max_drawdown=-50%')


def test_entry_day_drawdown_is_still_invisible(upstream, record_property):
    frame = bars()
    frame.loc[frame.index[1], 'close'] = 50.
    result = simulate(upstream, {'a':frame}, [trade(frame)])
    entry_mark = result['equity_curve'].iloc[0]
    assert entry_mark.open_positions == 1 and entry_mark.equity == 10000.
    assert result['summary']['max_drawdown'] == 0.
    record_property('observed', 'REMAINING: bought at 100, same-day close=50; entry NAV=10000; DD=0')


def test_missing_mark_reverts_to_original_principal(upstream, record_property):
    frame = bars()
    frame.loc[frame.index[2], 'close'] = 50.
    frame.loc[frame.index[3], 'close'] = np.nan
    result = simulate(upstream, {'a':frame}, [trade(frame)])
    values = result['equity_curve'].set_index('date').equity
    assert values.loc[frame.index[2]] == 5000. and values.loc[frame.index[3]] == 10000.
    record_property('observed', 'REMAINING: held mark 50 => NAV=5000; next unknown mark => original principal NAV=10000')


def test_same_day_close_changes_open_entry_allocation(upstream, record_property):
    a, b = bars(), bars()
    rows = [trade(a, 'a', entry=1), trade(b, 'b', entry=2)]
    base = simulate(upstream, {'a':a,'b':b}, rows, max_position_pct=.5,
                    drawdown_halt_pct=1., drawdown_throttle_pct=1.)
    later_close = a.copy()
    later_close.loc[a.index[2], 'close'] = 50.
    altered = simulate(upstream, {'a':later_close,'b':b}, rows, max_position_pct=.5,
                       drawdown_halt_pct=1., drawdown_throttle_pct=1.)
    before = base['executed_trades'].set_index('symbol').loc['b','alloc_capital']
    after = altered['executed_trades'].set_index('symbol').loc['b','alloc_capital']
    assert before == 5000. and after == 3750.
    record_property('observed', 'REGRESSION: same opens/history; alter held-symbol close on B entry day; '
                    'B next-open allocation=5000 versus 3750')


def test_open_prices_now_change_rl_entry_result(upstream, record_property):
    values = []
    for open_price in [100.,110.]:
        env = upstream.deep_models.TSETradingEnv(np.zeros((3,2)), np.array([100.,open_price,110.]),
            np.array([100.,110.,110.]), np.zeros(3,dtype=bool), np.zeros(3,dtype=bool))
        _, _, _, info = env.step(2)
        values.append(info['equity'])
    assert values[0] > values[1]
    record_property('verdict', 'FIX_CONFIRMED: next-open prices now affect RL entry equity')


def test_rl_half_position_drift_does_not_preserve_shares_when_sell_queue_locks(upstream, record_property):
    env = upstream.deep_models.TSETradingEnv(np.zeros((3,2)), np.array([100.,100.,200.]),
        np.array([100.,200.,400.]), np.zeros(3,dtype=bool), np.array([False,False,True]),
        commission_buy=0., commission_sell=0.)
    env.step(1)
    _, _, done, info = env.step(1)
    # First buy: cash=.5, quantity=.5/100. No sale is possible at the second open.
    cash_and_shares_nav = .5 + (.5/100.)*400.
    assert done and info['pos'] == .5 and info['equity'] == 2.25 and cash_and_shares_nav == 2.5
    record_property('observed', 'REMAINING: 50%-entry then locked sell; cash/share NAV=2.5; RL NAV=2.25')


def test_terminal_rl_long_is_not_liquidated_or_censored(upstream, record_property):
    env = upstream.deep_models.TSETradingEnv(np.zeros((2,2)), np.full(2,100.), np.full(2,100.),
        np.zeros(2,dtype=bool), np.array([False,True]))
    _, _, done, info = env.step(2)
    assert done and info['pos'] == 1.
    assert set(info) == {'equity','drawdown','pos'}
    record_property('observed', 'REMAINING: episode done=True with long position; no terminal liquidity/censoring flag')


def test_oof_fallback_no_longer_leaks_labels_but_is_still_a_fabricated_probability(upstream, record_property):
    rows = [dict(pattern_id='probe', pattern_name_fa='test', entry_time='2024-01-02', exit_time='2024-01-10',
        entry_index=1, exit_index=9, net_return=.1 if i<6 else -.1, label=int(i<6),
        barrier_hit='time_exit',delayed_exit_bars=0) for i in range(12)]
    frame = pd.DataFrame(rows)
    cfg = upstream.backtest_models.BacktestConfig(meta_threshold=.5)
    first = upstream.meta_labeling.MetaLabelingAnalyzer(cfg).fit_predict_oof(frame)['trades_with_meta']
    frame.loc[11,'label'] = 1
    second = upstream.meta_labeling.MetaLabelingAnalyzer(cfg).fit_predict_oof(frame)['trades_with_meta']
    assert first.meta_prob.eq(.5).all() and second.meta_prob.eq(.5).all()
    assert first.meta_take.all()
    record_property('observed', 'PARTIAL: no global-label fallback leakage; absent OOF=.5, meta_threshold=.5 takes all')


def test_missing_index_is_false_but_remaining_tablo_defaults_are_not_unknown(upstream, record_property):
    frame = upstream.iran_filters.compute_iran_market_features(bars())
    assert not frame.iran_index_bull.any() and frame.iran_index_ret20.isna().all()
    assert frame.iran_buyer_power.eq(1.).all() and frame.iran_real_inflow_ratio.eq(0.).all()
    record_property('observed', 'PARTIAL: missing index no longer bullish; unknown buyer_power=1 and inflow=0 remain')


def test_future_dps_still_rewrites_the_past(upstream, tmp_path, record_property):
    frame = bars().reset_index().rename(columns={'index':'date'})
    frame[['open','high','low','close']] = 100.
    frame['dps'] = [0.,0.,0.,0.,10.]
    frame.loc[4,['open','high','low','close']] = 90.
    frame.to_csv(tmp_path/'full.csv',index=False)
    frame.iloc[:4].to_csv(tmp_path/'prefix.csv',index=False)
    a = upstream.run_tse_pattern_report.load_tse_csv(tmp_path/'full.csv')
    b = upstream.run_tse_pattern_report.load_tse_csv(tmp_path/'prefix.csv')
    assert a.close.iloc[0] == 90. and b.close.iloc[0] == 100.
    record_property('observed', 'INTENTIONAL_BUT_NOT_RAW_CAUSAL: past close with future DPS=90; prefix alone=100')


def test_checkpoint_sum_signature_collides_on_different_market_paths(upstream, record_property):
    tree = ast.parse((upstream.source_root/'long_run_gpu_pipeline.py').read_text(encoding='utf-8'))
    nodes = [node for node in ast.walk(tree) if isinstance(node,ast.Assign)
             and any(isinstance(t,ast.Name) and t.id in {'data_sig','config_sig'} for t in node.targets)]
    assert len(nodes) == 2
    executable = compile(ast.Module(body=sorted(nodes,key=lambda n:n.lineno),type_ignores=[]),'upstream-signature','exec')
    signatures = []
    for closes, returns in [([100.,101.,102.],[.01,.02,.03]),([102.,101.,100.],[.03,.02,.01])]:
        scope=dict(df=pd.DataFrame({'close':closes}),out_by_mult={3.5:(np.array(returns),)},
                   trail_mults=[3.5],max_k=2,p=43)
        exec(executable,scope)
        signatures.append(scope['config_sig'])
    assert signatures[0] == signatures[1]
    record_property('observed', 'PARTIAL: sum-based data_sig collides for different ordered prices/returns; no code/fee hash')


def test_optional_dsr_dispersion_is_unused_by_all_workflow_callers(upstream, record_property):
    callers = []
    for path in upstream.source_root.glob('*.py'):
        if path.name.startswith('test_'):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='deflated_sharpe_ratio':
                callers.append((path.name,node.lineno))
                assert len(node.args)<4 and all(k.arg!='trials_sr_std' for k in node.keywords)
    assert callers
    r = np.array([-.01,.02,.03,-.02,.015,.04,-.005,.01])
    a = upstream.risk_metrics.deflated_sharpe_ratio(r,n_trials=20)
    b = upstream.risk_metrics.deflated_sharpe_ratio(r,n_trials=20,trials_sr_std=np.nan)
    assert a == b
    record_property('observed', f'PARTIAL: optional dispersion added, but all {len(callers)} workflow calls omit it; invalid NaN silently falls back')
