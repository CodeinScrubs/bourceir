"""Replay the same frozen ten sleeves with explicit documents and economic claims.

This is an accounting correction study, never a new stock-selection experiment.
Only a completed development-period parent is allowed; old files are untouched.
"""

import argparse
import json
import shutil
import uuid
from pathlib import Path

import pandas as pd

from config import settings
from core import market_rules
from core.opportunity_radar import digest
from core.price_quality import numeric_prices
from core.timeutils import is_canonical_iso_date, now_tehran_iso
from database.db import DatabaseManager
from ops.report_files import private_report_path, read_private_report, write_private_report
from research.anniversary import aggregate_sleeves, replay_sleeve, scoped_prices
from research.anniversary_run import _same_frame, _verified_selection, sha
from research.corporate_inputs import accounting_book, evidence_path, load_inputs
from research.labels import market_calendar
from research.run import _json_safe, archive_research_code

VERSION = 'anniversary-economic-accounting-v1'


def _write(path, value):
    return write_private_report(path, _json_safe(value))


def _register(folder, protocol, suffix, metrics):
    with DatabaseManager(folder / 'experiments.db') as db:
        receipt = db.log_experiment(experiment_id=protocol['id'] + suffix, strategy_name=VERSION,
            symbols_tested='same ten frozen parent identities; no reselection',
            parameters_json=json.dumps(protocol, ensure_ascii=False, allow_nan=False),
            metrics_json=json.dumps(_json_safe(metrics), ensure_ascii=False, allow_nan=False),
            start_date=protocol['intent']['requested_start'], end_date=protocol['intent']['requested_end'],
            dataset_version=protocol['parent_files']['public-source.parquet'],
            features_version='unchanged parent selection', model_version=VERSION)
        if receipt is None:
            raise RuntimeError('Accounting study registration failed')


def accounting_requirements(prices, cases, decision, terminal):
    """Observed reference discontinuities are document tasks, never guessed action types."""
    rows = []
    for case in cases:
        own = prices.loc[prices.symbol.eq(case['symbol']) & prices.date.le(terminal)].sort_values('date').copy()
        own = pd.concat([own.loc[own.date.lt(decision)].tail(1), own.loc[own.date.ge(decision)]], ignore_index=True)
        if own.empty:
            rows.append(dict(symbol=case['symbol'], status='no_price_path', transitions=[]))
            continue
        numbers = numeric_prices(own)
        for name in numbers:
            own[name] = numbers[name]
        factor = market_rules.corporate_action_factors(own)
        previous = market_rules.official_close(own).shift(1)
        dates = own.date.shift(1)
        supplied = {e['ex_date']: e['evidence_id'] for kind in ('bonuses', 'dividends') for e in case[kind]}
        transitions = []
        for index in own.index[own.date.gt(decision)]:
            value = factor.loc[index]
            if pd.isna(value) or value != 1:
                day = own.loc[index, 'date']
                transitions.append(dict(date=day, previous_price_session=dates.loc[index],
                    previous_official_rial=previous.loc[index], reference_rial=own.loc[index, 'ref_price_raw'],
                    observed_multiplier=value, status='unknown_reference_transition' if pd.isna(value) else 'observed_reference_gap',
                    declared_evidence_id=supplied.get(day), action_type_inferred=False, holder_eligibility_verified=False))
        rows.append(dict(symbol=case['symbol'], coverage=case['coverage'], transitions=transitions,
                         dividend_payment_receipts_supplied=len(case['payments']),
                         bonus_credit_receipts_supplied=len(case['releases'])))
    return _json_safe(rows)


def _evaluate(folder, protocol, *, save):
    if (sha(folder / 'inputs/selection.json') != protocol['selection_sha256']
            or sha(folder / 'inputs/actions.json') != protocol['actions_sha256']):
        raise ValueError('Frozen selection or declared accounting actions changed')
    selection = read_private_report(folder / 'inputs/selection.json')['selected']
    inputs, documents = load_inputs(folder / 'inputs/actions.json', selection)
    prices = pd.read_parquet(folder / 'inputs/prices.parquet')
    days = read_private_report(folder / 'inputs/calendar.json')['days']
    intent = protocol['intent']
    if (not days or days != sorted(set(days)) or days[0] != protocol['decision_date']
            or days[-1] > intent['requested_end'] or days[-1] >= settings.RESEARCH_CONFIG['holdout_start']
            or prices.date.max() > days[-1] or set(prices.symbol) - {s['symbol'] for s in selection}):
        raise ValueError('Accounting inputs exceed the fixed development scope')
    by_symbol = {c['symbol']: c for c in inputs['cases']}
    scenarios = {}
    artifacts = {}
    for mode in ('normal', 'stress'):
        cases, summaries, curves = [], [], []
        for selected in selection:
            symbol = selected['symbol']
            source = by_symbol[symbol]
            case = dict(symbol=symbol, ins_code=selected['ins_code'], coverage=source['coverage'],
                coverage_note=source['coverage_note'], net_model_return=None,
                economic_profit_verified=False, accepted_for_signals=False)
            try:
                book = accounting_book(source, documents)
                summary, curve, executions, decisions = replay_sleeve(prices, symbol=symbol,
                    decision=protocol['decision_date'], calendar=days,
                    initial_capital_rial=intent['initial_capital_rial'] / 10, fees=intent['fees'],
                    slippage_bps=intent['slippage_bps'], participation=intent['participation'],
                    stress=mode == 'stress', corporate_actions=book)
            except ValueError as error:
                case.update(status='blocked_explicit_accounting', reason=str(error), replay_completed=False)
                cases.append(case)
                continue
            complete = source['coverage'] == 'declared_complete'
            if not complete:
                # Preserve the declared-events ledger, never a plausible full NAV.
                summary.update(net_model_return=None, model_final_value_rial=None, fully_liquidated=None)
                curve = curve.copy()
                curve['equity_rial'] = float('nan')
            case.update(status=summary['status'] if complete else 'incomplete_event_coverage',
                net_model_return=summary['net_model_return'], replay_completed=True, summary=summary)
            cases.append(case)
            summaries.append(summary)
            curves.append(curve)
            for kind, frame in (('curve', curve), ('executions', executions), ('decisions', decisions)):
                name = f"{selected['ins_code']}-{mode}-{kind}.parquet"
                if save:
                    frame.to_parquet(folder / name, index=False)
                else:
                    _same_frame(folder / name, frame)
                artifacts[name] = sha(folder / name)
        known = sum(c['net_model_return'] is not None for c in cases)
        basket = dict(sleeves=10, known_sleeves=known, unknown_sleeves=10-known,
                      net_model_return=None, final_model_value_rial=None, net_cash_return=None,
                      max_drawdown=None, max_loss_from_initial=None)
        if len(summaries) == 10 and all(c['coverage'] == 'declared_complete' for c in cases):
            basket, curve = aggregate_sleeves(summaries, curves, k=10,
                                              initial_capital_rial=intent['initial_capital_rial'])
            name = mode + '-basket-curve.parquet'
            if save:
                curve.to_parquet(folder / name, index=False)
            else:
                _same_frame(folder / name, curve)
            artifacts[name] = sha(folder / name)
        basket.update(stocks=cases, economic_profit_verified=False, accepted_for_signals=False)
        scenarios[mode] = basket
    return _json_safe(dict(version=VERSION, id=protocol['id'], selected=[s['symbol'] for s in selection],
        scenarios=scenarios, accounting_requirements=accounting_requirements(prices, inputs['cases'], days[0], days[-1]),
        artifacts_sha256=artifacts, original_selection_changed=False,
        controls_replayed=False, independent_strategy_test=False, probability_calibrated=False,
        holder_entitlements_verified=False, coverage_independently_verified=False,
        historical_fee_schedule_verified=False, economic_profit_verified=False, accepted_for_signals=False,
        parent_results_rewritten=False, holdout_opened=False,
        model='explicit declared bonus/dividend accounting; incomplete coverage preserves unknown NAV'))


def run(parent, actions, folder):
    parent, folder, actions = map(lambda p: Path(p).resolve(), (parent, folder, actions))
    private_report_path(folder / 'protocol.json', forbidden_paths=(settings.DB_PATH, actions))
    if folder.exists():
        raise ValueError('A new private study folder is required; parent and old studies are never overwritten')
    # Reject sealed windows before any parent outcome file is read.
    parent_protocol = read_private_report(parent / 'protocol.json')
    if (not is_canonical_iso_date(parent_protocol['intent']['requested_end'])
            or not is_canonical_iso_date(parent_protocol['intent']['requested_start'])
            or parent_protocol['intent']['requested_end'] >= settings.RESEARCH_CONFIG['holdout_start']
            or parent_protocol['intent']['requested_start'] >= parent_protocol['intent']['requested_end']):
        raise ValueError('Economic accounting correction is restricted to development dates before the seal')
    parent_protocol, frozen, _ = _verified_selection(parent, allow_review_code_change=True)
    with DatabaseManager(parent / 'experiments.db', read_only=True) as db:
        for suffix, metrics in (('-PROTOCOL', {}), ('-SELECTION', frozen)):
            row = db.conn.execute('SELECT parameters_json,metrics_json FROM experiments WHERE experiment_id=?',
                                  (parent_protocol['id'] + suffix,)).fetchone()
            if row is None or json.loads(row['parameters_json']) != parent_protocol or json.loads(row['metrics_json']) != metrics:
                raise ValueError('Frozen parent experiment registration differs')
    payload, documents = load_inputs(actions, frozen['selected'])
    parent_files = {name: sha(parent / name) for name in (
        'protocol.json', 'selection.json', 'selected.parquet', 'asof-panel.parquet', 'registry.json', 'public-source.parquet')}
    folder.mkdir(parents=True, exist_ok=False)
    code = archive_research_code(folder / 'code')
    name = 'ops/report_files.py'
    source = (settings.BASE_DIR / name).read_bytes().replace(b'\r\n', b'\n')
    target = folder / 'code' / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source)
    code[name] = sha(target)
    (folder / 'inputs').mkdir()
    _write(folder / 'inputs/selection.json', frozen)
    _write(folder / 'inputs/actions.json', payload)
    for item in documents.values():
        target = evidence_path(folder / 'inputs', item['path'])
        if target in (folder / 'inputs/actions.json', folder / 'inputs/selection.json', folder / 'inputs/calendar.json'):
            raise ValueError('Evidence may not replace a study input')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(item['source'], target)
    load_inputs(folder / 'inputs/actions.json', frozen['selected'])
    protocol = dict(version=VERSION, id='ANNUAL-ECON-' + uuid.uuid4().hex, registered_at=now_tehran_iso(),
        intent=parent_protocol['intent'], parent_id=parent_protocol['id'], parent_files=parent_files,
        decision_date=frozen['decision_date'], source_code=code,
        actions_sha256=sha(folder / 'inputs/actions.json'), selection_sha256=sha(folder / 'inputs/selection.json'),
        accounting_only=True, retrospective_documents=True, selection_changed=False,
        holdout_opened=False, accepted_for_signals=False, economic_profit_verified=False)
    _write(folder / 'protocol.json', protocol)
    _register(folder, protocol, '-PROTOCOL', {})
    prices = pd.read_parquet(parent / 'public-source.parquet',
                             filters=[('date', '<=', protocol['intent']['requested_end'])])
    identities = read_private_report(parent / 'registry.json')['identities']
    prices = scoped_prices(prices, identities, through=protocol['intent']['requested_end'])
    days = [day for day in market_calendar(prices).tolist() if day >= frozen['decision_date']]
    prices.loc[prices.symbol.isin([s['symbol'] for s in frozen['selected']])].to_parquet(
        folder / 'inputs/prices.parquet', index=False)
    _write(folder / 'inputs/calendar.json', dict(days=days))
    if any(sha(parent / name) != expected for name, expected in parent_files.items()):
        raise ValueError('Parent changed while binding the accounting study')
    inputs = {p.relative_to(folder / 'inputs').as_posix(): sha(p)
              for p in (folder / 'inputs').rglob('*') if p.is_file()}
    _write(folder / 'inputs-manifest.json', inputs)
    _register(folder, protocol, '-INPUTS', inputs)
    _write(folder / 'evaluation-start.json', dict(started_at=now_tehran_iso(),
        inputs_manifest_sha256=sha(folder / 'inputs-manifest.json'), protocol_sha256=sha(folder / 'protocol.json')))
    result = _evaluate(folder, protocol, save=True)
    _write(folder / 'result.json', result)
    _register(folder, protocol, '-RESULT', result)
    _write(folder / 'completion.json', dict(completed_at=now_tehran_iso(), result_sha256=sha(folder / 'result.json'),
        protocol_sha256=sha(folder / 'protocol.json'), accepted_for_signals=False, economic_profit_verified=False))
    return result


def review(folder):
    folder = Path(folder).resolve()
    protocol = read_private_report(folder / 'protocol.json')
    completed = read_private_report(folder / 'completion.json')
    if (protocol['version'] != VERSION or completed['protocol_sha256'] != sha(folder / 'protocol.json')
            or completed['result_sha256'] != sha(folder / 'result.json')):
        raise ValueError('Accounting protocol or result changed')
    for name, expected in protocol['source_code'].items():
        source = (folder / 'code' / name).resolve()
        if (not source.is_relative_to(folder / 'code') or sha(source) != expected
                or (settings.BASE_DIR / name).read_bytes().replace(b'\r\n', b'\n') != source.read_bytes()):
            raise ValueError('Current or archived accounting code differs')
    manifest = read_private_report(folder / 'inputs-manifest.json')
    actual_files = {p.relative_to(folder / 'inputs').as_posix() for p in (folder / 'inputs').rglob('*') if p.is_file()}
    if actual_files != set(manifest):
        raise ValueError('An accounting input was added or omitted')
    for name, expected in manifest.items():
        target = (folder / 'inputs' / name).resolve()
        if not target.is_relative_to(folder / 'inputs') or sha(target) != expected:
            raise ValueError('Accounting input bytes changed')
    start = read_private_report(folder / 'evaluation-start.json')
    if start['inputs_manifest_sha256'] != sha(folder / 'inputs-manifest.json') or start['protocol_sha256'] != sha(folder / 'protocol.json'):
        raise ValueError('Registered accounting inputs differ')
    saved = read_private_report(folder / 'result.json')
    for name, expected in saved['artifacts_sha256'].items():
        target = (folder / name).resolve()
        if not target.is_relative_to(folder) or sha(target) != expected:
            raise ValueError('Accounting ledger bytes changed')
    with DatabaseManager(folder / 'experiments.db', read_only=True) as db:
        for suffix, metrics in (('-PROTOCOL', {}), ('-INPUTS', manifest), ('-RESULT', saved)):
            row = db.conn.execute('SELECT parameters_json,metrics_json FROM experiments WHERE experiment_id=?',
                                  (protocol['id'] + suffix,)).fetchone()
            if row is None or json.loads(row['parameters_json']) != protocol or json.loads(row['metrics_json']) != metrics:
                raise ValueError('Immutable accounting experiment differs')
    actual = _evaluate(folder, protocol, save=False)
    if digest(actual) != digest(saved):
        raise ValueError('Recomputed accounting result differs')
    return dict(verified=True, sleeves_recomputed=20, selection_changed=False,
                accepted_for_signals=False, economic_profit_verified=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['run', 'review'])
    parser.add_argument('--folder', type=Path, required=True)
    parser.add_argument('--parent', type=Path)
    parser.add_argument('--actions', type=Path)
    args = parser.parse_args(argv)
    result = run(args.parent, args.actions, args.folder) if args.mode == 'run' else review(args.folder)
    if args.mode == 'run':
        result = dict(id=result['id'], selected=result['selected'],
                      returns={key: value['net_model_return'] for key, value in result['scenarios'].items()},
                      accepted_for_signals=False, economic_profit_verified=False)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
