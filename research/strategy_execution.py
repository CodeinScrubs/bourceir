"""Execution-only follow-up of frozen decisions using dated native publications.

Raw feature inputs and all original decision tapes remain unchanged. Native
zero-activity evidence may resolve an unfilled entry or a marked holding day;
it never creates a candle, a fill, a dividend or a share credit.
"""

import base64
import hashlib
import json
import os
import tempfile
import uuid
import zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from backtest.evidence import audit_daily_replay
from backtest.portfolio import replay_portfolio
from backtest.prepared import prepare_portfolio_prices
from config import settings
from core.daily_publications import parse_publication
from core.no_trade_valuation import parse_no_trade_publication
from core.timeutils import now_tehran_iso
from database.db import DatabaseManager
from features.provenance import feature_environment
from ops.validation_run import write_state
from research.replay_cache import ReplayCache
from research.run import _json_safe, _sha256, research_code_fingerprint
from research.search_ledger import DEFAULT_ATTEMPT_BUDGET, finish_trials, reserve_trials, validate_registration
from research.strategy_suite import (
    SCENARIOS,
    SuitePlan,
    _bytes,
    _configuration,
    _decision_diagnostics,
    _frame_hash,
    _freeze_code,
    _restore_cases,
    _sources_unchanged,
    review_suite,
    summarize_records,
)
from research.walkforward import preceding_price_observations

VERSION = 'saved-decisions-native-execution-v1'
MAX_INPUT_BYTES = 64 * 1024 * 1024


def load_publications(contents, registry, calendar, cutoff):
    """Reject unsupported wrappers; classify every response, including unusable ones."""
    if not isinstance(contents, bytes) or not 0 < len(contents) <= MAX_INPUT_BYTES:
        raise ValueError('A bounded native-publication input is required')
    payload = json.loads(contents)
    if (not isinstance(payload, dict) or payload.get('kind') != 'priority_missing_session_publications'
            or not isinstance(payload.get('records'), list) or len(payload['records']) > 12000):
        raise ValueError('Versioned priority-publication records required')
    identities = dict(zip(registry.symbol_fa, registry.ins_code, strict=True))
    if len(identities) != len(registry) or len(set(identities.values())) != len(registry):
        raise ValueError('Unique original instrument identities required')
    entries, marks, rows, seen = [], [], [], set()
    for record in payload['records']:
        request = record.get('request', {})
        symbol, code, day = (request.get(key) for key in ('symbol', 'ins_code', 'date'))
        if identities.get(symbol) != code or day not in calendar or day >= cutoff or (code, day) in seen:
            raise ValueError('Publication identity/date is outside the original registry/calendar or duplicated')
        seen.add((code, day))
        source = record.get('observation')
        if source is None:
            rows.append(dict(symbol=symbol, ins_code=code, date=day, status='source_unavailable'))
            continue
        if not isinstance(source, dict) or not isinstance(source.get('content_base64'), str):
            raise ValueError('Native source bytes are required')
        observation = {k: v for k, v in source.items() if k != 'content_base64'}
        try:
            observation['content'] = base64.b64decode(source['content_base64'], validate=True)
            typed = parse_publication(observation, code, day, 'price')
            if observation.get('http_status') != 200:
                raise ValueError('Native source did not return a complete HTTP 200 response')
            flat = parse_no_trade_publication(observation, code, day)
        except ValueError as error:
            rows.append(dict(symbol=symbol, ins_code=code, date=day,
                             status='unverified_native_response', reason=str(error)))
            continue
        if typed['status'] == 'observed_zero_activity':
            entries.append(observation)
        if flat['flat_mark_eligible']:
            marks.append(observation)
        rows.append(dict(symbol=symbol, ins_code=code, date=day, status=typed['status'],
            flat_mark_eligible=flat['flat_mark_eligible'], body_sha256=source['body_sha256'],
            positive_candle_quarantined=typed['status'] == 'valid_price'))
    return entries, marks, rows


def _scope(parent_protocol, protocol):
    cases = _restore_cases(parent_protocol)
    names = protocol.get('evaluation_case_names', [case.name for case in cases])
    indices = protocol.get('evaluation_fold_indices', list(range(len(parent_protocol['folds']))))
    if (not isinstance(names, list) or not names or not all(isinstance(name, str) for name in names)
            or len(set(names)) != len(names)
            or not set(names) <= {case.name for case in cases}
            or not isinstance(indices, list) or not indices
            or any(type(index) is not int or not 0 <= index < len(parent_protocol['folds']) for index in indices)
            or indices != sorted(set(indices))):
        raise ValueError('An explicit nonempty registered case/fold scope is required')
    selected = [case for case in cases if case.name in names]
    if [case.name for case in selected] != names:
        raise ValueError('Subset case order must preserve the original registered catalogue')
    return selected, indices


def _evaluate(parent, protocol, contents, *, archive=None, expected=None, progress=None, fold_subset=None):
    parent_protocol = json.loads((parent / 'protocol.json').read_bytes())
    source_result = json.loads((parent / 'results.json').read_bytes())
    plan = SuitePlan(**parent_protocol['plan'])
    cases, indices = _scope(parent_protocol, protocol)
    folds = parent_protocol['folds']
    selected_folds = indices if fold_subset is None else list(fold_subset)
    if not set(selected_folds) <= set(indices):
        raise ValueError('Worker folds differ from the registered scope')
    prices = pd.read_parquet(parent / 'prices.parquet')
    registry = pd.read_parquet(parent / 'registry.parquet')
    calendar = json.loads((parent / 'calendar.json').read_bytes())
    entries, marks, coverage = load_publications(contents, registry, calendar, plan.end_exclusive)
    identities = dict(zip(registry.symbol_fa, registry.ins_code, strict=True))
    inverse = {code: symbol for symbol, code in identities.items()}
    # Ranking stays on the original immutable names/tapes. Only execution keys
    # change to the stable native IDs to which publisher receipts are bound.
    prices = prices.assign(symbol=prices.symbol.map(identities))
    records, residual = [], {}
    with zipfile.ZipFile(parent / 'replays.zip') as original:
        lookup = {(r['fold'], r['case'], r['scenario']): r for r in source_result['records']}
        for i in selected_folds:
            fold = folds[i]
            days = [d for d in calendar if fold['test_start'] <= d < fold['test_end_exclusive']]
            raw = pd.concat([preceding_price_observations(prices, fold['test_start']),
                prices.loc[prices.date.ge(fold['test_start']) & prices.date.lt(fold['test_end_exclusive'])]], ignore_index=True)
            prepared = prepare_portfolio_prices(raw)
            for j, case in enumerate(cases):
                normal = json.loads(original.read(lookup[i, case.name, 'normal']['member']))
                selected = pd.DataFrame(normal['picks'], columns=['date', 'symbol', 'rank'])
                if _frame_hash(selected) != lookup[i, case.name, 'normal']['picks_sha256']:
                    raise ValueError('Original full decision tape did not reproduce')
                native = selected.assign(symbol=selected.symbol.map(identities))
                if native.symbol.isna().any():
                    raise ValueError('Original decision is not in its native identity registry')
                scope = set(native.symbol)
                entry_evidence = [o for o in entries if o['ins_code'] in scope
                    and fold['test_start'] <= o['requested_date'] < fold['test_end_exclusive']]
                mark_evidence = [o for o in marks if o['ins_code'] in scope
                    and fold['test_start'] <= o['requested_date'] < fold['test_end_exclusive']]
                cfg = normal['assumptions']['config']
                for scenario in SCENARIOS:
                    configuration = _configuration(case, plan, scenario, cfg['horizon'], cfg['max_positions'])
                    result = replay_portfolio(prepared, native, calendar, configuration,
                        start=fold['test_start'], end=fold['test_end_exclusive'], dip_rule=case.dip_rule,
                        entry_allocation_fraction=case.allocation, loss_floor_exit=case.family == 'surge',
                        no_trade_entry_observations=entry_evidence, no_trade_valuation_observations=mark_evidence)
                    audit = audit_daily_replay(result, prepared, native, days,
                        no_trade_valuation_observations=mark_evidence)
                    item = dict(case=case.name, fold=i, scenario=scenario, metrics=result.metrics,
                        assumptions=result.assumptions, audit=audit, picks=native.to_dict('records'),
                        equity=result.equity.to_dict('records'), executions=result.executions.to_dict('records'),
                        decisions=result.decisions.to_dict('records'))
                    body = _bytes(item)
                    member = f'fold-{i:04d}/case-{j:04d}/{scenario}.json'
                    if archive is not None:
                        archive.writestr(member, body)
                    if expected is not None and expected.read(member) != body:
                        raise ValueError('Native-evidence execution replay did not reproduce exactly')
                    records.append(dict(case=case.name, fold=i, scenario=scenario, member=member,
                        sha256=hashlib.sha256(body).hexdigest(), picks_sha256=_frame_hash(native), audit=audit,
                        original_picks_sha256=lookup[i, case.name, scenario]['picks_sha256'],
                        decision_diagnostics=_json_safe(_decision_diagnostics(result))))
                    if result.metrics['status'] != 'complete' and not result.decisions.empty:
                        failure = result.decisions.iloc[-1].to_dict()
                        code, day = failure.get('symbol'), failure.get('date')
                        if code in inverse and day in calendar:
                            key = code, day, failure['status']
                            own = residual.setdefault(key, dict(ins_code=code, symbol=inverse[code], date=day,
                                reason=failure['status'], affected_replays=0))
                            own['affected_replays'] += 1
                if progress and ((j+1) % 25 == 0 or j+1 == len(cases)):
                    progress(dict(stage='native_execution', fold=selected_folds.index(i)+1, fold_id=i, folds=len(selected_folds),
                                  cases_done=j+1, cases=len(cases)))
    return _evaluation_result(records, cases, len(selected_folds), coverage, list(residual.values()))


def _evaluation_result(records, cases, fold_count, coverage, residual):
    return dict(records=records, summary=summarize_records(records, cases, fold_count),
        publication_coverage=coverage, residual_requests=residual,
        expected_replays=len(cases)*fold_count*len(SCENARIOS),
        invariant_failures=sum(bool(r['audit']['invariant_violations']) for r in records),
        accepted_for_signals=False, economic_profit_verified=False)


def _fold_job(parent, protocol, contents, index, path, fees, cutoff):
    if dict(settings.FEES) != fees or settings.RESEARCH_CONFIG['holdout_start'] != cutoff:
        raise ValueError('Parallel worker financial settings differ from the registered run')
    with zipfile.ZipFile(path, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        return _evaluate(parent, protocol, contents, archive=archive, fold_subset=(index,))


def _execution_contract(protocol):
    result = {key: protocol[key] for key in ('version', 'parent_manifest_sha256', 'publications_sha256',
        'code_sha256', 'environment', 'fees', 'holdout_end_exclusive')}
    for key in ('evaluation_case_names', 'evaluation_fold_indices'):
        if key in protocol:
            result[key] = protocol[key]
    return result


def _checkpoint_inputs_unchanged(parent, protocol):
    manifest = json.loads((parent / 'manifest.json').read_bytes())
    names = ('protocol.json', 'results.json', 'prices.parquet', 'registry.parquet', 'calendar.json', 'replays.zip')
    if (_sha256(parent / 'manifest.json') != protocol['parent_manifest_sha256']
            or any(_sha256(parent / name) != manifest[name] for name in names)
            or research_code_fingerprint() != protocol['code_sha256']
            or dict(settings.FEES) != protocol['fees']
            or settings.RESEARCH_CONFIG['holdout_start'] != protocol['holdout_end_exclusive']
            or feature_environment() != protocol['environment']):
        raise ValueError('Checkpoint source/data changed; cached result is not admitted')


def _cached_fold_job(parent, protocol, contents, index, path, fees, cutoff, cache_root, private_root):
    if (dict(settings.FEES) != fees or fees != protocol['fees']
            or settings.RESEARCH_CONFIG['holdout_start'] != cutoff or cutoff != protocol['holdout_end_exclusive']):
        raise ValueError('Checkpoint worker financial settings differ from the registered run')
    if feature_environment() != protocol['environment']:
        raise ValueError('Checkpoint worker environment differs from the registered run')
    _checkpoint_inputs_unchanged(parent, protocol)
    if hashlib.sha256(contents).hexdigest() != protocol['publications_sha256']:
        raise ValueError('Checkpoint publication bytes changed')
    def compute(destination):
        result = _fold_job(parent, protocol, contents, index, destination, fees, cutoff)
        _checkpoint_inputs_unchanged(parent, protocol)
        return json.loads(_bytes(result))
    result, hit = ReplayCache(cache_root, private_root=private_root).run(
        dict(contract=_execution_contract(protocol), fold=index), path, compute)
    return dict(result=result, cache_hit=hit)


def _evaluate_parallel(parent, parent_protocol, protocol, contents, archive, progress, workers, cache_root=None):
    """Evaluate the same saved tapes by independent fold, then merge in serial order."""
    cases, indices = _scope(parent_protocol, protocol)
    count = len(indices)
    partials = {}
    cache_hits = 0
    with tempfile.TemporaryDirectory(prefix='brs-folds-', dir=Path(archive.filename).parent) as directory:
        paths = {index: Path(directory)/f'fold-{index}.zip' for index in indices}
        if cache_root is not None and workers == 1:
            for index, path in paths.items():
                checkpoint = _cached_fold_job(parent, protocol, contents, index, path, dict(settings.FEES),
                    settings.RESEARCH_CONFIG['holdout_start'], cache_root, settings.DATA_DIR / 'private')
                partials[index] = checkpoint['result']
                cache_hits += int(checkpoint['cache_hit'])
                if progress:
                    progress(dict(stage='native_execution_fold_complete', fold=len(partials), fold_id=index, folds=count,
                                  completed_folds=len(partials), cases_done=len(cases), cache_hit=checkpoint['cache_hit']))
        else:
            with ProcessPoolExecutor(max_workers=min(workers, count)) as pool:
                jobs = {}
                for i, path in paths.items():
                    args = (parent, protocol, contents, i, path, dict(settings.FEES), settings.RESEARCH_CONFIG['holdout_start'])
                    job = (pool.submit(_fold_job, *args) if cache_root is None else
                           pool.submit(_cached_fold_job, *args, cache_root, settings.DATA_DIR / 'private'))
                    jobs[job] = i
                for job in as_completed(jobs):
                    index = jobs[job]
                    result = job.result()
                    partials[index] = result if cache_root is None else result['result']
                    cache_hits += int(cache_root is not None and result['cache_hit'])
                    if progress:
                        progress(dict(stage='native_execution_fold_complete', fold=len(partials), fold_id=index, folds=count,
                                      completed_folds=len(partials), cases_done=len(cases)))
        records, residual = [], {}
        coverage = partials[indices[0]]['publication_coverage']
        for index,path in paths.items():
            partial = partials[index]
            expected = {(case.name,index,scenario) for case in cases for scenario in SCENARIOS}
            own = partial['records']
            if (len(own) != len(expected) or {(r['case'],r['fold'],r['scenario']) for r in own} != expected
                    or partial['publication_coverage'] != coverage
                    or partial['summary'] != summarize_records(own, cases, 1)
                    or any(row['member'] != f"fold-{index:04d}/case-{[c.name for c in cases].index(row['case']):04d}/{row['scenario']}.json"
                           for row in own)):
                raise ValueError('Parallel fold replay denominator or publication coverage changed')
            with zipfile.ZipFile(path) as part:
                if len(part.namelist()) != len(own) or set(part.namelist()) != {r['member'] for r in own}:
                    raise ValueError('Parallel fold archive has incomplete or duplicate members')
                for row in own:
                    body = part.read(row['member'])
                    if hashlib.sha256(body).hexdigest() != row['sha256']:
                        raise ValueError('Parallel fold replay hash mismatch')
                    archive.writestr(row['member'], body)
            records.extend(own)
            for row in partial['residual_requests']:
                key = row['ins_code'],row['date'],row['reason']
                total = residual.setdefault(key, dict(row, affected_replays=0))
                total['affected_replays'] += row['affected_replays']
    result = _evaluation_result(records, cases, count, coverage, list(residual.values()))
    result['computation'] = dict(cache_enabled=cache_root is not None, reused_folds=cache_hits,
        recomputed_folds=count-cache_hits, new_independent_evidence=False)
    return result


def run_execution_followup(parent, publications, *, root=None, progress=None, workers=1,
                           use_cache=True, allow_repeat=False, search_attempt_budget=DEFAULT_ATTEMPT_BUDGET,
                           case_names=None, fold_indices=None, max_replays=10000):
    if (type(use_cache) is not bool or type(allow_repeat) is not bool
            or type(search_attempt_budget) is not int or search_attempt_budget < 1
            or type(max_replays) is not int or max_replays < 1):
        raise ValueError('Explicit cache/repeat booleans and positive search attempt budget are required')
    tracking = {}
    try:
        result = _run_execution_followup(parent, publications, root=root, progress=progress, workers=workers,
            use_cache=use_cache, allow_repeat=allow_repeat, search_attempt_budget=search_attempt_budget, tracking=tracking,
            case_names=case_names, fold_indices=fold_indices, max_replays=max_replays)
    except BaseException as exc:
        if tracking.get('registration'):
            try:
                finish_trials(tracking['registration'], status='interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed',
                    evidence=dict(error_type=type(exc).__name__, financial_verdict=None))
            except Exception as ledger_error:
                exc.add_note(f'Search termination not recorded: {type(ledger_error).__name__}; inspect unfinished attempts')
        raise
    finish_trials(tracking['registration'], status='complete', evidence=dict(
        manifest_sha256=_sha256(Path(result['folder']) / 'manifest.json'), financial_verdict=None, accepted_for_signals=False))
    return result


def _run_execution_followup(parent, publications, *, root, progress, workers, use_cache, allow_repeat,
                            search_attempt_budget, tracking, case_names, fold_indices, max_replays):
    if type(workers) is not int or not 1 <= workers <= 4:
        raise ValueError('Worker count must be an integer between 1 and 4')
    parent = Path(parent).resolve()
    review_suite(parent)
    source = Path(publications).resolve()
    if not source.is_relative_to((settings.DATA_DIR / 'private').resolve()):
        raise ValueError('Native source input must be private and explicit')
    with source.open('rb') as stream:
        contents = stream.read(MAX_INPUT_BYTES + 1)
    if len(contents) > MAX_INPUT_BYTES:
        raise ValueError('Native source input exceeds its size bound')
    parent_protocol = json.loads((parent / 'protocol.json').read_bytes())
    original_names = [c.name for c in _restore_cases(parent_protocol)]
    if case_names is not None:
        if (not isinstance(case_names, list) or not case_names or not all(isinstance(name, str) for name in case_names)
                or len(set(case_names)) != len(case_names) or not set(case_names) <= set(original_names)):
            raise ValueError('A nonempty unique subset of original case names is required')
        case_names = [name for name in original_names if name in case_names]
    scope = dict(evaluation_case_names=original_names if case_names is None else case_names,
                 evaluation_fold_indices=list(range(len(parent_protocol['folds']))) if fold_indices is None else fold_indices)
    selected_cases, selected_folds = _scope(parent_protocol, scope)
    if len(selected_cases) * len(selected_folds) * len(SCENARIOS) > max_replays:
        raise ValueError('Execution replay budget exceeded before computation')
    if parent_protocol['fees'] != dict(settings.FEES):
        raise ValueError('Fee assumptions differ from the frozen source study')
    # Validation occurs before creating or registering the new study.
    load_publications(contents, pd.read_parquet(parent / 'registry.parquet'),
        json.loads((parent / 'calendar.json').read_bytes()), parent_protocol['plan']['end_exclusive'])
    base = Path(root or parent.parent).resolve()
    if not base.is_relative_to((settings.DATA_DIR / 'private').resolve()):
        raise ValueError('Execution follow-up must remain private')
    run_id = f'EXEC-{now_tehran_iso()[:10]}-{uuid.uuid4().hex}'
    folder = base / run_id
    folder.mkdir(parents=True, exist_ok=False)
    (folder / 'publications.json').write_bytes(contents)
    protocol = dict(version=VERSION, run_id=run_id, created_at=now_tehran_iso(), parent=str(parent),
        parent_manifest_sha256=_sha256(parent / 'manifest.json'), publications_sha256=hashlib.sha256(contents).hexdigest(),
        code_sha256=research_code_fingerprint(), source_files=_freeze_code(folder / 'code.zip'),
        environment=feature_environment(), fees=dict(settings.FEES),
        holdout_end_exclusive=settings.RESEARCH_CONFIG['holdout_start'],
        feature_or_decision_retraining=False, raw_price_amendments=False,
        execution_keys='native ins_code; original name-based ranking retained',
        missing_held_mark_policy='dated continuous flat mark required; otherwise unknown',
        positive_native_candles='quarantined; no raw/feature vintage mixing',
        evaluation_workers=workers, checkpoint_cache_enabled=use_cache,
        prospective_independence=False, accepted_for_signals=False, max_replays=max_replays, **scope)
    cases = selected_cases
    policies = [dict(name=case.name, policy={k:v for k,v in asdict(case).items() if k != 'name'}) for case in cases]
    registration = reserve_trials(_execution_contract(protocol), policies, run_id=run_id,
        allow_repeat=allow_repeat, attempt_budget=search_attempt_budget)
    tracking['registration'] = registration
    protocol['local_search_registration'] = registration
    write_state(folder / 'protocol.json', protocol)
    write_state(folder / 'state.json', dict(status='registered', run_id=run_id))
    with DatabaseManager(folder / 'experiments.db') as db:
        db.init_db()
        registered = db.log_experiment(experiment_id=run_id+'-PROTOCOL', strategy_name=VERSION,
            symbols_tested='original-suite', parameters_json=_bytes(protocol).decode(), metrics_json='{}',
            start_date=parent_protocol['plan']['start'], end_date=parent_protocol['plan']['end_exclusive'],
            dataset_version=protocol['parent_manifest_sha256'], features_version='frozen_decisions', model_version=VERSION)
        if registered is None:
            raise RuntimeError('Execution protocol registration failed')
        with zipfile.ZipFile(folder / 'replays.zip', 'x', compression=zipfile.ZIP_DEFLATED) as archive:
            if use_cache:
                result = _evaluate_parallel(parent, parent_protocol, protocol, contents, archive, progress, workers,
                    cache_root=settings.DATA_DIR / 'private' / 'research-cache' / 'execution-folds')
            elif workers == 1 or len(parent_protocol['folds']) == 1:
                result = _evaluate(parent, protocol, contents, archive=archive, progress=progress)
            else:
                result = _evaluate_parallel(parent, parent_protocol, protocol, contents, archive, progress, workers)
        if research_code_fingerprint() != protocol['code_sha256'] or not _sources_unchanged(protocol['source_files']):
            raise RuntimeError('Source changed during execution follow-up')
        _checkpoint_inputs_unchanged(parent, protocol)
        result.update(version=VERSION, run_id=run_id)
        write_state(folder / 'results.json', json.loads(_bytes(result)))
        logged = db.log_experiment(experiment_id=run_id, strategy_name=VERSION, symbols_tested='original-suite',
            parameters_json=_bytes(protocol).decode(), metrics_json=_bytes(dict(replays=len(result['records']),
                invariant_failures=result['invariant_failures'])).decode(), start_date=parent_protocol['plan']['start'],
            end_date=parent_protocol['plan']['end_exclusive'], dataset_version=protocol['parent_manifest_sha256'],
            features_version='frozen_decisions', model_version=VERSION)
        if logged is None:
            raise RuntimeError('Execution result registration failed')
    for path in folder.iterdir():
        if path.is_file():
            with path.open('r+b') as stream:
                os.fsync(stream.fileno())
    write_state(folder / 'state.json', dict(status='complete', run_id=run_id))
    write_state(folder / 'manifest.json', {p.name: _sha256(p) for p in folder.iterdir() if p.is_file()})
    return dict(folder=str(folder), **result)


def review_execution_followup(folder, *, reexecute=False, progress=None, parent_override=None):
    folder = Path(folder).resolve()
    if not folder.is_relative_to((settings.DATA_DIR / 'private').resolve()):
        raise ValueError('A private execution study is required')
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    if (set(manifest) != {p.name for p in folder.iterdir() if p.is_file() and p.name != 'manifest.json'}
            or any(Path(name).name != name or _sha256(folder/name) != digest for name, digest in manifest.items())):
        raise ValueError('Execution evidence manifest mismatch')
    protocol = json.loads((folder / 'protocol.json').read_bytes())
    result = json.loads((folder / 'results.json').read_bytes())
    state = json.loads((folder/'state.json').read_bytes())
    if (protocol['version'] != VERSION or result['version'] != VERSION
            or state.get('status') != 'complete' or not protocol['run_id'] == result['run_id'] == state['run_id']):
        raise ValueError('Unsupported execution study')
    # Relocation is explicit and must match the originally bound manifest.
    # The saved protocol is never rewritten to point at a convenient dataset.
    parent = Path(parent_override if parent_override is not None else protocol['parent']).resolve()
    if not (parent/'manifest.json').is_file() or _sha256(parent/'manifest.json') != protocol['parent_manifest_sha256']:
        raise ValueError('Execution parent manifest identity changed or is unavailable')
    review_suite(parent)
    if (_sha256(parent/'manifest.json') != protocol['parent_manifest_sha256']
            or _sha256(folder/'publications.json') != protocol['publications_sha256']):
        raise ValueError('Execution source identity changed')
    parent_protocol = json.loads((parent/'protocol.json').read_bytes())
    cases, selected_folds = _scope(parent_protocol, protocol)
    if 'local_search_registration' in protocol:
        registration = protocol['local_search_registration']
        if registration.get('run_id') != protocol['run_id']:
            raise ValueError('Search registration run differs from execution study')
        validate_registration(_execution_contract(protocol),
            [dict(name=case.name, policy={k:v for k,v in asdict(case).items() if k != 'name'}) for case in cases], registration)
    expected_keys = {(c.name, i, s) for c in cases for i in selected_folds for s in SCENARIOS}
    if 'max_replays' in protocol and (type(protocol['max_replays']) is not int or protocol['max_replays'] < len(expected_keys)):
        raise ValueError('Registered execution replay budget is inconsistent')
    records = result['records']
    if 'computation' in result:
        stats = result['computation']
        if (set(stats) != {'cache_enabled', 'reused_folds', 'recomputed_folds', 'new_independent_evidence'}
                or type(stats['cache_enabled']) is not bool or stats['new_independent_evidence'] is not False
                or any(type(stats[k]) is not int or stats[k] < 0 for k in ('reused_folds', 'recomputed_folds'))
                or stats['reused_folds'] + stats['recomputed_folds'] != len(selected_folds)
                or stats['cache_enabled'] != protocol.get('checkpoint_cache_enabled', False)
                or not stats['cache_enabled'] and stats['reused_folds'] != 0):
            raise ValueError('Checkpoint computational counts are inconsistent')
    if (len(records) != len(expected_keys) or result['expected_replays'] != len(expected_keys)
            or {(r['case'], r['fold'], r['scenario']) for r in records} != expected_keys
            or summarize_records(records, cases, len(selected_folds)) != result['summary']
            or sum(bool(r['audit']['invariant_violations']) for r in records) != result['invariant_failures']):
        raise ValueError('Execution denominator or summary is inconsistent')
    with zipfile.ZipFile(folder/'code.zip') as archive:
        if (len(archive.namelist()) != len(protocol['source_files'])
                or set(archive.namelist()) != set(protocol['source_files'])
                or any(hashlib.sha256(archive.read(n)).hexdigest() != h for n, h in protocol['source_files'].items())):
            raise ValueError('Execution source archive mismatch')
    with zipfile.ZipFile(folder/'replays.zip') as archive:
        if (len(archive.namelist()) != result['expected_replays']
                or {r['member'] for r in result['records']} != set(archive.namelist())
                or any(hashlib.sha256(archive.read(r['member'])).hexdigest() != r['sha256'] for r in result['records'])):
            raise ValueError('Execution replay member mismatch')
        if reexecute:
            if research_code_fingerprint() != protocol['code_sha256'] or not _sources_unchanged(protocol['source_files']):
                raise ValueError('Exact execution source is required')
            actual = _evaluate(parent, protocol, (folder/'publications.json').read_bytes(), expected=archive, progress=progress)
            actual.update(version=VERSION, run_id=protocol['run_id'])
            if 'computation' in result:
                # These are declared execution telemetry, never independent financial evidence.
                actual['computation'] = result['computation']
            if _bytes(actual) != _bytes(result):
                raise ValueError('Execution summary did not reproduce')
            if research_code_fingerprint() != protocol['code_sha256'] or not _sources_unchanged(protocol['source_files']):
                raise ValueError('Source changed during execution review')
    return dict(status='verified_execution_archive', replays=result['expected_replays'],
        invariant_failures=result['invariant_failures'], financial_reexecution_performed=reexecute,
        accepted_for_signals=False, economic_profit_verified=False)
