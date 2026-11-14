"""Registered, bounded capacity-screen ablation using the existing daily engine.

These previously inspected folds and synthetic accounting cannot establish an
economic trading edge. Every incomplete path stays in the paired denominator.
"""

import argparse
import hashlib
import json
import subprocess
import zipfile
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.evidence import audit_daily_replay
from backtest.portfolio import replay_portfolio
from backtest.prepared import prepare_portfolio_prices
from config import settings
from core.timeutils import now_tehran_iso
from database.db import DatabaseManager
from ops.validation_run import write_state
from research.capacity_preflight import CAPITAL_RIAL, HORIZONS, SLOTS, THRESHOLD_RIAL, _verified_inputs
from research.run import _sha256
from research.strategy_execution import load_publications
from research.strategy_suite import SuitePlan, _bytes, _configuration, _freeze_code, _restore_cases, _sources_unchanged
from research.walkforward import preceding_price_observations

VERSION = 'capacity-momentum-ablation-01-v1'
ARMS, SCENARIOS = ('control', 'filtered'), ('normal', 'stress')


def selection_tape(rows, fold, horizon, identities):
    """Rank first, then use the same pending/held-slot policy in both arms."""
    records = {arm: [] for arm in ARMS}
    for row in rows:
        if row['fold'] != fold or horizon == 2 and row['planned_primary_exit'] is None:
            continue
        for arm, key in (('control', 'control_top5'), ('filtered', 'filtered_top5')):
            names = row[key]
            if len(names) > SLOTS or len(names) != len(set(names)) or any(s not in identities for s in names):
                raise ValueError('Invalid frozen selection tape')
            records[arm].extend(dict(date=row['decision_date'], symbol=identities[name], rank=i)
                                for i, name in enumerate(names))
    return {arm: pd.DataFrame(values, columns=['date', 'symbol', 'rank']) for arm, values in records.items()}


def diagnostics(replay, audit, prepared, days):
    """Full-window metrics are null on incomplete paths; prefix diagnostics are labeled."""
    metrics, curve, trades = replay.metrics, replay.equity, replay.executions
    complete = (metrics['status'] == 'complete' and curve.date.tolist() == list(days)
                and audit['observed_path_model_return'] is not None)
    nav = np.r_[CAPITAL_RIAL, curve.equity_rial.to_numpy(dtype=float)]
    returns = nav[1:] / nav[:-1] - 1
    sharpe = sortino = None
    if complete and np.isfinite(returns).all():
        deviation = returns.std(ddof=1)
        downside = np.sqrt(np.mean(np.minimum(returns, 0)**2))
        sharpe = float(np.sqrt(252)*returns.mean()/deviation) if deviation > 0 else None
        sortino = float(np.sqrt(252)*returns.mean()/downside) if downside > 0 else None
    status_counts = Counter(replay.decisions.status) if not replay.decisions.empty else Counter()
    buys = [] if trades.empty else trades.loc[trades.side.eq('buy')].to_dict('records')
    slippage = 0.
    for trade in trades.to_dict('records'):
        bar = prepared.sessions[trade['date']][trade['symbol']]
        vwap = bar['value']/bar['volume']
        slippage += trade['quantity']*(trade['price_rial']-vwap)*(1 if trade['side']=='buy' else -1)
    invested = (curve.equity_rial - curve.cash_rial - curve.noncash_receivable_rial)/curve.equity_rial
    terminal_cash = (float(curve.cash_rial.iloc[-1]) if complete and metrics['open_positions'] == 0
                     and metrics['noncash_receivable_rial'] == 0 else None)
    # Fees can be added back on the SAME realized fill path only when fully cash.
    # This is neither a no-fee sizing counterfactual nor an independently liquidated NAV.
    gross = None if terminal_cash is None else (terminal_cash + metrics['fees_rial'])/CAPITAL_RIAL - 1
    unknown_entry = sum(status_counts[k] for k in ('unknown_entry_execution', 'no_session',
                                                  'conflicting_no_trade_entry'))
    unfilled = sum(status_counts[k] for k in ('locked_buy', 'observed_no_trade_entry',
                                            'insufficient_capacity', 'limit_not_filled'))
    attempts = len(buys) + unfilled + unknown_entry
    return dict(full_model_path=bool(complete), net_modeled_return=metrics['total_return'] if complete else None,
        same_fill_path_gross_return_before_explicit_fees=gross,
        maximum_drawdown=metrics['max_drawdown'] if complete else None,
        sharpe_252_sessions_descriptive=sharpe, sortino_252_sessions_descriptive=sortino,
        gross_turnover_multiple=audit['gross_turnover_multiple'] if complete else None,
        explicit_fees_rial=metrics['fees_rial'], slippage_vs_same_day_vwap_rial=slippage,
        costs_scope='observed prefix; full window only when full_model_path is true',
        mean_capital_utilization=float(invested.mean()) if complete else None,
        observed_prefix_mean_capital_utilization=float(invested.mean()),
        end_observed_cash_rial=float(curve.cash_rial.iloc[-1]),
        terminal_modeled_liquidation_cash_rial=terminal_cash, actual_liquidation_cash_verified=False,
        residual_positions=metrics['open_positions'], synthetic_receivable_rial=metrics['noncash_receivable_rial'],
        synthetic_fractional_exposures=metrics['fractional_exposures'],
        actual_receivables=None, actual_entitlements_verified=False,
        buy_execution_count=len(buys), known_unfilled_entry_count=unfilled,
        unknown_entry_attempt_count=unknown_entry,
        observed_attempt_execution_rate=len(buys)/attempts if attempts else None,
        execution_rate_scope='attempts reached before a possible path stop; excludes reserved/held slot rejections',
        partial_entry_count=None, partial_entry_count_reason='base engine does not persist pre-capacity requested quantity',
        partial_exit_wait_count=status_counts['partial_exit_wait'],
        status_counts=dict(status_counts), recorded_sessions=len(curve), expected_sessions=len(days),
        corporate_action_events=metrics['corporate_action_events'])


def paired_results(records):
    expected = {(fold, h, scenario, arm) for fold in range(5) for h in HORIZONS
                for scenario in SCENARIOS for arm in ARMS}
    lookup = {(r['fold'], r['horizon'], r['scenario'], r['arm']): r for r in records}
    if len(records) != 40 or set(lookup) != expected:
        raise ValueError('All forty paths, including unknowns, must be preserved')
    pairs = []
    for fold in range(5):
        for horizon in HORIZONS:
            for scenario in SCENARIOS:
                left, right = (lookup[fold, horizon, scenario, arm] for arm in ARMS)
                a, b = (r['diagnostics']['net_modeled_return'] for r in (left, right))
                pairs.append(dict(fold=fold, horizon=horizon, scenario=scenario, control=a, filtered=b,
                    paired_difference=None if a is None or b is None else b-a,
                    full_paired_model_path=a is not None and b is not None,
                    economic_comparison_verified=False))
    primary = [p for p in pairs if p['horizon'] == 2]
    # A five-fold, previously viewed, synthetic-valuation comparison never
    # clears the independent prospective/economic gate by itself.
    return dict(pairs=pairs, primary_pairs=10, primary_complete_pairs=sum(p['full_paired_model_path'] for p in primary),
        primary_unknown_pairs=sum(not p['full_paired_model_path'] for p in primary),
        successful_subset_average=None, verdict='INCONCLUSIVE',
        verdict_reason='conditional calendar/universe and synthetic accounting; unknown paths remain in denominator',
        accepted_for_signals=False, profitability_proven=False, prospective_candidate=False)


def run_ablation(suite, preflight, publications, calendar_review, output, *, progress=None):
    private = (settings.DATA_DIR/'private').resolve()
    suite, preflight, publications, calendar_review, output = (
        Path(p).resolve() for p in (suite, preflight, publications, calendar_review, output))
    if any(p == private or not p.is_relative_to(private) for p in
           (suite, preflight, publications, calendar_review, output)):
        raise ValueError('Explicit private research paths are required')
    inputs = _verified_inputs(suite, ('protocol.json', 'calendar.json', 'registry.parquet',
                                      'prices.parquet', 'features.parquet'))
    source = json.loads((suite/'protocol.json').read_bytes())
    scope = json.loads((preflight/'scope.json').read_bytes())
    result = json.loads((preflight/'result.json').read_bytes())
    adjudication = json.loads(calendar_review.read_bytes())
    if (scope['suite_inputs_sha256'] != inputs or result['financial_replays'] != 0
            or result['holdout_loaded'] or source['fees'] != dict(settings.FEES)
            or source['plan']['end_exclusive'] > settings.RESEARCH_CONFIG['holdout_start']
            or len(source['folds']) != 5 or source['plan']['initial_capital_rial'] != CAPITAL_RIAL
            or not adjudication['bounded_review_complete']
            or adjudication['retained_calendar_sha256'] != inputs['calendar.json']
            or adjudication['financial_use'] != 'conditional_exploratory_only'):
        raise ValueError('Conditional preflight/data/fee/calendar contract differs')
    for name, digest in scope['code_sha256'].items():
        if _sha256(settings.BASE_DIR/name) != digest:
            raise ValueError('Preflight source code changed')
    calendar = json.loads((suite/'calendar.json').read_bytes())
    rows = json.loads((preflight/'decisions.json').read_bytes())
    registry = pd.read_parquet(suite/'registry.parquet')
    identities = dict(zip(registry.symbol_fa, registry.ins_code, strict=True))
    contents = publications.read_bytes()
    entries, marks, classified = load_publications(contents, registry, calendar, source['plan']['end_exclusive'])
    if source['holdout_loaded'] or any(d >= settings.RESEARCH_CONFIG['holdout_start'] for d in calendar):
        raise ValueError('Development-only calendar is required')
    output.mkdir(parents=True, exist_ok=False)
    protocol = dict(version=VERSION, experiment='CAPACITY-MOMENTUM-ABLATION-01', created_at=now_tehran_iso(),
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'], text=True).strip(),
        source_files=_freeze_code(output/'code.zip'), suite_inputs_sha256=inputs,
        frozen_inputs_sha256={str(p.relative_to(private)): _sha256(p) for p in
            (preflight/'scope.json', preflight/'result.json', preflight/'decisions.json',
             preflight/'candidate-capacity.parquet', publications, calendar_review)},
        initial_capital_rial=CAPITAL_RIAL, max_positions=SLOTS, threshold_rial=THRESHOLD_RIAL,
        slot_budget='decision NAV/5 constrained by reservable cash, identically in both arms',
        nominal_historical_capital_not_constant_purchasing_power=True,
        primary_horizon=2, secondary_horizon=1, folds=source['folds'], scenarios=SCENARIOS,
        expected_replays=40, arms=ARMS, fees=dict(settings.FEES), plan=source['plan'],
        capacity_policy='20 declared market sessions, >=15 known, sharp lower median >= threshold',
        momentum_contract='unchanged cached ret_20d; 20 instrument rows, all PIT returns valid; absent sessions not inserted',
        calendar_contract='same frozen observed-union calendar; five date checks reviewed with source limitations',
        primary_endpoint='paired net modeled return filtered minus control per fold and scenario',
        secondary_endpoint='same comparison at h1; cannot replace h2',
        unknown_policy='null path and paired delta retained; no successful-subset average',
        accounting='unchanged synthetic reference-ratio shares; actual corporate entitlements unverified',
        rejection='complete paired model paths show no improvement or negative filtered net results',
        prospective_acceptance='requires positive net and paired excess in all primary normal/stress paths, '
            'no material unknowns or unresolved economic accounting and a new independent forward test',
        interpretation='previously inspected development folds, conditional exploratory only',
        holdout_loaded=False, accepted_for_signals=False, parameter_search=False)
    write_state(output/'protocol.json', protocol)
    write_state(output/'state.json', dict(status='registering', financial_replays=0))
    with DatabaseManager(output/'experiments.db') as db:
        db.init_db()
        registered = db.log_experiment(experiment_id='CAPACITY-MOMENTUM-ABLATION-01-PROTOCOL',
            strategy_name=VERSION, symbols_tested='frozen627', parameters_json=_bytes(protocol).decode(),
            metrics_json='{}', start_date=source['plan']['start'], end_date=source['plan']['end_exclusive'],
            dataset_version=inputs['prices.parquet'], features_version=inputs['features.parquet'], model_version=VERSION)
        if registered is None:
            raise RuntimeError('Could not register before financial evaluation')
        write_state(output/'state.json', dict(status='registered', financial_replays=0,
                                            protocol_sha256=_sha256(output/'protocol.json')))
        prices = pd.read_parquet(suite/'prices.parquet')
        prices = prices.assign(symbol=prices.symbol.map(identities))
        cases, plan, records = _restore_cases(source), SuitePlan(**source['plan']), []
        with zipfile.ZipFile(output/'replays.zip', 'x', compression=zipfile.ZIP_DEFLATED) as archive:
            for fold, window in enumerate(source['folds']):
                start, end = window['test_start'], window['test_end_exclusive']
                days = [d for d in calendar if start <= d < end]
                raw = pd.concat([preceding_price_observations(prices, start),
                    prices.loc[prices.date.ge(start) & prices.date.lt(end)]], ignore_index=True)
                prepared = prepare_portfolio_prices(raw)
                for horizon in HORIZONS:
                    tapes = selection_tape(rows, fold, horizon, identities)
                    case = next(c for c in cases if c.name == f'base:momentum_20d:h{horizon}')
                    for scenario in SCENARIOS:
                        cfg = _configuration(case, plan, scenario, horizon, SLOTS)
                        for arm in ARMS:
                            tape = tapes[arm]
                            symbols = set(tape.symbol)
                            own_entries = [o for o in entries if o['ins_code'] in symbols and start <= o['requested_date'] < end]
                            own_marks = [o for o in marks if o['ins_code'] in symbols and start <= o['requested_date'] < end]
                            replay = replay_portfolio(prepared, tape, calendar, cfg, start=start, end=end,
                                entry_allocation_fraction=case.allocation, loss_floor_exit=False,
                                no_trade_entry_observations=own_entries, no_trade_valuation_observations=own_marks)
                            audit = audit_daily_replay(replay, prepared, tape, days, no_trade_valuation_observations=own_marks)
                            own = dict(fold=fold, horizon=horizon, scenario=scenario, arm=arm,
                                audit=audit, diagnostics=diagnostics(replay, audit, prepared, days))
                            body = _bytes(dict(**own, metrics=replay.metrics, assumptions=replay.assumptions,
                                picks=tape.to_dict('records'), equity=replay.equity.to_dict('records'),
                                executions=replay.executions.to_dict('records'), decisions=replay.decisions.to_dict('records')))
                            member = f'fold-{fold}/h{horizon}/{scenario}/{arm}.json'
                            archive.writestr(member, body)
                            records.append(dict(**own, member=member, sha256=hashlib.sha256(body).hexdigest()))
                            write_state(output/'state.json', dict(status='running', financial_replays=len(records)))
                            if progress:
                                progress(dict(completed=len(records), expected=40, fold=fold, horizon=horizon, scenario=scenario, arm=arm))
        if not _sources_unchanged(protocol['source_files']):
            raise RuntimeError('Frozen source changed during the experiment')
        final = dict(version=VERSION, records=records, **paired_results(records),
            financial_replays=len(records), publication_status_counts=dict(Counter(r['status'] for r in classified)),
            invariant_failures=sum(bool(r['audit']['invariant_violations']) for r in records), holdout_loaded=False)
        write_state(output/'result.json', json.loads(_bytes(final)))
    write_state(output/'state.json', dict(status='complete', financial_replays=len(records)))
    write_state(output/'manifest.json', {p.name:_sha256(p) for p in output.iterdir() if p.is_file()})
    return final


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('suite', 'preflight', 'publications', 'calendar-review', 'output'):
        parser.add_argument('--'+name, required=True)
    args = parser.parse_args()
    final = run_ablation(args.suite, args.preflight, args.publications, args.calendar_review, args.output,
        progress=lambda row: print(json.dumps(row), flush=True))
    print(json.dumps({k:v for k,v in final.items() if k not in ('records','pairs')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
