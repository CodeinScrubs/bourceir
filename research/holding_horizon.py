"""Fixed development-only holding-period study; no signal or profitability acceptance."""

import argparse
import json
import shutil
import uuid
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.portfolio import PORTFOLIO_VERSION, PortfolioConfig, replay_portfolio
from config import settings
from core.capital_capacity import diagnose_cash_ledger, summarize_cases
from core.timeutils import now_tehran_iso
from ops.research_review import _relative_file, _verify_inventory, read_development_parquet
from research.labels import market_calendar
from research.portfolio_run import _picks, select_from_past, verified_baseline_inputs
from research.probability import opportunity_features
from research.run import _json_safe, _sha256, archive_research_code, research_code_fingerprint
from research.walkforward import time_folds

VERSION = 'holding-horizon-study-v1'
HOLDING_SESSIONS = (5, 20, 60)  # Includes the entry session: engine horizons are 4, 19, 59.
SEEDS = (41, 97, 173)
RULE = 'momentum_20d'


def _write(path, value):
    path.write_text(json.dumps(_json_safe(value), ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def _modes():
    return ['idle_cash', 'past_baseline', 'past_baseline-stress'] + [f'hold{hold}-{selection}{suffix}' for hold in HOLDING_SESSIONS
        for selection in (RULE, *(f'random{seed}' for seed in SEEDS)) for suffix in ('', '-stress')]


def _panel(inputs, start, cutoff, min_value):
    # The helper exposes decision features and same-day value, never future labels.
    frame = opportunity_features(inputs['features'], inputs['labels'], prices=inputs['prices'])
    frame = frame.loc[frame.date.ge(start) & frame.date.lt(cutoff) & frame.value.ge(min_value),
                      ['date', 'symbol', 'value', 'ret_20d']].copy()
    eligible = np.isfinite(frame.ret_20d) & np.isfinite(frame.value)
    return frame.loc[eligible].sort_values(['date', 'symbol']).reset_index(drop=True), int((~eligible).sum())


def _same_frame(actual, saved):
    # Parquet restores the columns of a 0x0 frame as an object Index rather than RangeIndex.
    if actual.shape == saved.shape == (0, 0):
        return
    pd.testing.assert_frame_equal(actual.reset_index(drop=True), saved.reset_index(drop=True))


def fold_picks(panel, days, k, index):
    """Every holding period gets the same pre-outcome intake window and opportunity counts."""
    if set(panel) != {'date', 'symbol', 'value', 'ret_20d'}:
        raise ValueError('انتخاب فقط چهار ستونِ روز تصمیم را می‌پذیرد')
    if len(days) <= max(HOLDING_SESSIONS):
        raise ValueError('هر برش باید بیش از شصت جلسه داشته باشد')
    # t < days[-61]: entry plus 59 later sessions fit, with one additional buffer session.
    pool = panel.loc[panel.date.ge(days[0]) & panel.date.lt(days[-max(HOLDING_SESSIONS)-1])]
    chosen = {RULE: _picks(pool, RULE, k)}
    counts = chosen[RULE].groupby('date').size().to_dict()
    for seed in SEEDS:
        control = _picks(pool, None, k, seed + index)
        if control.groupby('date').size().to_dict() != counts:
            raise ValueError('شمار روزانه کنترل و نامزد برابر نیست')
        chosen[f'random{seed}'] = control
    return chosen


def _jobs(chosen, config, baseline):
    base_picks, horizon = baseline
    jobs = {'idle_cash': (pd.DataFrame(columns=['date', 'symbol', 'rank']), config),
            'past_baseline': (base_picks, replace(config, horizon=horizon)),
            'past_baseline-stress': (base_picks, replace(config, horizon=horizon, stress=True))}
    for hold in HOLDING_SESSIONS:
        for selection, picks in chosen.items():
            for stress in (False, True):
                name = f'hold{hold}-{selection}' + ('-stress' if stress else '')
                jobs[name] = (picks, replace(config, horizon=hold-1, stress=stress))
    if list(jobs) != _modes():
        raise ValueError('حالت‌های پژوهش با خانواده ثابت برابر نیستند')
    return jobs


def _past_baseline(inputs, panel, days, fold, horizons, k):
    """Choose among existing rules using only purged training outcomes, never test outcomes."""
    full = opportunity_features(inputs['features'], inputs['labels'], prices=inputs['prices'])
    full = full.merge(panel[['date', 'symbol']], on=['date', 'symbol'], how='inner', validate='one_to_one')
    training = full.loc[full.date.lt(fold.train_end_exclusive)].merge(
        inputs['labels'].drop(columns='value'), on=['date', 'symbol'], validate='one_to_one')
    best, diagnostics = select_from_past(training, fold, horizons, k)
    test = full.loc[full.date.ge(days[0]) & full.date.lt(days[-max(HOLDING_SESSIONS)-1])]
    picked = _picks(test, best['strategy'], k) if best else pd.DataFrame(columns=['date', 'symbol', 'rank'])
    return (picked, best['horizon'] if best else horizons[0]), best, diagnostics


def _fold_inputs(prices, calendar, fold):
    days = calendar[(calendar >= fold.test_start) & (calendar < fold.test_end_exclusive)]
    prior = prices.loc[prices.date.lt(fold.test_start)].groupby('symbol', sort=False).tail(1)
    current = prices.loc[prices.date.ge(fold.test_start) & prices.date.lt(fold.test_end_exclusive)]
    return pd.concat([prior, current], ignore_index=True), days


def _summarize(records):
    # Missing folds never disappear from the denominator or become zero return.
    summary = {mode: summarize_cases([record['modes'][mode]['cash_ledger'] for record in records])
               for mode in _modes()}
    comparisons = {}
    for hold in HOLDING_SESSIONS:
        comparisons[str(hold)] = {}
        for suffix in ('', '-stress'):
            name = f'hold{hold}-{RULE}' + suffix
            candidate = summary[name]
            edges = {}
            for seed in SEEDS:
                control = summary[f'hold{hold}-random{seed}' + suffix]
                a, b = candidate['all_fold_mean'], control['all_fold_mean']
                edges[str(seed)] = None if a is None or b is None else a-b
            comparisons[str(hold)]['stress' if suffix else 'normal'] = dict(
                mean_independent_fold_return=candidate['all_fold_mean'],
                mean_edge_to_same_holding_random_controls=edges,
                mean_edge_to_past_selected_baseline=None if candidate['all_fold_mean'] is None or
                    summary['past_baseline'+suffix]['all_fold_mean'] is None else
                    candidate['all_fold_mean']-summary['past_baseline'+suffix]['all_fold_mean'],
                risk_matched=False, inferential_test_performed=False)
    return summary, comparisons


def _register(db, experiment_id, protocol, summary=None):
    result = db.log_experiment(experiment_id=experiment_id, strategy_name=VERSION,
        symbols_tested='pinned baseline archive; current known registry, not historical membership',
        parameters_json=json.dumps(_json_safe(protocol), ensure_ascii=False, allow_nan=False),
        metrics_json=json.dumps(_json_safe(summary or {}), ensure_ascii=False, allow_nan=False),
        start_date=protocol['development_start'], end_date=protocol['holdout_start'],
        dataset_version=protocol['source_id'], features_version='pinned-baseline', model_version=VERSION)
    if result is None:
        raise RuntimeError('ثبت پروتکل/نتیجه پژوهش ناموفق بود')


def run_holding_study(db, *, source_root, source_id, root, initial_capital_rial,
                      prior_trials, min_train_sessions=504, test_sessions=252):
    """Register in an explicit experiment DB; market inputs stay in a separate immutable archive."""
    if type(prior_trials) is not int or prior_trials < 1:
        raise ValueError('شمار صحیح تلاش‌های قبلی باید صریح باشد')
    config = PortfolioConfig(initial_capital_rial=initial_capital_rial, max_positions=5,
        max_loss_pct=.15, slippage_bps=settings.BACKTEST_CONFIG['default_slippage_bps'], participation=.05)
    fingerprint = research_code_fingerprint()
    source_root, root = Path(source_root).resolve(), Path(root).resolve()
    source_id, inputs, digests = verified_baseline_inputs(source_root, source_id)
    source = json.loads((source_root / f'{source_id}.json').read_text(encoding='utf-8'))
    source_pairs = len(source['params']['strategies']) * len(source['params']['horizons'])
    if prior_trials < source_pairs:
        raise ValueError('شمار تلاش‌های قبلی از خانواده منبع کمتر است')
    cfg = settings.RESEARCH_CONFIG
    panel, excluded_scores = _panel(inputs, cfg['dev_start'], cfg['holdout_start'], cfg['min_daily_value_rial'])
    calendar = market_calendar(inputs['prices'])
    folds = time_folds(calendar[calendar >= cfg['dev_start']], min_train_sessions=min_train_sessions,
        test_sessions=test_sessions, embargo_sessions=max(HOLDING_SESSIONS), end_exclusive=cfg['holdout_start'])
    if any(sum((calendar >= f.test_start) & (calendar < f.test_end_exclusive)) <= max(HOLDING_SESSIONS) for f in folds):
        raise ValueError('هر برش باید بیش از شصت جلسه داشته باشد؛ برش کوتاه حذف نمی‌شود')
    experiment_id = f'EXP-{now_tehran_iso()[:10]}-HOLDING-{uuid.uuid4().hex}'
    folder = root / experiment_id
    folder.mkdir(parents=True, exist_ok=False)
    pinned_root = folder / 'source'
    pinned_folder = pinned_root / source_id
    pinned_folder.mkdir(parents=True)
    files = {f'source/{source_id}.json': source_root / f'{source_id}.json',
             f'source/{source_id}/manifest.json': source_root / source_id / 'manifest.json'}
    files.update({f'source/{source_id}/{name}.parquet': source_root / source_id / f'{name}.parquet' for name in digests})
    hashes = {}
    for relative, origin in files.items():
        target = folder / relative
        shutil.copyfile(origin, target)
        hashes[relative] = _sha256(target)
        if _sha256(origin) != hashes[relative]:
            raise ValueError('ورودی منبع حین کپی تغییر کرده است')
    # Revalidate copied bytes (including date-first reads) before registering or selecting anything.
    _, _, copied_digests = verified_baseline_inputs(pinned_root, source_id)
    if digests != copied_digests:
        raise ValueError('نسخه محفوظ منبع با ورودی خوانده‌شده برابر نیست')
    protocol = dict(version=VERSION, portfolio_version=PORTFOLIO_VERSION, source_id=source_id,
        registered_at=now_tehran_iso(), source_files=archive_research_code(folder / 'code'),
        input_files=hashes, inputs=digests, code_sha256=fingerprint, config=asdict(config),
        development_start=cfg['dev_start'], holdout_start=cfg['holdout_start'],
        min_daily_value_rial=cfg['min_daily_value_rial'], score_unavailable_rows=excluded_scores,
        decision_rows_with_score=len(panel), decision_rows_before_score_gate=len(panel)+excluded_scores,
        windows=dict(min_train_sessions=min_train_sessions, test_sessions=test_sessions, embargo_sessions=60),
        folds=[asdict(f) for f in folds], modes=_modes(), holding_sessions=list(HOLDING_SESSIONS),
        engine_horizons=[h-1 for h in HOLDING_SESSIONS], rule=RULE, random_seeds=list(SEEDS),
        source_horizons=source['params']['horizons'],
        prior_disclosed_trials=prior_trials, modes_in_this_run=len(_modes()),
        minimum_disclosed_trials=prior_trials+len(_modes()), historical_total_trials_verified=False,
        scope='fixed archived-input hypothesis screen; old data not upgraded to current source revisions',
        selection='same 20-session PIT momentum score as existing baseline; ties by decision value then symbol; no parameter fitting',
        intake='same decision window for all holding periods; stop 60 sessions before fold boundary',
        risk='independent hypothetical sleeve; pause new entries at 15% initial loss; not full owner portfolio or guaranteed loss limit',
        accounting='portfolio-v4 synthetic reference adjustment; actual entitlements and historical fees unverified',
        inference='descriptive rejection screen only; no p-values, winner selection, holdout access or signal acceptance',
        baseline='best positive prior cohort mean among eight existing rules/source horizons; actual-exit purge before training boundary; otherwise cash',
        prospective_independence=False, accepted_for_signals=False, profitability_proven=False)
    _write(folder / 'protocol.json', protocol)
    _register(db, experiment_id+'-PROTOCOL', protocol)
    records = []
    for index, fold in enumerate(folds):
        replay_prices, days = _fold_inputs(inputs['prices'], calendar, fold)
        chosen = fold_picks(panel, days, config.max_positions, index)
        baseline, best, training = _past_baseline(inputs, panel, days, fold, protocol['source_horizons'], config.max_positions)
        jobs = _jobs(chosen, config, baseline)
        # All decisions are sealed on disk before the engine sees test-period execution data.
        for mode, (picked, _) in jobs.items():
            picked.to_parquet(folder / f'fold-{index:02d}-{mode}-picks.parquet', index=False, compression='zstd')
        record = dict(fold=asdict(fold), best_past_baseline=best, baseline_training=training, modes={})
        for mode, (picked, configuration) in jobs.items():
            replay = replay_portfolio(replay_prices, picked, calendar, configuration,
                                      start=fold.test_start, end=fold.test_end_exclusive)
            ledger = diagnose_cash_ledger(replay.equity, replay.executions, replay.decisions, replay.metrics, len(picked))
            for label, frame in dict(equity=replay.equity, executions=replay.executions, decisions=replay.decisions).items():
                frame.to_parquet(folder / f'fold-{index:02d}-{mode}-{label}.parquet', index=False, compression='zstd')
            record['modes'][mode] = dict(metrics=replay.metrics, assumptions=replay.assumptions, cash_ledger=ledger)
        records.append(record)
        _write(folder / f'fold-{index:02d}-checkpoint.json', record)
    if research_code_fingerprint() != fingerprint:
        raise RuntimeError('کد پژوهش حین اجرا تغییر کرده است؛ نتیجه منتشر نشد')
    summary, comparisons = _summarize(records)
    payload = dict(experiment_id=experiment_id, completed_at=now_tehran_iso(), protocol=protocol,
        results=records, summary=summary, comparisons=comparisons,
        artifacts_sha256={p.name: _sha256(p) for p in folder.glob('*.parquet')},
        checkpoints_sha256={p.name: _sha256(p) for p in folder.glob('*checkpoint.json')},
        accepted_for_signals=False, holdout_eligible=False, profitability_proven=False,
        unresolved_gates=['historical_membership', 'economic_corporate_entitlements',
                          'historical_fee_schedule', 'execution_feasibility', 'risk_matched_control',
                          'independent_validation', 'full_portfolio_and_rolling_12_month_targets'])
    _register(db, experiment_id, protocol, summary)
    _write(folder / 'result.json', payload)
    return payload


def review_holding_study(db, folder):
    """Read-only reconstruction of all selections, executions and cash ledgers with current code."""
    folder = Path(folder).resolve()
    payload = json.loads((folder / 'result.json').read_text(encoding='utf-8'))
    p = payload['protocol']
    if (p['version'] != VERSION or p['rule'] != RULE or p['modes'] != _modes()
            or p['holding_sessions'] != list(HOLDING_SESSIONS) or p['engine_horizons'] != [h-1 for h in HOLDING_SESSIONS]
            or p['random_seeds'] != list(SEEDS) or p['windows']['embargo_sessions'] != 60
            or p['holdout_start'] > settings.RESEARCH_CONFIG['holdout_start']
            or p['prospective_independence'] is not False or p['accepted_for_signals'] is not False
            or p['profitability_proven'] is not False
            or any(payload[key] is not False for key in ('accepted_for_signals', 'holdout_eligible', 'profitability_proven'))
            or json.loads((folder / 'protocol.json').read_text(encoding='utf-8')) != p):
        raise ValueError('قرارداد پژوهش یا مرز پذیرش ناسازگار است')
    fixed_config = dict(horizon=2, max_positions=5, max_loss_pct=.15, participation=.05,
                        stress=False, allow_entry_day_exit=False)
    if (any(p['config'].get(k) != value for k, value in fixed_config.items())
            or folder.name != payload['experiment_id'] or p['portfolio_version'] != PORTFOLIO_VERSION
            or p['historical_total_trials_verified'] is not False):
        raise ValueError('تنظیمات ثابت یا هویت مطالعه ناسازگار است')
    _verify_inventory(folder / 'code', p['source_files'])
    if any(not name.startswith('source/') for name in p['input_files']):
        raise ValueError('ورودی باید در آرشیو مستقل منبع باشد')
    _verify_inventory(folder / 'source', {name.removeprefix('source/'): h for name, h in p['input_files'].items()})
    _verify_inventory(folder, payload['artifacts_sha256'], parquet=True)
    expected_artifacts = {f'fold-{index:02d}-{mode}-{label}.parquet' for index in range(len(p['folds']))
                          for mode in _modes() for label in ('picks', 'equity', 'executions', 'decisions')}
    if set(payload['artifacts_sha256']) != expected_artifacts:
        raise ValueError('جدول انتخاب یا اجرای یک حالت حذف شده است')
    checkpoints = {f'fold-{i:02d}-checkpoint.json' for i in range(len(p['folds']))}
    if set(payload['checkpoints_sha256']) != checkpoints or {f.name for f in folder.glob('*checkpoint.json')} != checkpoints:
        raise ValueError('فهرست برش‌های محفوظ ناسازگار است')
    for name, digest in payload['checkpoints_sha256'].items():
        if _sha256(_relative_file(folder, name)) != digest:
            raise ValueError('اثر انگشت برش محفوظ ناسازگار است')
    for name, expected in ((payload['experiment_id']+'-PROTOCOL', {}), (payload['experiment_id'], payload['summary'])):
        row = db.conn.execute('SELECT * FROM experiments WHERE experiment_id=? AND status=?', (name, 'completed')).fetchone()
        if row is None or json.loads(row['parameters_json']) != p or json.loads(row['metrics_json']) != expected:
            raise ValueError('ثبت آزمایش با آرشیو برابر نیست')
    _, inputs, digests = verified_baseline_inputs(folder / 'source', p['source_id'])
    source = json.loads((folder / 'source' / (p['source_id']+'.json')).read_text(encoding='utf-8'))
    if digests != p['inputs'] or p['modes_in_this_run'] != len(_modes()) or p['minimum_disclosed_trials'] != p['prior_disclosed_trials']+len(_modes()):
        raise ValueError('ورودی یا شمار تلاش‌ها ناسازگار است')
    if (p['source_horizons'] != source['params']['horizons'] or
            p['prior_disclosed_trials'] < len(source['params']['strategies'])*len(p['source_horizons'])):
        raise ValueError('افق‌های پایه یا شمار تلاش‌های منبع ناسازگار است')
    panel, excluded = _panel(inputs, p['development_start'], p['holdout_start'], p['min_daily_value_rial'])
    calendar = market_calendar(inputs['prices'])
    folds = time_folds(calendar[calendar >= p['development_start']], end_exclusive=p['holdout_start'], **p['windows'])
    if (p['folds'] != [asdict(f) for f in folds] or excluded != p['score_unavailable_rows']
            or p['decision_rows_with_score'] != len(panel)
            or p['decision_rows_before_score_gate'] != len(panel)+excluded
            or len(payload['results']) != len(folds)):
        raise ValueError('برش یا پوشش امتیاز برابر نیست')
    records = []
    for index, fold in enumerate(folds):
        prices, days = _fold_inputs(inputs['prices'], calendar, fold)
        chosen = fold_picks(panel, days, p['config']['max_positions'], index)
        baseline, best, training = _past_baseline(inputs, panel, days, fold, p['source_horizons'], p['config']['max_positions'])
        record = dict(fold=asdict(fold), best_past_baseline=best, baseline_training=training, modes={})
        for mode, (picked, configuration) in _jobs(chosen, PortfolioConfig(**p['config']), baseline).items():
            prefix = f'fold-{index:02d}-{mode}'
            def saved(label):
                name = f'{prefix}-{label}.parquet'
                return read_development_parquet(folder / name, payload['artifacts_sha256'][name],
                                                p['holdout_start'], require_date=False).reset_index(drop=True)

            _same_frame(picked, saved('picks'))
            replay = replay_portfolio(prices, picked, calendar, configuration, start=fold.test_start, end=fold.test_end_exclusive)
            for label, frame in dict(equity=replay.equity, executions=replay.executions, decisions=replay.decisions).items():
                _same_frame(frame, saved(label))
            ledger = diagnose_cash_ledger(replay.equity, replay.executions, replay.decisions, replay.metrics, len(picked))
            record['modes'][mode] = dict(metrics=replay.metrics, assumptions=replay.assumptions, cash_ledger=ledger)
        if (_json_safe(record) != payload['results'][index]
                or _json_safe(record) != json.loads((folder / f'fold-{index:02d}-checkpoint.json').read_text(encoding='utf-8'))):
            raise ValueError('محاسبه مجدد دفتر مالی با نتیجه برابر نیست')
        records.append(record)
    summary, comparisons = _summarize(records)
    if _json_safe(summary) != payload['summary'] or _json_safe(comparisons) != payload['comparisons']:
        raise ValueError('خلاصه یا مقایسه با بازحساب برابر نیست')
    return dict(experiment_id=payload['experiment_id'], verified_at=now_tehran_iso(),
                modes_recomputed=len(_modes())*len(folds), selection_and_financial_tables_recomputed=True,
                current_code_matches_capture=research_code_fingerprint() == p['code_sha256'],
                accepted_for_signals=False, profitability_proven=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source')
    parser.add_argument('--source-root', default=str(settings.DATA_DIR / 'research'))
    parser.add_argument('--root', default=str(settings.DATA_DIR / 'private' / 'holding-studies'))
    parser.add_argument('--db', required=True, help='Explicit research experiment DB; no broker DB is required')
    parser.add_argument('--capital-rial', type=float)
    parser.add_argument('--prior-trials', type=int)
    parser.add_argument('--review', type=Path)
    args = parser.parse_args(argv)
    from database.db import DatabaseManager
    if args.review:
        with DatabaseManager(args.db, read_only=True) as db:
            result = review_holding_study(db, args.review)
    else:
        if not args.source or args.capital_rial is None or args.prior_trials is None:
            parser.error('source, capital-rial and prior-trials must be explicit')
        if Path(args.db).resolve() == settings.DB_PATH.resolve():
            parser.error('Use a separate experiment DB; the main account DB is excluded')
        with DatabaseManager(args.db) as db:
            payload = run_holding_study(db, source_root=args.source_root, source_id=args.source,
                root=args.root, initial_capital_rial=args.capital_rial, prior_trials=args.prior_trials)
        result = dict(experiment_id=payload['experiment_id'], summary=payload['summary'], accepted_for_signals=False)
    print(json.dumps(_json_safe(result), ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
