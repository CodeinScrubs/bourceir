"""Registered paired exit-policy experiment on a frozen holding-horizon case."""

import argparse
import json
import re
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from backtest.portfolio import RISK_EXIT_VERSION, PortfolioConfig, replay_portfolio
from config import settings
from core.capital_capacity import diagnose_cash_ledger, summarize_cases
from core.timeutils import now_tehran_iso
from ops.research_review import _load_json, _relative_file, _verify_inventory, read_development_parquet
from research.holding_horizon import (
    VERSION as PARENT_VERSION,
)
from research.holding_horizon import (
    _fold_inputs,
    _jobs,
    _modes,
    _panel,
    _past_baseline,
    _same_frame,
    fold_picks,
    review_holding_study,
)
from research.labels import market_calendar
from research.portfolio_run import verified_baseline_inputs
from research.run import _json_safe, _sha256, archive_research_code, research_code_fingerprint
from research.walkforward import TimeFold

VERSION = 'paired-risk-exit-study-v1'
SUFFIX = '-risk-exit'
REGISTRY_COLUMNS = ('id', 'experiment_id', 'created_at', 'strategy_name', 'symbols_tested', 'start_date',
                    'end_date', 'dataset_version', 'features_version', 'model_version',
                    'parameters_json', 'metrics_json', 'status')


def _write(path, value):
    path.write_text(json.dumps(_json_safe(value), ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


@contextmanager
def _registry(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    conn.set_authorizer(lambda action, table, *rest: sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_READ and table != 'experiments' else sqlite3.SQLITE_OK)
    conn.execute('BEGIN')
    try:
        yield SimpleNamespace(conn=conn)
    finally:
        conn.rollback()
        conn.close()


def _parent_files(parent, payload):
    p = payload['protocol']
    if p['version'] != PARENT_VERSION or payload['accepted_for_signals'] is not False or payload['holdout_eligible'] is not False:
        raise ValueError('مطالعه والد باید همان خانواده محفوظ و بدون پذیرش باشد')
    _verify_inventory(parent/'code', p['source_files'])
    if any(not name.startswith('source/') for name in p['input_files']):
        raise ValueError('مسیر ورودی والد نامعتبر است')
    _verify_inventory(parent/'source', {k.removeprefix('source/'): v for k, v in p['input_files'].items()})
    _verify_inventory(parent, payload['artifacts_sha256'], parquet=True)
    names = {'result.json', 'protocol.json', *payload['checkpoints_sha256'],
             *payload['artifacts_sha256'], *p['input_files'], *('code/'+name for name in p['source_files'])}
    return {name: _sha256(_relative_file(parent, name)) for name in sorted(names)}


def _export_registry(source_db, target, payload):
    rows = []
    with _registry(source_db) as registry:
        for identifier, metrics in ((payload['experiment_id']+'-PROTOCOL', {}),
                                    (payload['experiment_id'], payload['summary'])):
            row = registry.conn.execute('SELECT '+','.join(REGISTRY_COLUMNS)+' FROM experiments WHERE experiment_id=?',
                                        (identifier,)).fetchone()
            if (row is None or row['status'] != 'completed'
                    or json.loads(row['parameters_json']) != payload['protocol']
                    or json.loads(row['metrics_json']) != metrics):
                raise ValueError('ثبت اصلی والد با فایل آن برابر نیست')
            rows.append(tuple(row))
    # This is a new evidence snapshot, not an application DB or a re-registration of the old study.
    if target.exists():
        raise ValueError('آرشیو ثبت والد موجود است؛ بازنویسی مجاز نیست')
    with sqlite3.connect(target) as dest:
        dest.execute('CREATE TABLE experiments ('+','.join(name+(' INTEGER' if name == 'id' else ' TEXT')
                                                         for name in REGISTRY_COLUMNS)+')')
        dest.executemany('INSERT INTO experiments VALUES ('+','.join('?' for _ in REGISTRY_COLUMNS)+')', rows)
    return _sha256(target)


def _all_modes():
    return _modes()+[name+SUFFIX for name in _modes()]


def _summaries(records):
    summary = {mode: summarize_cases([r['modes'][mode]['cash_ledger'] for r in records]) for mode in _all_modes()}
    pairs = {}
    for mode in _modes():
        old, new = summary[mode], summary[mode+SUFFIX]
        pairs[mode] = dict(pause_only_mean=old['all_fold_mean'], risk_exit_mean=new['all_fold_mean'],
            mean_difference=None if old['all_fold_mean'] is None or new['all_fold_mean'] is None else
                            new['all_fold_mean']-old['all_fold_mean'], risk_matched_control_verified=False,
            inference_performed=False, profitability_proven=False)
    return summary, pairs


def _register(db, identifier, protocol, metrics=None):
    if db.log_experiment(experiment_id=identifier, strategy_name=VERSION,
        symbols_tested='fixed parent ordinary-equity research archive',
        parameters_json=json.dumps(_json_safe(protocol), ensure_ascii=False, allow_nan=False),
        metrics_json=json.dumps(_json_safe(metrics or {}), ensure_ascii=False, allow_nan=False),
        start_date=protocol['development_start'], end_date=protocol['holdout_start'],
        dataset_version=protocol['parent_experiment_id'], features_version='pinned-baseline', model_version=VERSION) is None:
        raise RuntimeError('ثبت پروتکل/نتیجه خروج ریسک ناموفق بود')


def _read_frame(folder, inventory, name, cutoff):
    return read_development_parquet(folder/name, inventory[name], cutoff, require_date=False)


def run_risk_exit_study(db, *, parent_folder, parent_db, root):
    parent = Path(parent_folder).resolve()
    payload = _load_json(parent/'result.json')
    if (parent.name != payload['experiment_id'] or
            not re.fullmatch(r'EXP-\d{4}-\d{2}-\d{2}-HOLDING-[a-f0-9]{32}', payload['experiment_id'])):
        raise ValueError('هویت پوشه والد ناسازگار است')
    parent_files = _parent_files(parent, payload)
    p = payload['protocol']
    if p['config']['max_loss_pct'] != .15 or p['holding_sessions'] != [5,20,60]:
        raise ValueError('آستانه و افق‌های والد باید همان قرارداد ثابت باشند')
    fingerprint = research_code_fingerprint()
    identifier = f'EXP-{now_tehran_iso()[:10]}-RISKEXIT-{uuid.uuid4().hex}'
    folder = Path(root).resolve()/identifier
    clone = folder/'input'/payload['experiment_id']
    clone.mkdir(parents=True, exist_ok=False)
    for name, digest in parent_files.items():
        target = clone/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_relative_file(parent, name), target)
        if _sha256(target) != digest:
            raise ValueError('نسخه والد حین کپی تغییر کرده است')
    registry_sha = _export_registry(parent_db, folder/'parent-registry.db', payload)
    _, inputs, digests = verified_baseline_inputs(clone/'source', p['source_id'])
    if digests != p['inputs']:
        raise ValueError('ورودی محفوظ والد تغییر کرده است')
    panel, _ = _panel(inputs, p['development_start'], p['holdout_start'], p['min_daily_value_rial'])
    calendar = market_calendar(inputs['prices'])
    protocol = dict(version=VERSION, risk_exit_version=RISK_EXIT_VERSION,
        registered_at=now_tehran_iso(), parent_experiment_id=payload['experiment_id'], parent_directory=clone.relative_to(folder).as_posix(),
        parent_files=parent_files, parent_registry_sha256=registry_sha,
        parent_registry_scope='two original rows copied verbatim with original timestamps; no account tables; not new registrations',
        source_files=archive_research_code(folder/'code'), code_sha256=fingerprint,
        development_start=p['development_start'], holdout_start=p['holdout_start'], config=p['config'],
        modes=_all_modes(), folds=p['folds'], modes_in_this_run=54, additional_modes=27,
        parent_minimum_disclosed_trials=p['minimum_disclosed_trials'],
        minimum_disclosed_trials=p['minimum_disclosed_trials']+27, historical_total_trials_verified=False,
        prior_results_seen=True, prospective_independence=False,
        primary_candidate='hold60-momentum_20d'+SUFFIX,
        policy='known finite marked model NAV EOD breach at unchanged 15%; pause entries; request all sellable whole positions from next session; pending until feasible',
        unknown_rights='unvalued model NAV cannot initiate a new risk exit',
        assumptions='same frozen selections, fees, capacity, accounting and fold accounts; no floor-price fill, same-day lookahead, forced end cash or threshold fitting',
        acceptance='no automatic acceptance; economic rights, fees, membership, real execution, same-risk controls and independent evidence still required',
        accepted_for_signals=False, profitability_proven=False)
    _write(folder/'protocol.json', protocol)
    _register(db, identifier+'-PROTOCOL', protocol)
    records = []
    for index, raw_fold in enumerate(p['folds']):
        fold = TimeFold(**raw_fold)
        prices, days = _fold_inputs(inputs['prices'], calendar, fold)
        chosen = fold_picks(panel, days, p['config']['max_positions'], index)
        baseline, _, _ = _past_baseline(inputs, panel, days, fold, p['source_horizons'], p['config']['max_positions'])
        jobs = _jobs(chosen, PortfolioConfig(**p['config']), baseline)
        for mode, (picked, _) in jobs.items():
            original = _read_frame(clone, payload['artifacts_sha256'], f'fold-{index:02d}-{mode}-picks.parquet', p['holdout_start'])
            _same_frame(picked, original)
            picked.to_parquet(folder/f'fold-{index:02d}-{mode+SUFFIX}-picks.parquet', index=False, compression='zstd')
        record = dict(fold=raw_fold, modes=dict(payload['results'][index]['modes']))
        for mode, (picked, configuration) in jobs.items():
            replay = replay_portfolio(prices, picked, calendar, configuration, start=fold.test_start,
                                      end=fold.test_end_exclusive, loss_floor_exit=True)
            for label, frame in dict(equity=replay.equity, executions=replay.executions, decisions=replay.decisions).items():
                frame.to_parquet(folder/f'fold-{index:02d}-{mode+SUFFIX}-{label}.parquet', index=False, compression='zstd')
            ledger = diagnose_cash_ledger(replay.equity, replay.executions, replay.decisions, replay.metrics, len(picked))
            record['modes'][mode+SUFFIX] = dict(metrics=replay.metrics, assumptions=replay.assumptions, cash_ledger=ledger)
        records.append(record)
        _write(folder/f'fold-{index:02d}-checkpoint.json', record)
    if research_code_fingerprint() != fingerprint:
        raise RuntimeError('کد خروج ریسک حین اجرا تغییر کرده است؛ نتیجه منتشر نشد')
    summary, pairs = _summaries(records)
    result = dict(experiment_id=identifier, completed_at=now_tehran_iso(), protocol=protocol,
        results=records, summary=summary, comparisons=pairs,
        artifacts_sha256={f.name: _sha256(f) for f in folder.glob('*.parquet')},
        checkpoints_sha256={f.name: _sha256(f) for f in folder.glob('*checkpoint.json')},
        accepted_for_signals=False, holdout_eligible=False, profitability_proven=False)
    _register(db, identifier, protocol, summary)
    _write(folder/'result.json', result)
    return result


def review_risk_exit_study(db, folder):
    folder = Path(folder).resolve()
    result = _load_json(folder/'result.json')
    p = result['protocol']
    if (folder.name != result['experiment_id'] or p['version'] != VERSION
            or not re.fullmatch(r'EXP-\d{4}-\d{2}-\d{2}-RISKEXIT-[a-f0-9]{32}', result['experiment_id'])
            or not re.fullmatch(r'EXP-\d{4}-\d{2}-\d{2}-HOLDING-[a-f0-9]{32}', p['parent_experiment_id'])
            or p['risk_exit_version'] != RISK_EXIT_VERSION or p['modes'] != _all_modes()
            or p['modes_in_this_run'] != 54 or p['additional_modes'] != 27
            or p['minimum_disclosed_trials'] != p['parent_minimum_disclosed_trials']+27
            or p['prospective_independence'] is not False or p['prior_results_seen'] is not True
            or p['accepted_for_signals'] is not False or p['profitability_proven'] is not False
            or p['holdout_start'] > settings.RESEARCH_CONFIG['holdout_start']
            or any(result[k] is not False for k in ('accepted_for_signals', 'holdout_eligible', 'profitability_proven'))
            or _load_json(folder/'protocol.json') != p):
        raise ValueError('قرارداد خروج ریسک ناسازگار است')
    expected_parent = 'input/'+p['parent_experiment_id']
    if p['parent_directory'] != expected_parent:
        raise ValueError('مسیر والد باید همان هویت محفوظ باشد')
    clone = folder/expected_parent
    _verify_inventory(clone, p['parent_files'])
    _verify_inventory(folder/'code', p['source_files'])
    _verify_inventory(folder, result['artifacts_sha256'], parquet=True)
    if len(result['results']) != len(p['folds']):
        raise ValueError('یک برش خروج ریسک حذف شده است')
    expected_checkpoints = {f'fold-{i:02d}-checkpoint.json' for i in range(len(p['folds']))}
    if (set(result['checkpoints_sha256']) != expected_checkpoints
            or {f.name for f in folder.glob('*checkpoint.json')} != expected_checkpoints):
        raise ValueError('فهرست برش‌های محفوظ خروج ریسک ناسازگار است')
    for name, digest in result['checkpoints_sha256'].items():
        if _sha256(_relative_file(folder, name)) != digest:
            raise ValueError('اثر انگشت برش خروج ریسک ناسازگار است')
    if _sha256(folder/'parent-registry.db') != p['parent_registry_sha256']:
        raise ValueError('ثبت اصلی والد تغییر کرده است')
    for identifier, metrics in ((result['experiment_id']+'-PROTOCOL', {}), (result['experiment_id'], result['summary'])):
        row = db.conn.execute('SELECT * FROM experiments WHERE experiment_id=? AND status=?', (identifier, 'completed')).fetchone()
        if row is None or json.loads(row['parameters_json']) != p or json.loads(row['metrics_json']) != metrics:
            raise ValueError('ثبت خروج ریسک با آرشیو برابر نیست')
    parent = _load_json(clone/'result.json')
    if (p['folds'] != parent['protocol']['folds'] or p['config'] != parent['protocol']['config']
            or p['parent_minimum_disclosed_trials'] != parent['protocol']['minimum_disclosed_trials']):
        raise ValueError('پیکربندی یا برش والد عوض شده است')
    with _registry(folder/'parent-registry.db') as registry:
        parent_review = review_holding_study(registry, clone)
    # The original 270 paths are recomputed here, not merely trusted because hashes match.
    _, inputs, _ = verified_baseline_inputs(clone/'source', parent['protocol']['source_id'])
    pp = parent['protocol']
    panel, _ = _panel(inputs, pp['development_start'], pp['holdout_start'], pp['min_daily_value_rial'])
    calendar = market_calendar(inputs['prices'])
    records = []
    for index, raw_fold in enumerate(p['folds']):
        fold = TimeFold(**raw_fold)
        prices, days = _fold_inputs(inputs['prices'], calendar, fold)
        chosen = fold_picks(panel, days, pp['config']['max_positions'], index)
        baseline, _, _ = _past_baseline(inputs, panel, days, fold, pp['source_horizons'], pp['config']['max_positions'])
        jobs = _jobs(chosen, PortfolioConfig(**p['config']), baseline)
        record = dict(fold=raw_fold, modes=dict(parent['results'][index]['modes']))
        for mode, (picked, configuration) in jobs.items():
            name = mode+SUFFIX
            prefix = f'fold-{index:02d}-{name}'
            _same_frame(picked, _read_frame(folder, result['artifacts_sha256'], prefix+'-picks.parquet', p['holdout_start']))
            replay = replay_portfolio(prices, picked, calendar, configuration, start=fold.test_start,
                                      end=fold.test_end_exclusive, loss_floor_exit=True)
            for label, frame in dict(equity=replay.equity, executions=replay.executions, decisions=replay.decisions).items():
                _same_frame(frame, _read_frame(folder, result['artifacts_sha256'], prefix+'-'+label+'.parquet', p['holdout_start']))
            ledger = diagnose_cash_ledger(replay.equity, replay.executions, replay.decisions, replay.metrics, len(picked))
            record['modes'][name] = dict(metrics=replay.metrics, assumptions=replay.assumptions, cash_ledger=ledger)
        if (_json_safe(record) != result['results'][index]
                or _load_json(folder/f'fold-{index:02d}-checkpoint.json') != _json_safe(record)):
            raise ValueError('دفتر خروج ریسک با بازحساب برابر نیست')
        records.append(record)
    summary, pairs = _summaries(records)
    if _json_safe(summary) != result['summary'] or _json_safe(pairs) != result['comparisons']:
        raise ValueError('خلاصه سیاست‌ها با بازحساب برابر نیست')
    return dict(experiment_id=result['experiment_id'], verified_at=now_tehran_iso(),
        original_paths_recomputed=parent_review['modes_recomputed'], new_paths_recomputed=27*len(p['folds']),
        base_compatibility_verified=True, selection_and_financial_tables_recomputed=True,
        current_code_matches_capture=research_code_fingerprint() == p['code_sha256'],
        accepted_for_signals=False, profitability_proven=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--parent', type=Path)
    parser.add_argument('--parent-db', type=Path)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--root', type=Path, default=settings.DATA_DIR/'private'/'risk-exit-studies')
    parser.add_argument('--review', type=Path)
    args = parser.parse_args(argv)
    from database.db import DatabaseManager
    if args.review:
        with DatabaseManager(args.db, read_only=True) as db:
            output = review_risk_exit_study(db, args.review)
    else:
        if args.parent is None or args.parent_db is None:
            parser.error('parent and parent-db must be explicit')
        if args.db.resolve() == settings.DB_PATH.resolve():
            parser.error('A separate experiment DB is required')
        with DatabaseManager(args.db) as db:
            result = run_risk_exit_study(db, parent_folder=args.parent, parent_db=args.parent_db, root=args.root)
        output = dict(experiment_id=result['experiment_id'], accepted_for_signals=False)
    print(json.dumps(_json_safe(output), ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
