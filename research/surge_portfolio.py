"""Fixed surge hypotheses with shared cash; development diagnostics, never signals."""

from dataclasses import asdict

import numpy as np
import pandas as pd

from backtest.portfolio import PortfolioConfig
from config import settings
from core.text import normalize_symbol
from core.timeutils import is_canonical_iso_date
from research.surge_patterns import HORIZONS, HYPOTHESES

VERSION = 'surge-shared-capital-v1'
SEEDS = (41, 97, 173)
BLOCKS = (20, 60, 120)
ALLOCATION = .30
LOSS_FRACTION = .045
PRIMARY_CONFIGURATIONS = len(HYPOTHESES)*len(HORIZONS)
WITHIN_RUN_CONFIGURATIONS = PRIMARY_CONFIGURATIONS+len(HORIZONS)
COMPARISON_FAMILY = PRIMARY_CONFIGURATIONS*2*(2+len(SEEDS))*len(BLOCKS)


def portfolio_config(capital_rial, horizon, stress=False):
    if horizon not in HORIZONS or type(horizon) is not int:
        raise ValueError('Only the two registered decision-to-exit horizons are allowed')
    return PortfolioConfig(initial_capital_rial=capital_rial, horizon=horizon-1, max_positions=5,
        participation=.05, slippage_bps=settings.BACKTEST_CONFIG['default_slippage_bps'],
        max_loss_pct=LOSS_FRACTION, stress=stress)


def mode_specs():
    result = []
    for horizon in HORIZONS:
        for stress in (False, True):
            scenario = 'stress' if stress else 'normal'
            result.append(dict(name=f'liquidity-h{horizon}-{scenario}', hypothesis=None, horizon=horizon,
                               stress=stress, random_seed=None))
            for hypothesis in HYPOTHESES:
                for seed in (None, *SEEDS):
                    control = '' if seed is None else f'-random{seed}'
                    result.append(dict(name=f'{hypothesis.name}-h{horizon}{control}-{scenario}',
                        hypothesis=hypothesis.name, horizon=horizon, stress=stress, random_seed=seed))
    return result


def _keys(frame):
    if (not isinstance(frame, pd.DataFrame) or frame.columns.duplicated().any()
            or not {'date','symbol'} <= set(frame) or frame.duplicated(['date','symbol']).any()
            or any(not is_canonical_iso_date(d) or d >= settings.RESEARCH_CONFIG['holdout_start']
                   for d in frame.date.unique())
            or not frame.symbol.map(lambda s: isinstance(s,str) and bool(s) and normalize_symbol(s)==s).all()):
        raise ValueError('Unique normalized development-only date/symbol keys are required')


def ranked_decisions(prices, features, flags, calendar, *, start, end, horizon, hypothesis=None, random_seed=None):
    """Rank with decision-day value only. Unknown flags are never false controls.

    A random control has the same candidate count on the same decision date,
    sampled from known flags with a valid current bar, then the SAME liquidity
    ranking. No future fill/return or winner label is an input. No top-five
    truncation here: the replay accounts for already-held issuers and shared slots.
    """
    for frame in (prices, features, flags):
        _keys(frame)
    if (horizon not in HORIZONS or type(horizon) is not int
            or hypothesis not in (None, *(h.name for h in HYPOTHESES))
            or random_seed not in (None, *SEEDS) or isinstance(random_seed,bool)
            or (hypothesis is None and random_seed is not None)):
        raise ValueError('Unregistered hypothesis, horizon or control seed')
    days = list(calendar)
    if (not days or days != sorted(set(days)) or any(not is_canonical_iso_date(d)
            or d >= settings.RESEARCH_CONFIG['holdout_start'] for d in days)
            or not is_canonical_iso_date(start) or not is_canonical_iso_date(end)
            or not days[0] <= start < end <= settings.RESEARCH_CONFIG['holdout_start']
            or start not in days or end not in days):
        raise ValueError('A registered contiguous development calendar interval is required')
    if (not features[['date','symbol']].reset_index(drop=True).equals(flags[['date','symbol']].reset_index(drop=True))
            or not set(features.date) <= set(days)):
        raise ValueError('Frozen flags and features must retain the same entire census keys')
    f = features[['date','symbol','ret_1']].copy()
    f['flag'] = True if hypothesis is None else flags[hypothesis]
    if hypothesis is not None and str(f.flag.dtype) != 'boolean':
        raise ValueError('Three-state boolean hypothesis flags are required')
    f = f.merge(prices[['date','symbol','value']],on=['date','symbol'],how='left',validate='one_to_one')
    f = f.loc[f.date.ge(start) & f.date.lt(end)].copy()
    positions = {d:i for i,d in enumerate(days)}
    f['tail_excluded'] = f.date.map(lambda d: positions[d]+horizon >= positions[end])
    current_known = np.isfinite(f.ret_1) & np.isfinite(f.value) & f.value.gt(0)
    coverage = dict(census_rows=len(f), true_flags=int(f.flag.eq(True).sum()),
        false_flags=int(f.flag.eq(False).sum()), unknown_flags=int(f.flag.isna().sum()),
        invalid_current_rows=int((~current_known).sum()), tail_excluded_rows=int(f.tail_excluded.sum()),
        candidate_rows=0, decision_dates=0)
    f = f.loc[current_known & ~f.tail_excluded]
    selected = []
    number = None if hypothesis is None else [h.name for h in HYPOTHESES].index(hypothesis)
    for day, group in f.groupby('date',sort=True):
        chosen = group.loc[group.flag.fillna(False)].copy()
        if random_seed is not None:
            pool = group.loc[group.flag.notna()].sort_values('symbol')
            rng = np.random.default_rng(np.random.SeedSequence([random_seed,number,int(day.replace('-',''))]))
            chosen = pool.iloc[rng.choice(len(pool),size=len(chosen),replace=False)].copy()
        chosen = chosen.sort_values(['value','symbol'],ascending=[False,True])
        if len(chosen):
            chosen['rank'] = np.arange(len(chosen),dtype=int)
            selected.append(chosen[['date','symbol','rank']])
    picks = pd.concat(selected,ignore_index=True) if selected else pd.DataFrame({
        'date':pd.Series(dtype=str),'symbol':pd.Series(dtype=str),'rank':pd.Series(dtype='int64')})
    coverage.update(candidate_rows=len(picks),decision_dates=int(picks.date.nunique()))
    return picks, coverage


def relevant_prices(prices, picks, *, start, end):
    """Computational pruning only; retain each issuer's preceding action reference."""
    names = set(picks.symbol)
    own = prices.loc[prices.symbol.isin(names) & prices.date.lt(end)]
    prior = own.loc[own.date.lt(start)].sort_values(['symbol','date']).groupby('symbol').tail(1)
    return pd.concat([prior,own.loc[own.date.ge(start)]],ignore_index=True)


def _rolling_year(curve):
    """Calendar 12-month model endpoints; unknown/stale paths remain separately counted."""
    dates = pd.to_datetime(curve.date)
    rows = []
    stale = curve.stale_positions.gt(0) | curve.stale_fractional_exposures.gt(0)
    for i, date in enumerate(dates):
        target = date-pd.DateOffset(years=1)
        if target < dates.iloc[0]:
            continue
        j = int(dates.searchsorted(target,side='right'))-1
        values = curve.equity_rial.iloc[j:i+1]
        known = np.isfinite(values).all() and values.iloc[0] > 0
        base_lag = int((target-dates.iloc[j]).days)
        stale_days = int(stale.iloc[j:i+1].sum())
        model_return = float(values.iloc[-1]/values.iloc[0]-1) if known and base_lag <= 10 else None
        rows.append(dict(date=curve.date.iloc[i],base_date=curve.date.iloc[j],base_lag_days=base_lag,
            model_return=model_return, stale_mark_days=stale_days,
            fresh_model_path=bool(model_return is not None and not stale_days),
            nominal_30pct_target_met_in_model=None if model_return is None else model_return >= .30,
            economic_target_verified=False))
    return pd.DataFrame(rows,columns=['date','base_date','base_lag_days','model_return','stale_mark_days',
        'fresh_model_path','nominal_30pct_target_met_in_model','economic_target_verified'])


def summarize_portfolio(result, prices, calendar, config):
    """Independently reconcile modeled cash, fees, daily capacity and headline return."""
    curve, executions, decisions = result.equity, result.executions, result.decisions
    days = list(calendar)
    if curve.date.tolist() != days[:len(curve)] or curve.empty:
        raise ValueError('Replay curve must retain the exact session prefix, including unknown days')
    movement = pd.Series(0.,index=curve.date)
    if not executions.empty:
        if set(executions.side)-{'buy','sell'} or not executions.quantity.gt(0).all():
            raise ValueError('Invalid execution side or quantity')
        trades = executions.merge(prices[['date','symbol','low_raw','high_raw','volume','value']],
            on=['date','symbol'],how='left',validate='many_to_one')
        if (not trades.price_rial.between(trades.low_raw,trades.high_raw).all()
                or not np.isfinite(trades[['quantity','price_rial','fee_rial','volume','value']]).all().all()):
            raise ValueError('Execution without finite observed range/capacity')
        expected_fees = trades.quantity*trades.price_rial*trades.side.map(settings.FEES)
        if not np.allclose(expected_fees,trades.fee_rial,rtol=1e-12,atol=1e-5):
            raise ValueError('Execution fees differ from the registered schedule')
        trades['notional'] = trades.quantity*trades.price_rial
        cap = trades.groupby(['date','symbol']).agg(quantity=('quantity','sum'),notional=('notional','sum'),
            volume=('volume','first'),value=('value','first'))
        if ((cap.quantity > cap.volume*config.participation+1e-6)
                | (cap.notional > cap.value*config.participation+1e-4)).any():
            raise ValueError('Combined buy/sell participation exceeded the daily observed capacity')
        trades['cash_delta'] = np.where(trades.side.eq('buy'),-trades.notional,trades.notional)-trades.fee_rial
        movement = trades.groupby('date').cash_delta.sum().reindex(curve.date,fill_value=0)
    cash = config.initial_capital_rial+movement.cumsum().to_numpy()
    if not np.allclose(cash,curve.cash_rial,rtol=1e-11,atol=1e-3) or curve.cash_rial.lt(-1e-3).any():
        raise ValueError('Daily shared cash does not reconcile with the execution ledger')
    fees = 0. if executions.empty else float(executions.fee_rial.sum())
    if not np.isclose(fees,result.metrics['fees_rial'],rtol=1e-11,atol=1e-3):
        raise ValueError('Headline total fees differ from actual modeled executions')
    complete = (result.metrics['status']=='complete' and curve.date.tolist()==days
                and np.isfinite(curve.equity_rial).all())
    total = float(curve.equity_rial.iloc[-1]/config.initial_capital_rial-1) if complete else None
    if (total is None) != (result.metrics['total_return'] is None) or (total is not None
            and not np.isclose(total,result.metrics['total_return'],rtol=1e-12,atol=1e-12)):
        raise ValueError('Headline return differs from the complete calendar curve')
    stale = int((curve.stale_positions.gt(0) | curve.stale_fractional_exposures.gt(0)).sum())
    blockers = []
    for condition, label in ((not complete,'incomplete_or_unknown_curve'),(stale>0,'stale_mark_days'),
            (result.metrics['corporate_action_events']>0,'synthetic_corporate_actions'),
            (result.metrics['open_positions']>0,'unliquidated_positions'),
            (result.metrics['noncash_receivable_rial']>0,'noncash_fractional_claims')):
        if condition:
            blockers.append(label)
    rolling = _rolling_year(curve)
    returns = pd.to_numeric(rolling.model_return,errors='coerce')
    loss = max(0.,1-float(curve.equity_rial.min())/config.initial_capital_rial) if complete else None
    summary = dict(calendar_complete=bool(complete),expected_sessions=len(days),observed_sessions=len(curve),
        audited_model_total_return=total,stale_mark_days=stale,
        audited_model_max_loss_from_initial=loss,
        modeled_loss_budget_breached=None if loss is None else loss>config.max_loss_pct+1e-9,
        modeled_final_cash_rial=float(curve.cash_rial.iloc[-1]),
        mean_observed_cash_fraction=float((curve.cash_rial/curve.equity_rial).mean()) if complete else None,
        execution_status_counts={} if decisions.empty else {str(k):int(v) for k,v in decisions.status.value_counts().items()},
        buys=0 if executions.empty else int(executions.side.eq('buy').sum()),
        sells=0 if executions.empty else int(executions.side.eq('sell').sum()),
        rolling_12month=dict(windows=len(rolling),known_model_endpoints=int(returns.notna().sum()),
            unknown_model_endpoints=int(returns.isna().sum()),fresh_model_paths=int(rolling.fresh_model_path.sum()),
            below_30pct_model_windows=int(returns.lt(.30).sum()),minimum_model_return=None if returns.dropna().empty else float(returns.min()),
            median_model_return=None if returns.dropna().empty else float(returns.median()),
            gregorian_calendar_months=12,economic_target_verified=False),
        computational_uncertainty_blockers=blockers,cash_fees_capacity_reconciled=True,
        accepted_for_signals=False,economic_profit_verified=False)
    return summary,rolling


def fixed_contract(capital_rial):
    return dict(version=VERSION,hypotheses=[asdict(h) for h in HYPOTHESES],horizons=list(HORIZONS),
        modes=mode_specs(),entry_allocation_fraction=ALLOCATION,loss_floor_exit=True,
        config_by_horizon={str(h):asdict(portfolio_config(capital_rial,h)) for h in HORIZONS},
        random_seeds=list(SEEDS),blocks=list(BLOCKS),within_run_comparison_family=COMPARISON_FAMILY,
        primary_configurations=PRIMARY_CONFIGURATIONS,within_run_configurations=WITHIN_RUN_CONFIGURATIONS,
        historical_project_trial_count=None,previous_disclosed_trial_lower_bound=715,
        disclosed_trial_lower_bound_after_this_run=715+WITHIN_RUN_CONFIGURATIONS,
        thresholds_optimized=False,models_fitted=0,holdout_opened=False,accepted_for_signals=False,
        economic_profit_verified=False,probability_of_future_profit=None)
