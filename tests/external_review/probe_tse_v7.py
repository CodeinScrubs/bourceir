"""Opt-in v7 checks of repaired contracts and downstream financial consumers.

Passing checks describe observed behavior; they do not approve financial claims.
Set BRS_TSE_V7_REVIEW_ROOT to the safely extracted owner-provided archive.
"""

import ast
import hashlib
import importlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from tests.external_review.probe_tse_v6 import bars, signal, simulate, trade


@pytest.fixture
def upstream(monkeypatch):
    path = Path(os.environ['BRS_TSE_V7_REVIEW_ROOT']).resolve()
    monkeypatch.syspath_prepend(str(path))
    names = ('backtest_models','backtester','iran_queue_sim','deep_models','run_tse_pattern_report',
             'portfolio_manager','iran_filters','meta_labeling','interpret_results',
             'preflight_validator','risk_metrics','exhaustive_backtester','long_run_gpu_pipeline')
    old = dict(sys.modules)
    modules = {name:importlib.import_module(name) for name in names}
    try:
        yield SimpleNamespace(**modules,source_root=path)
    finally:
        for name in set(sys.modules)-set(old):
            module_path = getattr(sys.modules[name],'__file__',None)
            if module_path and Path(module_path).resolve().is_relative_to(path):
                sys.modules.pop(name)


@pytest.mark.parametrize('count',[np.nan,np.inf])
def test_nonfinite_trade_count_is_now_rejected(upstream,count,record_property):
    verdict,_ = upstream.interpret_results.classify_candidate(count,.65,2.,-.05,.95,40.)
    assert verdict=='REJECT_FALSE_POSITIVE'
    record_property('verdict','FIX_CONFIRMED: nonfinite trade count rejected')


def test_mtm_repairs_same_close_sizing_entry_day_and_sticky_mark(upstream,record_property):
    a,b = bars(),bars()
    rows = [trade(a,'a',entry=1),trade(b,'b',entry=2)]
    before = simulate(upstream,{'a':a,'b':b},rows,max_position_pct=.5,
                      drawdown_halt_pct=1.,drawdown_throttle_pct=1.)
    changed = a.copy()
    changed.loc[a.index[2],'close']=50.
    changed.loc[a.index[3],'close']=np.nan
    after = simulate(upstream,{'a':changed,'b':b},rows,max_position_pct=.5,
                     drawdown_halt_pct=1.,drawdown_throttle_pct=1.)
    assert before['executed_trades'].set_index('symbol').loc['b','alloc_capital']==5000.
    assert after['executed_trades'].set_index('symbol').loc['b','alloc_capital']==5000.
    curve = after['equity_curve'].set_index('date').equity
    assert curve.loc[a.index[2]]==curve.loc[a.index[3]]==7500.
    entry_drop=bars()
    entry_drop.loc[entry_drop.index[1],'close']=50.
    result=simulate(upstream,{'a':entry_drop},[trade(entry_drop)])
    assert result['summary']['max_drawdown']==-.5
    record_property('verdict','FIX_CONFIRMED: open budget stable at 5000; unknown close retains 7500 NAV; entry-day DD=-50%')


def trapped_trade(upstream):
    frame=bars()
    for i,price in enumerate([100.,100.,95.,90.,85.]):
        frame.iloc[i,frame.columns.get_indexer(['open','high','low','close'])]=price
    frame.loc[frame.index[2]:,'is_sell_queue']=True
    bt=upstream.backtester.TripleBarrierBacktester(upstream.backtest_models.BacktestConfig(slippage_bps=0.))
    return frame,bt,bt.run(frame,signal())


def test_flagged_terminal_position_now_stays_open_in_portfolio(upstream,record_property):
    frame,bt,rows=trapped_trade(upstream)
    summary=bt.summarize_results(rows)
    assert summary['filled_trades']==0 and summary['unfilled_sell_queue']==1
    result=simulate(upstream,{'a':frame},rows.assign(symbol='a').to_dict('records'))
    assert result['summary']['executed_trades']==0 and result['summary']['unliquidated_positions']==1
    assert result['executed_trades'].empty and result['equity_curve'].iloc[-1].open_positions==1
    assert result['summary']['final_equity']==8500.
    record_property('verdict','FIX_CONFIRMED: flagged sell does not close; one open position, marked NAV=8500 (not a cash return)')


def test_mixed_summary_omits_trapped_count_and_meta_still_trains_on_censored_labels(upstream,record_property):
    _,bt,trapped=trapped_trade(upstream)
    closed=trapped.copy()
    closed['barrier_hit']='take_profit'
    closed['net_return'],closed['label']=.1,1
    mixed=pd.concat([closed,trapped],ignore_index=True)
    summary=bt.summarize_results(mixed)
    assert summary['filled_trades']==1 and 'unfilled_sell_queue' not in summary
    training=pd.concat([mixed]*6,ignore_index=True)
    analyzer=upstream.meta_labeling.MetaLabelingAnalyzer()
    result=analyzer.fit_predict_oof(training)
    retained=result['trades_with_meta']
    assert len(retained)==12 and retained.barrier_hit.eq('unfilled_sell_queue').sum()==6
    assert retained.loc[retained.barrier_hit.eq('unfilled_sell_queue'),'label'].eq(0).all()
    assert analyzer.model_ is not None
    record_property('observed','PARTIAL: mixed summary drops trapped count; final meta model fits all 12 rows including 6 censored exits labeled 0')


def test_rl_exact_ledger_repairs_weight_drift_and_fee_arithmetic(upstream,record_property):
    cls=upstream.deep_models.TSETradingEnv
    env=cls(np.zeros((3,2)),np.array([100.,100.,200.]),np.array([100.,200.,400.]),
            np.zeros(3,dtype=bool),np.array([False,False,True]),commission_buy=0.,commission_sell=0.)
    env.step(1)
    _,_,done,info=env.step(1)
    assert done and info['equity']==2.5 and info['cash']==.5 and info['shares']==.005
    assert info['unfilled_sell_queue'] and info['pos']==.8
    flat=cls(np.zeros((3,2)),np.full(3,100.),np.full(3,100.),
             np.zeros(3,dtype=bool),np.zeros(3,dtype=bool))
    flat.step(2)
    _,_,done,info=flat.step(0)
    assert done and info['shares']==0.
    assert info['cash']==pytest.approx((1-.0088)/(1+.003712),abs=1e-14)
    record_property('verdict','FIX_CONFIRMED: drift case NAV=2.5; exact flat buy/sell commission ratio and cash ledger')


def test_ppo_caller_does_not_inspect_the_new_terminal_lock_flag(upstream,record_property):
    tree=ast.parse((upstream.source_root/'deep_models.py').read_text(encoding='utf-8'))
    function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='train_transformer_and_ppo')
    assert not any(isinstance(n,ast.Constant) and n.value=='unfilled_sell_queue' for n in ast.walk(function))
    calls=[n for n in ast.walk(function) if isinstance(n,ast.Assign) and isinstance(n.value,ast.Call)
           and isinstance(n.value.func,ast.Attribute) and n.value.func.attr=='step']
    assert any(isinstance(n.targets[0],ast.Tuple) and n.targets[0].elts[-1].id=='_' for n in calls)
    assert any(isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='backward'
               for n in ast.walk(function))
    record_property('observed','DOWNSTREAM_GAP_STATIC: trainer discards step info before gradient update; terminal lock flag never checked (Torch not executed)')


def test_grid_slippage_clamp_is_fixed_but_locked_exit_remains_numeric(upstream,record_property):
    grid=upstream.exhaustive_backtester.precompute_full_grid
    flat=bars(70)
    flat[['open','high','low','close']]=100.
    panel,_=grid({'a':flat},pd.DataFrame(),trail_mults=(3.5,))
    assert panel.entry_price.eq(100.).all()
    locked=bars(70)
    locked['low']=99.
    locked.loc[locked.index[62]:,['open','high','low','close']]=95.
    locked.loc[locked.index[62]:,'is_sell_queue']=True
    panel,outcomes=grid({'a':locked},pd.DataFrame(),trail_mults=(3.5,))
    returns,exits=outcomes[3.5]
    assert exits[0]==69 and locked.is_sell_queue.iloc[exits[0]] and np.isfinite(returns[0])
    assert 'barrier_hit' not in panel
    record_property('observed','PARTIAL: flat-grid entry clamp fixed; GPU/long-run labels still emit numeric '
                    f'return {returns[0]:.8f} for locked final exit without censoring metadata')


def signature(upstream,df,outcomes):
    tree=ast.parse((upstream.source_root/'long_run_gpu_pipeline.py').read_text(encoding='utf-8'))
    function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='run_long_checkpointed_pipeline')
    def assigns(node,name):
        return isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id==name for t in node.targets)
    first=next(i for i,n in enumerate(function.body) if assigns(n,'h'))
    last=next(i for i,n in enumerate(function.body) if assigns(n,'config_sig'))
    code=compile(ast.Module(body=function.body[first:last+1],type_ignores=[]),'v7-checkpoint-contract','exec')
    scope=dict(df=df,out_by_mult=outcomes,trail_mults=(3.5,4.5),max_k=2,p=43,np=np,hashlib=hashlib,
               BacktestConfig=upstream.backtest_models.BacktestConfig)
    exec(code,scope)
    return scope['config_sig']


def signature_inputs():
    frame=pd.DataFrame(dict(close=[100.,101.,102.],volume=[10.,20.,30.],rsi=[40.,50.,60.],
                            entry_time=pd.date_range('2024-01-01',periods=3)))
    return frame,{3.5:(np.array([.01,.02,.03]),),4.5:(np.array([.02,.03,.04]),)}


def test_checkpoint_byte_hash_fixes_the_old_sum_collision(upstream,record_property):
    frame,outcomes=signature_inputs()
    before=signature(upstream,frame,outcomes)
    frame.loc[:,'close']=[102.,101.,100.]
    outcomes[3.5]=(np.array([.03,.02,.01]),)
    after=signature(upstream,frame,outcomes)
    assert before!=after and 'fees' in after
    record_property('verdict','FIX_CONFIRMED: ordered byte digest and fees replace the v6 sums-only signature')


@pytest.mark.parametrize('change',['feature','calendar','other_trail'])
def test_checkpoint_still_omits_material_inputs(upstream,change,record_property):
    frame,outcomes=signature_inputs()
    before=signature(upstream,frame,outcomes)
    if change=='feature':
        frame.loc[:,'rsi']=[60.,50.,40.]
    elif change=='calendar':
        frame.loc[:,'entry_time']=pd.date_range('2024-02-01',periods=3)
    else:
        outcomes[4.5]=(np.array([-.2,-.3,-.4]),)
    assert signature(upstream,frame,outcomes)==before
    record_property('observed',f'PARTIAL: SHA contract remains identical after changing {change}; not a complete input/code identity')


def test_gross_return_is_fixed_but_recorded_exit_price_still_contains_fees(upstream,record_property):
    tree=ast.parse((upstream.source_root/'long_run_gpu_pipeline.py').read_text(encoding='utf-8'))
    function=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='_build_trades_for_row')
    mapping=next(n for n in ast.walk(function) if isinstance(n,ast.Dict)
                 and any(isinstance(k,ast.Constant) and k.value=='gross_return' for k in n.keys))
    fields={k.value:v for k,v in zip(mapping.keys,mapping.values) if isinstance(k,ast.Constant)}
    cfg=upstream.backtest_models.BacktestConfig()
    net=(1-cfg.commission_sell)/(1+cfg.commission_buy)-1
    scope=dict(sub=pd.DataFrame({'entry_price':[100.]}),sub_rets=np.array([net]),bt_cfg=cfg)
    gross=eval(compile(ast.Expression(fields['gross_return']),'v7-gross','eval'),scope)[0]
    exit_price=eval(compile(ast.Expression(fields['exit_price']),'v7-exit','eval'),scope)[0]
    assert gross==pytest.approx(0.,abs=1e-14) and exit_price!=pytest.approx(100.)
    record_property('observed',f'PARTIAL: gross for flat fee-only trade={gross:.12f}; exit-price field={exit_price:.8f}, expected 100')


def test_later_intraday_exit_cash_is_available_to_same_day_open_entry(upstream,record_property):
    a,b=bars(),bars()
    a['low']=99.
    a.loc[a.index[2],['high','close']]=[111.,110.]
    wanted=signal()
    wanted['stop_loss']=90.
    bt=upstream.backtester.TripleBarrierBacktester(upstream.backtest_models.BacktestConfig(slippage_bps=0.))
    first=bt.run(a,wanted).assign(symbol='a')
    assert first.barrier_hit.iloc[0]=='take_profit' and first.exit_price.iloc[0]==110.
    second=trade(b,'b',entry=2)
    result=simulate(upstream,{'a':a,'b':b},[*first.to_dict('records'),second],
                    drawdown_halt_pct=1.,drawdown_throttle_pct=1.)
    allocated=result['executed_trades'].set_index('symbol').loc['b','alloc_capital']
    assert allocated>10000.
    record_property('observed',f'REMAINING_CAUSAL_GAP: all 10000 allocated to A; sale at intraday 110 funds B at same-day open 100; B budget={allocated}')
