"""Registered development-only surge diagnostics from a frozen rare-event census."""

import argparse
import json
import shutil
import uuid
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from config import settings
from core.timeutils import now_tehran_iso
from ops.research_review import _load_json, read_development_parquet, sha256
from research.labels import LABEL_SET_VERSION, compute_labels
from research.run import _json_safe
from research.surge_patterns import (
    HORIZONS,
    HYPOTHESES,
    VERSION,
    build_pre_move_features,
    censor_at,
    event_counts,
    pattern_masks,
    summarize_patterns,
)
from research.walkforward import time_folds

SOURCE_FILES = ('research/surge_patterns.py', 'research/surge_pattern_run.py', 'research/rare_events.py',
                'research/labels.py', 'research/walkforward.py', 'research/uncertainty.py',
                'research/run.py', 'ops/research_review.py', 'database/db.py',
                'core/market_rules.py', 'core/text.py', 'core/timeutils.py',
                'config/default.yaml', 'config/production.yaml', 'config/settings.py', 'requirements.lock.txt')


def _write(path, value):
    path.write_text(json.dumps(_json_safe(value), ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def _register(db, identifier, protocol, metrics, *, status):
    result = db.log_experiment(experiment_id=identifier, strategy_name=VERSION,
        symbols_tested='entire fixed development census; never the recent winner list',
        parameters_json=json.dumps(_json_safe(protocol), ensure_ascii=False, allow_nan=False),
        metrics_json=json.dumps(_json_safe(metrics), ensure_ascii=False, allow_nan=False),
        start_date=protocol['development_start'], end_date=protocol['end_exclusive'], status=status,
        dataset_version=protocol['parent_prices_sha256'], features_version=VERSION, model_version='fixed-hypotheses-no-fit')
    if result is None:
        raise RuntimeError('Pattern-study registration failed')


def _inputs(parent):
    record = _load_json(parent/'result.json')
    protocol = record['protocol']
    cutoff = settings.RESEARCH_CONFIG['holdout_start']
    if (protocol['holdout_start'] != cutoff or protocol['observed_through'] >= cutoff
            or protocol['horizons'] != [10, 20, 60] or protocol['threshold'] != 2.
            or protocol.get('holdout_opened') is not False
            or protocol.get('accepted_for_signals') is not False):
        raise ValueError('A sealed development-only rare-event census is required')
    for name in ('prices.parquet', 'cohort.parquet', 'outcomes.parquet'):
        if sha256(parent/name) != record['artifacts_sha256'][name]:
            raise ValueError('Parent input fingerprint differs: '+name)
    if sha256(parent/'prices.parquet') != protocol['prices_sha256']:
        raise ValueError('Parent protocol price fingerprint differs')
    prices = read_development_parquet(parent/'prices.parquet', protocol['prices_sha256'], cutoff)
    cohort = read_development_parquet(parent/'cohort.parquet', record['artifacts_sha256']['cohort.parquet'], cutoff)
    outcomes = read_development_parquet(parent/'outcomes.parquet', record['artifacts_sha256']['outcomes.parquet'], cutoff)
    if len(cohort) != protocol['opportunities'] or cohort.duplicated(['symbol', 'date']).any():
        raise ValueError('Invalid fixed opportunity denominator')
    return record, prices, cohort, outcomes


def run_pattern_study(db, *, parent, root, replications=10000):
    parent, root = Path(parent).resolve(), Path(root).resolve()
    if root == parent or root.is_relative_to(parent):
        raise ValueError('Preserve the parent archive; use a separate output root')
    if Path(db.db_path).resolve() == settings.DB_PATH.resolve():
        raise ValueError('Use a separate research DB, never the account DB')
    record, prices, cohort, outcomes = _inputs(parent)
    p = record['protocol']
    calendar = p['calendar']
    cutoff = settings.RESEARCH_CONFIG['holdout_start']
    dev_calendar = [d for d in calendar if d >= p['development_start']]
    folds = time_folds(dev_calendar, min_train_sessions=504, test_sessions=252,
                       embargo_sessions=61, end_exclusive=cutoff)
    identifier = f'EXP-{now_tehran_iso()[:10]}-SURGEPATTERN-{uuid.uuid4().hex}'
    folder = root/identifier
    folder.mkdir(parents=True, exist_ok=False)
    source_hashes = {}
    for name in SOURCE_FILES:
        path = settings.BASE_DIR/name
        if not path.is_file():
            continue
        target = folder/'code'/name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes().replace(b'\r\n', b'\n'))
        source_hashes[name] = sha256(target)
    protocol = dict(version=VERSION, registered_at=now_tehran_iso(),
        parent_experiment_id=record['experiment_id'], parent_path=str(parent),
        parent_prices_sha256=p['prices_sha256'], parent_artifacts_sha256=record['artifacts_sha256'],
        development_start=p['development_start'], end_exclusive=cutoff, observed_through=p['observed_through'],
        calendar=calendar, anchor_stride=p['anchor_stride'], opportunities=len(cohort),
        hypotheses=[asdict(h) for h in HYPOTHESES], horizons=list(HORIZONS), threshold=2.,
        source_files_sha256=source_hashes, calendar_min_symbols=p['calendar_min_symbols'], cross_section_min_symbols=20,
        predefined_hypothesis_horizon_pairs=len(HYPOTHESES)*len(HORIZONS),
        reported_block_lengths=[4, 8], within_run_comparison_family=len(HYPOTHESES)*len(HORIZONS)*2,
        replications=replications, total_project_search_attempts=None,
        folds=[asdict(f) for f in folds], min_train_sessions=504, test_sessions=252, embargo_sessions=61,
        execution_contract=LABEL_SET_VERSION, execution_horizons=[h-1 for h in HORIZONS],
        execution_horizon_meaning='entry t+1, planned exit t+H; locked/missing exit may delay',
        fees=dict(settings.FEES), slippage_bps=settings.BACKTEST_CONFIG['default_slippage_bps'],
        recent_winners_used_for_selection=False, thresholds_optimized=False, models_fitted=0,
        past_development_previously_exposed=True, independent_future_test=False,
        conditional_diagnostics_only=True, historical_membership_verified=False,
        economic_entitlements_verified=False, shared_cash_simulated=False,
        holdout_opened=False, accepted_for_signals=False, profitability_proven=False)
    _write(folder/'protocol.json', protocol)
    _register(db, identifier+'-PROTOCOL', protocol, {}, status='registered')
    # Registration must succeed before adding features or calculating new outcome contrasts.
    features = build_pre_move_features(prices, cohort, calendar)
    features.to_parquet(folder/'features.parquet', index=False)
    masks = pattern_masks(features)
    pd.concat([features[['date', 'symbol']], masks], axis=1).to_parquet(folder/'flags.parquet', index=False)
    labels = compute_labels(prices, horizons=tuple(h-1 for h in HORIZONS), calendar=calendar)
    executions = cohort.merge(labels, on=['date', 'symbol'], how='left', validate='one_to_one')
    executions.to_parquet(folder/'executions.parquet', index=False)
    summaries = summarize_patterns(features, outcomes, executions=executions,
                                   bootstrap=True, replications=replications)
    temporal = []
    for number, fold in enumerate(folds):
        test_keys = features.date.ge(fold.test_start) & features.date.lt(fold.test_end_exclusive)
        train_keys = features.date.ge(fold.train_start) & features.date.lt(fold.train_end_exclusive)
        if not test_keys.any():
            continue
        test_features = features.loc[test_keys].reset_index(drop=True)
        test_days = set(test_features.date)
        test_outcomes = outcomes.loc[outcomes.date.isin(test_days)]
        test_executions = executions.loc[executions.date.isin(test_days)]
        training_days = set(features.loc[train_keys, 'date'])
        training = outcomes.loc[outcomes.date.isin(training_days)]
        training_counts = {}
        for horizon in HORIZONS:
            resolved = censor_at(training.loc[training.horizon_sessions.eq(horizon)], fold.train_end_exclusive)
            training_counts[str(horizon)] = event_counts(resolved)
        temporal.append(dict(fold=number, boundaries=asdict(fold), training_resolved_before_boundary=training_counts,
            summary=summarize_patterns(test_features, test_outcomes, executions=test_executions,
                                       end_exclusive=fold.test_end_exclusive)))
    # Year attribution is descriptive; it does not split one bull regime into independent evidence.
    hits = outcomes.loc[outcomes.status.eq('hit') & outcomes.horizon_sessions.eq(60)]
    year_counts = {str(k): int(v) for k, v in hits.groupby(hits.date.str[:4]).size().items()}
    for name, digest in source_hashes.items():
        if sha256(folder/'code'/name) != digest or settings.BASE_DIR.joinpath(name).read_bytes().replace(b'\r\n', b'\n') != (folder/'code'/name).read_bytes():
            raise ValueError('Source changed during the pattern study')
    artifacts = {name: sha256(folder/name) for name in ('features.parquet', 'flags.parquet', 'executions.parquet')}
    result = dict(experiment_id=identifier, protocol=protocol, completed_at=now_tehran_iso(),
        summary=summaries, temporal_folds=temporal, sixty_session_hits_by_anchor_year=year_counts,
        artifact_sha256=artifacts, accepted_for_signals=False, profitability_proven=False,
        limitations=['fixed-hypothesis retrospective development diagnostics; not independent validation',
                    'unknown outcomes remain in prevalence bounds; conditional rates may be selection biased',
                    'same-date observed controls do not establish causality, sector/size matching or complete membership',
                    'family intervals cover this fixed run only, not the unknown entire project search history',
                    'overlapping anchors, regime changes and synthetic corporate-action accounting remain',
                    'equal-notional trade diagnostics are not a capacity-tested shared-cash portfolio'])
    _write(folder/'result.json', result)
    _register(db, identifier, protocol, summaries, status='completed')
    shutil.copyfile(parent/'result.json', folder/'parent-result.json')
    return result, folder


def review_pattern_study(db, folder):
    """Replay the frozen denominator, every feature, execution and hypothesis summary."""
    folder = Path(folder).resolve()
    result = _load_json(folder/'result.json')
    protocol = _load_json(folder/'protocol.json')
    if (result['protocol'] != protocol or protocol['version'] != VERSION
            or protocol['hypotheses'] != [asdict(h) for h in HYPOTHESES]
            or protocol['horizons'] != list(HORIZONS)
            or any(result.get(k) is not False for k in ('accepted_for_signals', 'profitability_proven'))):
        raise ValueError('Pattern study identity or acceptance contract differs')
    for name, digest in protocol['source_files_sha256'].items():
        if sha256(folder/'code'/name) != digest:
            raise ValueError('Archived source fingerprint differs')
    for name, digest in result['artifact_sha256'].items():
        if sha256(folder/name) != digest:
            raise ValueError('Study artifact fingerprint differs')
    for suffix, status, metrics in (('-PROTOCOL', 'registered', {}), ('', 'completed', result['summary'])):
        entry = db.conn.execute('SELECT parameters_json,metrics_json FROM experiments WHERE experiment_id=? AND status=?',
                                (result['experiment_id']+suffix, status)).fetchone()
        if entry is None or json.loads(entry[0]) != protocol or json.loads(entry[1]) != metrics:
            raise ValueError('Independent experiment registry differs')
    parent, prices, cohort, outcomes = _inputs(Path(protocol['parent_path']))
    if parent['artifacts_sha256'] != protocol['parent_artifacts_sha256']:
        raise ValueError('Parent data changed since registration')
    features = build_pre_move_features(prices, cohort, protocol['calendar'])
    pd.testing.assert_frame_equal(features, pd.read_parquet(folder/'features.parquet'))
    flags = pd.concat([features[['date', 'symbol']], pattern_masks(features)], axis=1)
    pd.testing.assert_frame_equal(flags, pd.read_parquet(folder/'flags.parquet'))
    labels = compute_labels(prices, horizons=tuple(h-1 for h in HORIZONS), calendar=protocol['calendar'])
    executions = cohort.merge(labels, on=['date', 'symbol'], how='left', validate='one_to_one')
    pd.testing.assert_frame_equal(executions, pd.read_parquet(folder/'executions.parquet'))
    replay = summarize_patterns(features, outcomes, executions=executions,
                                bootstrap=True, replications=protocol['replications'])
    if _json_safe(replay) != result['summary']:
        raise ValueError('Recomputed hypothesis statistics differ')
    return dict(experiment_id=result['experiment_id'], verified_at=now_tehran_iso(),
                features_recomputed=len(features), executions_recomputed=len(executions),
                hypothesis_horizon_pairs_recomputed=len(replay),
                current_code_matches_capture=all(settings.BASE_DIR.joinpath(n).read_bytes().replace(b'\r\n', b'\n')
                    == (folder/'code'/n).read_bytes() for n in protocol['source_files_sha256']),
                temporal_folds_recomputed=False, accepted_for_signals=False, profitability_proven=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--parent', type=Path)
    parser.add_argument('--root', type=Path)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--replications', type=int, default=10000)
    parser.add_argument('--review', type=Path)
    args = parser.parse_args(argv)
    if args.db.resolve() == settings.DB_PATH.resolve():
        parser.error('A separate research DB is required')
    from database.db import DatabaseManager
    if args.review:
        with DatabaseManager(args.db, read_only=True) as db:
            review = review_pattern_study(db, args.review)
        print(json.dumps(review, ensure_ascii=False))
        return 0
    if args.parent is None or args.root is None:
        parser.error('parent and root are required for a new study')
    with DatabaseManager(args.db) as db:
        result, folder = run_pattern_study(db, parent=args.parent, root=args.root, replications=args.replications)
    print(json.dumps(dict(experiment_id=result['experiment_id'], folder=str(folder),
                         hypotheses=len(HYPOTHESES), accepted_for_signals=False), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
