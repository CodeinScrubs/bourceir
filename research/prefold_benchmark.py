"""One registered 20-session median-liquidity control, separate from Day-1 Top-5."""

import argparse
import hashlib
import json
import uuid
import zipfile
from pathlib import Path

import pandas as pd

from backtest.evidence import audit_daily_replay
from backtest.portfolio import PortfolioConfig, replay_portfolio
from backtest.prepared import prepare_portfolio_prices
from config import settings
from core.price_quality import RAW_PRICES, price_quality
from core.timeutils import is_canonical_iso_date, now_tehran_iso
from database.db import DatabaseManager
from ops.validation_run import write_state
from research.strategy_benchmarks import EXTENSIONS, SCENARIOS, _cashflows
from research.strategy_execution import load_publications
from research.strategy_outcomes import outcome_status
from research.strategy_suite import _bytes, _frame_hash, _freeze_code, _sha256, _sources_unchanged, review_suite
from research.walkforward import preceding_price_observations

VERSION = 'prefold-20d-median-liquidity-top5-v1'
CASE = 'PRE-FOLD-20D-MEDIAN-LIQUIDITY-TOP5'


def prefold_selection(prices, registry, calendar, test_start):
    """Use exactly the last 20 declared sessions and >=15 coherent positive rows.

    Present-day is_active is never an eligibility filter. Missing/zero/invalid
    observations are counted, not filled with zero or earlier observations.
    Membership is conditional on the supplied registry, not historical proof.
    """
    if (list(calendar) != sorted(set(calendar)) or any(not is_canonical_iso_date(d) for d in calendar)
            or test_start not in calendar or calendar.index(test_start) < 20):
        raise ValueError('Twenty declared sessions strictly before the fold are required')
    if (not {'symbol_fa', 'ins_code', 'instrument_type_code'} <= set(registry)
            or registry.symbol_fa.duplicated().any() or registry.ins_code.duplicated().any()
            or not registry.ins_code.map(lambda s: isinstance(s, str) and s.isascii() and s.isdigit()).all()):
        raise ValueError('Unique native instrument identities are required')
    if not {'date', 'symbol', *RAW_PRICES} <= set(prices) or prices.columns.duplicated().any():
        raise ValueError('Raw price and traded-value columns are required')
    first = calendar.index(test_start)
    lookback = list(calendar[first-20:first])
    # Slice before inspecting numeric observations. Post-decision values never affect ranking.
    window = prices.loc[prices.date.isin(lookback)].copy()
    if window.duplicated(['date', 'symbol']).any():
        raise ValueError('Duplicate pre-fold observation')
    numbers, flags = price_quality(window)
    window['valid'] = ~flags.any(axis=1)
    window['numeric_value'] = numbers.value
    groups = {symbol: frame for symbol, frame in window.groupby('symbol', sort=False)}
    rows = []
    for r in registry.itertuples(index=False):
        own = groups.get(r.symbol_fa, window.iloc[:0])
        values = own.loc[own.valid, 'numeric_value']
        ordinary = r.instrument_type_code in (300, 303)
        eligible = ordinary and len(values) >= 15
        rows.append(dict(symbol=r.symbol_fa, ins_code=r.ins_code, valid_observations=len(values),
            missing_observations=20-len(own), invalid_or_zero_observations=len(own)-len(values),
            median_traded_value_rial=float(values.median()) if len(values) else None,
            eligible=eligible, exclusion=None if eligible else (
                'not_ordinary_stock' if not ordinary else 'fewer_than_15_valid_sessions')))
    candidates = sorted([r for r in rows if r['eligible']],
                        key=lambda r: (-r['median_traded_value_rial'], int(r['ins_code'])))
    ranks = {r['ins_code']: i for i, r in enumerate(candidates)}
    for row in rows:
        row['rank'] = ranks.get(row['ins_code'])
        row['selected'] = row['ins_code'] in {r['ins_code'] for r in candidates[:5]}
    picks = [dict(date=lookback[-1], symbol=r['ins_code'], rank=i) for i, r in enumerate(candidates[:5])]
    return dict(lookback_sessions=lookback, decision_date=lookback[-1], test_start=test_start,
                complete_top5=len(picks) == 5, picks=picks, eligibility=rows)


def _evaluate(parent, publications, selections, archive=None):
    protocol = json.loads((parent / 'protocol.json').read_bytes())
    registry = pd.read_parquet(parent / 'registry.parquet')
    calendar = json.loads((parent / 'calendar.json').read_bytes())
    prices = pd.read_parquet(parent / 'prices.parquet')
    entries, marks, _ = load_publications(publications, registry, calendar, protocol['plan']['end_exclusive'])
    identities = dict(zip(registry.symbol_fa, registry.ins_code, strict=True))
    native = prices.assign(symbol=prices.symbol.map(identities))
    plan, records = protocol['plan'], []
    for fold, choice in enumerate(selections):
        bounds = protocol['folds'][fold]
        if _bytes(prefold_selection(prices, registry, calendar, bounds['test_start'])) != _bytes(choice):
            raise ValueError('Frozen pre-fold selection no longer reproduces')
        base = [d for d in calendar if bounds['test_start'] <= d < bounds['test_end_exclusive']]
        begin = calendar.index(choice['decision_date'])
        picks = pd.DataFrame(choice['picks'], columns=['date', 'symbol', 'rank'])
        scope = set(picks.symbol)
        prefixes = {}
        for extension in EXTENSIONS:
            days = calendar[begin:begin+len(base)+1+extension]
            enough = len(days) == len(base)+1+extension
            if enough and days[-1] >= settings.RESEARCH_CONFIG['holdout_start']:
                raise ValueError('The liquidation window crosses the sealed boundary')
            for scenario in SCENARIOS:
                header = dict(case=CASE, fold=fold, scenario=scenario, liquidation_sessions=extension,
                    test_start=bounds['test_start'], decision_date=choice['decision_date'],
                    objective='base_date_nav' if extension == 0 else 'terminal_cash',
                    base_endpoint_date=base[-1], target_endpoint_date=days[-1] if enough else None,
                    requested_sessions=len(base)+1+extension, decision_warmup_sessions=1,
                    picks_sha256=_frame_hash(picks))
                if not enough or not choice['complete_top5']:
                    item = dict(**header, financial_path_computed=False, model_nav_known=False,
                        modeled_terminal_cash_known=False, model_nav_return=None, modeled_terminal_cash_return=None,
                        economic_accounting_verified=False, actual_execution_verified=False,
                        unresolved_reason='insufficient_calendar' if not enough else 'fewer_than_five_eligible_stocks')
                else:
                    raw = pd.concat([preceding_price_observations(native, days[0]),
                        native.loc[native.date.ge(days[0]) & native.date.le(days[-1])]], ignore_index=True)
                    prepared = prepare_portfolio_prices(raw)
                    entry = [o for o in entries if o['ins_code'] in scope and o['requested_date'] in days]
                    proof = [o for o in marks if o['ins_code'] in scope and o['requested_date'] in days]
                    stress = scenario == 'stress'
                    cfg = PortfolioConfig(initial_capital_rial=plan['initial_capital_rial'],
                        horizon=len(base), max_positions=5, max_loss_pct=.10, allow_entry_day_exit=False,
                        participation=plan['stress_participation' if stress else 'participation'],
                        slippage_bps=plan['stress_slippage_bps' if stress else 'slippage_bps'], stress=stress)
                    result = replay_portfolio(prepared, picks, calendar, cfg, start=days[0],
                        end=calendar[calendar.index(days[-1])+1] if days[-1] != calendar[-1] else plan['end_exclusive'],
                        no_trade_entry_observations=entry, no_trade_valuation_observations=proof)
                    audit = audit_daily_replay(result, prepared, picks, days, no_trade_valuation_observations=proof)
                    item = dict(**header, financial_path_computed=True, audit=audit, assumptions=result.assumptions,
                        metrics=result.metrics, picks=picks.to_dict('records'), equity=result.equity.to_dict('records'),
                        executions=result.executions.to_dict('records'), decisions=result.decisions.to_dict('records'))
                    item.update(outcome_status(item, audit, days))
                    item['per_instrument_trading_cashflows'] = _cashflows(item['executions'])
                    prefix = json.loads(_bytes({field: [{k: v for k, v in row.items() if pd.notna(v)}
                        for row in item[field] if row['date'] <= base[-1]]
                        for field in ('equity', 'executions', 'decisions')}))
                    if extension == 0:
                        prefixes[scenario] = prefix
                    elif prefix != prefixes[scenario]:
                        raise ValueError('Liquidation changed a base-period financial path')
                body = _bytes(item)
                member = f'fold-{fold:04d}/{scenario}-{extension:02d}.json'
                if archive is not None:
                    archive.writestr(member, body)
                records.append(dict(**header, member=member, sha256=hashlib.sha256(body).hexdigest(),
                    model_nav_known=item['model_nav_known'], model_nav_return=item['model_nav_return'],
                    modeled_terminal_cash_known=item['modeled_terminal_cash_known'],
                    modeled_terminal_cash_return=item['modeled_terminal_cash_return'],
                    invariant_violations=item.get('audit', {}).get('invariant_violations', [])))
    return dict(version=VERSION, expected_replays=len(selections)*2*len(EXTENSIONS), records=records,
                invariant_failures=sum(bool(r['invariant_violations']) for r in records), accepted_for_signals=False)


def run_prefold_benchmark(parent, publications, *, root=None):
    private = (settings.DATA_DIR / 'private').resolve()
    parent, publications = Path(parent).resolve(), Path(publications).resolve()
    base = Path(root or private / 'prefold-benchmarks').resolve()
    if any(not p.is_relative_to(private) for p in (parent, publications, base)):
        raise ValueError('Explicit frozen inputs and output must remain private')
    review_suite(parent)
    source = json.loads((parent / 'protocol.json').read_bytes())
    if source['fees'] != dict(settings.FEES):
        raise ValueError('Frozen fee assumptions changed')
    content = publications.read_bytes()
    registry, prices = (pd.read_parquet(parent / name) for name in ('registry.parquet', 'prices.parquet'))
    calendar = json.loads((parent / 'calendar.json').read_bytes())
    load_publications(content, registry, calendar, source['plan']['end_exclusive'])
    choices = [prefold_selection(prices, registry, calendar, f['test_start']) for f in source['folds']]
    folder = base / ('PREFOLD-' + uuid.uuid4().hex)
    folder.mkdir(parents=True, exist_ok=False)
    (folder / 'publications.json').write_bytes(content)
    write_state(folder / 'selections.json', choices)
    sources = _freeze_code(folder / 'code.zip')
    protocol = dict(version=VERSION, created_at=now_tehran_iso(), parent=str(parent),
        parent_manifest_sha256=_sha256(parent / 'manifest.json'), source_files=sources,
        publications_sha256=_sha256(publications), selections_sha256=_sha256(folder / 'selections.json'),
        lookback=20, minimum_valid=15, statistic='median_positive_coherent_daily_traded_value',
        tie_break='numeric_native_instrument_id_ascending', present_day_active_filter=False,
        eligibility='ordinary stocks in frozen current registry with >=15 valid rows in exactly 20 prior sessions',
        holdout_opened=False, historical_membership_verified=False, historical_fees_verified=False,
        fees=dict(settings.FEES), economic_accounting_verified=False, actual_execution_verified=False,
        initial_order='decision before fold; one t+1 attempt on first fold session',
        passive_exit='first post-base session, retry within fixed 5/10/21 sessions', accepted_for_signals=False)
    write_state(folder / 'protocol.json', protocol)
    write_state(folder / 'state.json', dict(status='registered'))
    try:
        with DatabaseManager(folder / 'experiments.db') as db:
            db.init_db()
            registered = db.log_experiment(experiment_id=folder.name+'-PROTOCOL', strategy_name=VERSION,
                symbols_tested='conditional frozen registry', parameters_json=_bytes(protocol).decode(), metrics_json='{}',
                start_date=source['plan']['start'], end_date=source['plan']['end_exclusive'],
                dataset_version=protocol['parent_manifest_sha256'], features_version='prefold-median-20', model_version=VERSION)
            if registered is None:
                raise RuntimeError('Experiment protocol registration failed')
            with zipfile.ZipFile(folder / 'replays.zip', 'x', compression=zipfile.ZIP_DEFLATED) as archive:
                result = _evaluate(parent, content, choices, archive)
            if not _sources_unchanged(sources):
                raise RuntimeError('Runtime changed during the experiment')
            write_state(folder / 'results.json', result)
            registered = db.log_experiment(experiment_id=folder.name, strategy_name=VERSION,
                symbols_tested='conditional frozen registry',
                parameters_json=_bytes(protocol).decode(), metrics_json=_bytes(result).decode(),
                start_date=source['plan']['start'], end_date=source['plan']['end_exclusive'],
                dataset_version=protocol['parent_manifest_sha256'], features_version='prefold-median-20', model_version=VERSION)
            if registered is None:
                raise RuntimeError('Experiment result registration failed')
    except BaseException as error:
        write_state(folder / 'state.json', dict(status='failed', error=str(error)))
        raise
    write_state(folder / 'state.json', dict(status='complete' if not result['invariant_failures'] else 'failed'))
    write_state(folder / 'manifest.json', {p.name: _sha256(p) for p in folder.iterdir() if p.is_file()})
    return dict(folder=str(folder), **result)


def review_prefold_benchmark(folder, *, reexecute=False, parent_override=None):
    folder = Path(folder).resolve()
    if not folder.is_relative_to((settings.DATA_DIR / 'private').resolve()):
        raise ValueError('Private benchmark evidence required')
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    if (set(manifest) != {p.name for p in folder.iterdir() if p.is_file() and p.name != 'manifest.json'}
            or any(Path(n).name != n or _sha256(folder / n) != h for n, h in manifest.items())):
        raise ValueError('Evidence file manifest mismatch')
    protocol = json.loads((folder / 'protocol.json').read_bytes())
    parent = Path(parent_override or protocol['parent']).resolve()
    if _sha256(parent / 'manifest.json') != protocol['parent_manifest_sha256']:
        raise ValueError('Frozen parent identity mismatch')
    review_suite(parent)
    policy = dict(version=VERSION, lookback=20, minimum_valid=15,
        tie_break='numeric_native_instrument_id_ascending', present_day_active_filter=False,
        holdout_opened=False, historical_membership_verified=False, historical_fees_verified=False,
        economic_accounting_verified=False, actual_execution_verified=False, accepted_for_signals=False)
    if (any(type(protocol.get(k)) is not type(v) or protocol[k] != v for k, v in policy.items())
            or json.loads((folder / 'state.json').read_bytes())['status'] != 'complete'):
        raise ValueError('Incomplete or incompatible pre-fold experiment')
    result = json.loads((folder / 'results.json').read_bytes())
    choices = json.loads((folder / 'selections.json').read_bytes())
    if (_sha256(folder / 'selections.json') != protocol['selections_sha256']
            or _sha256(folder / 'publications.json') != protocol['publications_sha256']
            or result['version'] != VERSION or result['accepted_for_signals'] is not False
            or result['invariant_failures'] != sum(bool(r['invariant_violations']) for r in result['records'])):
        raise ValueError('Input binding or result policy mismatch')
    with zipfile.ZipFile(folder / 'code.zip') as archive:
        sources = protocol['source_files']
        if (len(archive.namelist()) != len(sources) or set(archive.namelist()) != set(sources)
                or any(hashlib.sha256(archive.read(n)).hexdigest() != h for n, h in sources.items())):
            raise ValueError('Runtime archive mismatch')
    expected = {(f, s, e) for f in range(len(choices)) for s in SCENARIOS for e in EXTENSIONS}
    if (len(result['records']) != len(expected) or result['expected_replays'] != len(expected)
            or {(r['fold'], r['scenario'], r['liquidation_sessions']) for r in result['records']} != expected):
        raise ValueError('Pre-fold result denominator mismatch')
    with zipfile.ZipFile(folder / 'replays.zip') as archive:
        if len(archive.namelist()) != len(expected):
            raise ValueError('Replay denominator mismatch')
        for r in result['records']:
            body = archive.read(r['member'])
            item = json.loads(body)
            if (hashlib.sha256(body).hexdigest() != r['sha256'] or any(item[k] != r[k] for k in r if k not in (
                    'member', 'sha256', 'invariant_violations')) or item.get('audit', {}).get('invariant_violations')):
                raise ValueError('Saved financial replay mismatch')
            if any(item.get(k) is not False for k in ('economic_accounting_verified', 'actual_execution_verified')):
                raise ValueError('Daily modeled replays cannot certify actual economic rights or fills')
            if item.get('financial_path_computed'):
                picks = pd.DataFrame(item['picks'], columns=['date', 'symbol', 'rank'])
                if (_frame_hash(picks) != r['picks_sha256']
                        or item['picks'] != choices[r['fold']]['picks']):
                    raise ValueError('Saved decision tape differs from the pre-fold selection')
    if reexecute:
        if not _sources_unchanged(protocol['source_files']) or protocol['fees'] != dict(settings.FEES):
            raise ValueError('Exact frozen runtime required')
        actual = _evaluate(parent, (folder / 'publications.json').read_bytes(), choices)
        if _bytes(actual) != _bytes(result):
            raise ValueError('Financial pre-fold reexecution did not reproduce')
    return dict(status='verified_prefold_benchmark', replays=len(expected),
                financial_reexecution_performed=reexecute, economic_profit_verified=False, accepted_for_signals=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--parent')
    action.add_argument('--review')
    parser.add_argument('--publications')
    parser.add_argument('--output-root')
    parser.add_argument('--reexecute', action='store_true')
    parser.add_argument('--parent-override')
    args = parser.parse_args()
    if args.review:
        result = review_prefold_benchmark(args.review, reexecute=args.reexecute, parent_override=args.parent_override)
    else:
        if not args.publications:
            parser.error('--parent requires --publications')
        result = run_prefold_benchmark(args.parent, args.publications, root=args.output_root)
    print(json.dumps({k: v for k, v in result.items() if k != 'records'}, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
