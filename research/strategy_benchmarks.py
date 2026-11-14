"""Registered benchmark objectives: a base-date model NAV and later modeled cash.

Selections are copied from a frozen study. These are controls, not a strategy
search, an economic entitlement ledger, or evidence of actual order fills.
"""

import argparse
import hashlib
import json
import uuid
import zipfile
from collections import Counter
from pathlib import Path

import pandas as pd

from backtest.evidence import audit_daily_replay
from backtest.portfolio import PortfolioConfig, replay_portfolio
from backtest.prepared import prepare_portfolio_prices
from config import settings
from core.timeutils import now_tehran_iso
from database.db import DatabaseManager
from ops.validation_run import write_state
from research.run import research_code_fingerprint
from research.strategy_execution import MAX_INPUT_BYTES, load_publications
from research.strategy_outcomes import outcome_status
from research.strategy_suite import _bytes, _frame_hash, _freeze_code, _sha256, _sources_unchanged, review_suite
from research.walkforward import preceding_price_observations

VERSION = 'fixed-benchmark-objectives-v1'
CASES = ('initial_eligible_equal_weight', 'initial_liquidity_top5', 'liquidity_rotation_h1')
EXTENSIONS = (0, 5, 10, 21)
SCENARIOS = ('normal', 'stress')


def benchmark_picks(buy_hold, rotation, identities):
    """The original first-date liquidity order fixes Top-5, without future data."""
    first = buy_hold.sort_values('rank').copy()
    tapes = dict(initial_eligible_equal_weight=first, initial_liquidity_top5=first.head(5).copy(),
                 liquidity_rotation_h1=rotation.copy())
    for name, frame in tapes.items():
        frame['symbol'] = frame.symbol.map(identities)
        if frame.symbol.isna().any() or frame.duplicated(['date','symbol']).any():
            raise ValueError('Benchmark selections must have unique bound native identities')
        tapes[name] = frame[['date','symbol','rank']].reset_index(drop=True)
    return tapes


def _cashflows(executions):
    rows = {}
    for trade in executions:
        own = rows.setdefault(trade['symbol'], dict(ins_code=trade['symbol'], buy_quantity=0, sell_quantity=0,
            buy_gross_rial=0., sell_gross_rial=0., fees_rial=0., net_trading_cashflow_rial=0.,
            is_total_return_attribution=False))
        side = trade['side']
        gross = trade['quantity']*trade['price_rial']
        own[side+'_quantity'] += trade['quantity']
        own[side+'_gross_rial'] += gross
        own['fees_rial'] += trade['fee_rial']
        own['net_trading_cashflow_rial'] += (gross if side == 'sell' else -gross)-trade['fee_rial']
    return list(rows.values())


def _evaluate(parent, contents, *, archive=None):
    protocol = json.loads((parent/'protocol.json').read_bytes())
    original = json.loads((parent/'results.json').read_bytes())
    calendar = json.loads((parent/'calendar.json').read_bytes())
    registry = pd.read_parquet(parent/'registry.parquet')
    entries, marks, coverage = load_publications(contents, registry, calendar, protocol['plan']['end_exclusive'])
    identities = dict(zip(registry.symbol_fa, registry.ins_code, strict=True))
    prices = pd.read_parquet(parent/'prices.parquet').assign(symbol=lambda x:x.symbol.map(identities))
    records, prefixes = [], {}
    index = {(r['case'],r['fold'],r['scenario']):r for r in original['records']}
    with zipfile.ZipFile(parent/'replays.zip') as source:
        for f,fold in enumerate(protocol['folds']):
            def tape(name):
                record = index[name,f,'normal']
                item = json.loads(source.read(record['member']))
                frame = pd.DataFrame(item['picks'], columns=['date','symbol','rank'])
                if _frame_hash(frame) != record['picks_sha256']:
                    raise ValueError('Original benchmark decision tape changed')
                return frame
            tapes = benchmark_picks(tape('buy_hold'), tape('control:liquidity:h1'), identities)
            base = [d for d in calendar if fold['test_start'] <= d < fold['test_end_exclusive']]
            start = calendar.index(base[0])
            plan = protocol['plan']
            for extension in EXTENSIONS:
                days = calendar[start:start+len(base)+extension]
                enough = len(days) == len(base)+extension
                if enough and days[-1] >= settings.RESEARCH_CONFIG['holdout_start']:
                    raise ValueError('Benchmark liquidation cannot cross the sealed boundary')
                if enough:
                    raw = pd.concat([preceding_price_observations(prices,base[0]),
                        prices.loc[prices.date.ge(base[0]) & prices.date.le(days[-1])]],ignore_index=True)
                    prepared = prepare_portfolio_prices(raw)
                for name in CASES:
                    picks = tapes[name]
                    scope = set(picks.symbol)
                    proofs = [o for o in marks if o['ins_code'] in scope and o['requested_date'] in set(days)]
                    entry_proofs = [o for o in entries if o['ins_code'] in scope and o['requested_date'] in set(days)]
                    for scenario in SCENARIOS:
                        header = dict(case=name,fold=f,scenario=scenario,liquidation_sessions=extension,
                            base_endpoint_date=base[-1], objective='base_date_nav' if extension==0 else 'terminal_cash',
                            requested_sessions=len(base)+extension, available_sessions=len(days),
                            target_endpoint_date=days[-1] if enough else None,
                            picks_sha256=_frame_hash(picks))
                        if not enough:
                            item = dict(**header, financial_path_computed=False,
                                unresolved_reason='insufficient_declared_calendar_for_liquidation_window',
                                model_nav_known=False, modeled_terminal_cash_known=False,
                                economic_accounting_verified=False, actual_execution_verified=False,
                                model_nav_return=None, modeled_terminal_cash_return=None)
                        else:
                            stress = scenario=='stress'
                            positions = 5 if name=='liquidity_rotation_h1' else max(1,len(picks))
                            cfg = PortfolioConfig(initial_capital_rial=plan['initial_capital_rial'],
                                horizon=1 if name=='liquidity_rotation_h1' else len(base)-1,
                                max_positions=positions,
                                participation=plan['stress_participation' if stress else 'participation'],
                                slippage_bps=plan['stress_slippage_bps' if stress else 'slippage_bps'],
                                stress=stress, max_loss_pct=.10, allow_entry_day_exit=False)
                            # Passive due date is the FIRST extra session, not the NAV date.
                            result = replay_portfolio(prepared,picks,calendar,cfg,start=base[0],
                                end=calendar[calendar.index(days[-1])+1] if days[-1]!=calendar[-1] else
                                    protocol['plan']['end_exclusive'],
                                no_trade_entry_observations=entry_proofs,no_trade_valuation_observations=proofs)
                            checked = audit_daily_replay(result,prepared,picks,days,
                                no_trade_valuation_observations=proofs)
                            item = dict(**header,financial_path_computed=True, metrics=result.metrics,
                                assumptions=result.assumptions,audit=checked,picks=picks.to_dict('records'),
                                equity=result.equity.to_dict('records'),executions=result.executions.to_dict('records'),
                                decisions=result.decisions.to_dict('records'))
                            status = outcome_status(item,checked,days)
                            item.update(status)
                            item['per_instrument_trading_cashflows'] = _cashflows(item['executions'])
                            # Later evaluation horizons cannot change an already modeled prefix.
                            key = name,f,scenario
                            prefix = {field:[row for row in item[field] if row['date'] <= base[-1]]
                                      for field in ('equity','executions','decisions')}
                            # Later rows add nullable DataFrame columns (exit_kind)
                            # and coerce quantities 33 -> 33.0. Known values must
                            # remain identical; absent and null carry no evidence.
                            prefix = {field:[{k:v for k,v in row.items() if v is not None} for row in rows]
                                      for field,rows in json.loads(_bytes(prefix)).items()}
                            if extension==0:
                                prefixes[key] = prefix
                            elif prefix != prefixes[key]:
                                before,after = prefixes[key],prefix
                                differences = [(field,a,b) for field in before for a,b in
                                    zip(before[field],after[field]) if a!=b]
                                raise ValueError(f'A later liquidation objective changed the base-period path: '
                                                 f'{key}: {differences[:1]}')
                        body = _bytes(item)
                        member = f'fold-{f:04d}/{name}/{scenario}-{extension:02d}.json'
                        if archive is not None:
                            archive.writestr(member,body)
                        records.append(dict(**header,member=member,sha256=hashlib.sha256(body).hexdigest(),
                            financial_path_computed=item['financial_path_computed'],
                            model_nav_known=item['model_nav_known'],
                            modeled_terminal_cash_known=item['modeled_terminal_cash_known'],
                            model_nav_return=item['model_nav_return'],
                            modeled_terminal_cash_return=item['modeled_terminal_cash_return'],
                            economic_accounting_verified=False,actual_execution_verified=False,
                            invariant_violations=item.get('audit',{}).get('invariant_violations',[])))
    return dict(version=VERSION,expected_replays=len(CASES)*len(protocol['folds'])*2*len(EXTENSIONS),
        records=records,publication_coverage=coverage, invariant_failures=sum(bool(r['invariant_violations']) for r in records),
        outcome_counts=dict(Counter(('nav_known' if r['model_nav_known'] else 'nav_unknown') for r in records)),
        economic_profit_verified=False,accepted_for_signals=False)


def run_benchmarks(parent, publications, *, root=None):
    parent, publications = Path(parent).resolve(),Path(publications).resolve()
    private = (settings.DATA_DIR/'private').resolve()
    if not parent.is_relative_to(private) or not publications.is_relative_to(private):
        raise ValueError('Explicit private frozen inputs are required')
    review_suite(parent)
    contents = publications.read_bytes()
    if not 0 < len(contents) <= MAX_INPUT_BYTES:
        raise ValueError('Bounded native-publication input required')
    original = json.loads((parent/'protocol.json').read_bytes())
    if original['fees'] != dict(settings.FEES):
        raise ValueError('Fee assumptions differ from the frozen source study')
    required = {'buy_hold','control:liquidity:h1'}
    if not required <= {r['case'] for r in json.loads((parent/'results.json').read_bytes())['records']}:
        raise ValueError('Source study lacks the predeclared benchmark decision tapes')
    load_publications(contents,pd.read_parquet(parent/'registry.parquet'),
        json.loads((parent/'calendar.json').read_bytes()), original['plan']['end_exclusive'])
    base = Path(root or private/'strategy-benchmarks').resolve()
    if not base.is_relative_to(private):
        raise ValueError('Benchmark outputs must remain private')
    folder = base/f'BENCH-{now_tehran_iso()[:10]}-{uuid.uuid4().hex}'
    folder.mkdir(parents=True,exist_ok=False)
    (folder/'publications.json').write_bytes(contents)
    files = _freeze_code(folder/'code.zip')
    protocol = dict(version=VERSION,created_at=now_tehran_iso(),parent=str(parent),
        parent_manifest_sha256=_sha256(parent/'manifest.json'),source_files=files,
        code_sha256=research_code_fingerprint(),
        publications_sha256=hashlib.sha256(contents).hexdigest(), cases=list(CASES),
        liquidation_sessions=list(EXTENSIONS),
        universe='all initially eligible members of the frozen current registry, not verified historical full membership',
        passive_exit='first post-base session, retried within the fixed liquidation window',
        prefix_contract='equal known base-period values; optional null columns ignored, numerical equality',
        nav_basis='observed official close net assumed sell fee; synthetic units and noncash residuals',
        allocation='equal initial budgets; integer quantities and capacity; unused cash remains cash',
        initial_order='one t+1 DAY attempt, no top-up or replacement',
        rotation='original h1 liquidity tape; 10% entry circuit breaker; no new selections after source tape',
        passive_risk='entry circuit breaker has no effect after the sole initial decision; no loss_floor_exit',
        actual_order_tape_available=False,fees=dict(settings.FEES), historical_fees_verified=False,
        historical_membership_verified=False, economic_accounting_verified=False, actual_execution_verified=False,
        new_strategy_search=False,holdout_opened=False,prospective_independence=False,accepted_for_signals=False)
    write_state(folder/'protocol.json',protocol)
    write_state(folder/'state.json',dict(status='registered'))
    try:
        with DatabaseManager(folder/'experiments.db') as db:
            db.init_db()
            registered = db.log_experiment(experiment_id=folder.name+'-PROTOCOL',strategy_name=VERSION,
                symbols_tested='frozen initial pool',parameters_json=_bytes(protocol).decode(),metrics_json='{}',
                start_date=original['plan']['start'],end_date=original['plan']['end_exclusive'],
                dataset_version=protocol['parent_manifest_sha256'],features_version='frozen_decisions',model_version=VERSION)
            if registered is None:
                raise RuntimeError('Benchmark protocol registration failed')
            with zipfile.ZipFile(folder/'replays.zip','x',compression=zipfile.ZIP_DEFLATED) as archive:
                result = _evaluate(parent,contents,archive=archive)
            if not _sources_unchanged(files) or research_code_fingerprint()!=protocol['code_sha256']:
                raise RuntimeError('Source changed during benchmark evaluation')
            write_state(folder/'results.json',result)
            logged = db.log_experiment(experiment_id=folder.name,strategy_name=VERSION,symbols_tested='frozen initial pool',
                parameters_json=_bytes(protocol).decode(),metrics_json=_bytes(dict(replays=len(result['records']),
                    invariant_failures=result['invariant_failures'])).decode(),start_date=original['plan']['start'],
                end_date=original['plan']['end_exclusive'],dataset_version=protocol['parent_manifest_sha256'],
                features_version='frozen_decisions',model_version=VERSION)
            if logged is None:
                raise RuntimeError('Benchmark result registration failed')
    except BaseException as error:
        write_state(folder/'state.json',dict(status='failed',error=f'{type(error).__name__}: {error}',
                                            finished_at=now_tehran_iso()))
        raise
    write_state(folder/'state.json',dict(status='complete' if not result['invariant_failures'] else 'failed'))
    write_state(folder/'manifest.json',{p.name:_sha256(p) for p in folder.iterdir() if p.is_file()})
    return dict(folder=str(folder),**result)


def review_benchmarks(folder, *, parent_override=None, reexecute=False):
    folder = Path(folder).resolve()
    if not folder.is_relative_to((settings.DATA_DIR/'private').resolve()):
        raise ValueError('An explicit private benchmark study is required')
    manifest = json.loads((folder/'manifest.json').read_bytes())
    if (set(manifest) != {p.name for p in folder.iterdir() if p.is_file() and p.name!='manifest.json'}
            or any(Path(n).name!=n or _sha256(folder/n)!=h for n,h in manifest.items())):
        raise ValueError('Benchmark evidence manifest mismatch')
    protocol,result = (json.loads((folder/n).read_bytes()) for n in ('protocol.json','results.json'))
    if (protocol['version']!=VERSION or result['version']!=VERSION
            or json.loads((folder/'state.json').read_bytes())['status']!='complete'
            or tuple(protocol['cases'])!=CASES or tuple(protocol['liquidation_sessions'])!=EXTENSIONS):
        raise ValueError('Incomplete or unsupported benchmark contract')
    parent = Path(parent_override or protocol['parent']).resolve()
    if _sha256(parent/'manifest.json')!=protocol['parent_manifest_sha256']:
        raise ValueError('Benchmark parent identity mismatch')
    review_suite(parent)
    if _sha256(folder/'publications.json')!=protocol['publications_sha256']:
        raise ValueError('Benchmark native evidence changed')
    parent_protocol = json.loads((parent/'protocol.json').read_bytes())
    expected = {(c,f,s,n) for c in CASES for f in range(len(parent_protocol['folds']))
                for s in SCENARIOS for n in EXTENSIONS}
    records = result['records']
    if (len(records)!=len(expected) or result['expected_replays']!=len(expected)
            or {(r['case'],r['fold'],r['scenario'],r['liquidation_sessions']) for r in records}!=expected
            or result['invariant_failures']!=sum(bool(r['invariant_violations']) for r in records)
            or result['outcome_counts']!=dict(Counter('nav_known' if r['model_nav_known'] else 'nav_unknown'
                                                     for r in records))):
        raise ValueError('Benchmark denominator mismatch')
    with zipfile.ZipFile(folder/'code.zip') as code:
        if (len(code.namelist())!=len(protocol['source_files']) or set(code.namelist())!=set(protocol['source_files'])
                or any(hashlib.sha256(code.read(n)).hexdigest()!=h for n,h in protocol['source_files'].items())):
            raise ValueError('Benchmark source archive mismatch')
    with zipfile.ZipFile(folder/'replays.zip') as paths:
        if len(paths.namelist())!=len(expected) or set(paths.namelist())!={r['member'] for r in records}:
            raise ValueError('Benchmark saved replay denominator mismatch')
        for r in records:
            body=paths.read(r['member'])
            item=json.loads(body)
            if (hashlib.sha256(body).hexdigest()!=r['sha256'] or any(item[k]!=r[k]
                    for k in ('case','fold','scenario','liquidation_sessions','picks_sha256','model_nav_return',
                              'modeled_terminal_cash_return','model_nav_known','modeled_terminal_cash_known'))):
                raise ValueError('Benchmark saved path identity or outcome mismatch')
            if any(item[k] or r[k] for k in ('economic_accounting_verified','actual_execution_verified')):
                raise ValueError('The daily synthetic benchmark cannot certify economic accounting or actual fills')
        if reexecute:
            if (dict(settings.FEES)!=protocol['fees'] or not _sources_unchanged(protocol['source_files'])
                    or research_code_fingerprint()!=protocol['code_sha256']):
                raise ValueError('Exact frozen benchmark runtime is required')
            actual = _evaluate(parent,(folder/'publications.json').read_bytes())
            if _bytes(actual)!=_bytes(result):
                raise ValueError('Financial benchmark replay did not reproduce')
    return dict(status='verified_benchmark_archive',replays=len(expected),
        financial_reexecution_performed=reexecute,economic_profit_verified=False,accepted_for_signals=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--parent')
    action.add_argument('--review')
    parser.add_argument('--publications')
    parser.add_argument('--output-root')
    parser.add_argument('--reexecute',action='store_true')
    parser.add_argument('--parent-override')
    args = parser.parse_args()
    if args.review:
        result = review_benchmarks(args.review,parent_override=args.parent_override,reexecute=args.reexecute)
    else:
        if not args.publications or args.reexecute or args.parent_override:
            parser.error('Run requires --publications; replay options belong to --review')
        result = run_benchmarks(args.parent,args.publications,root=args.output_root)
    print(json.dumps({k:v for k,v in result.items() if k not in {'records','publication_coverage'}},
                     ensure_ascii=True,indent=2))
    return 1 if result.get('invariant_failures',0) else 0


if __name__=='__main__':
    raise SystemExit(main())
