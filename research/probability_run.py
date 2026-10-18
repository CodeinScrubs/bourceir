"""آزمایش ثبت‌شده احتمال روی ورودی محفوظ؛ هیچ رتبه یا سفارش زنده‌ای تولید نمی‌کند."""

import json
import math
import os
import re
import shutil
import uuid
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from config import settings
from core.timeutils import is_canonical_iso_date, now_tehran_iso
from ops.research_review import _load_json, _verify_inventory
from research.baselines import STRATEGIES
from research.labels import market_calendar
from research.portfolio_run import verified_baseline_inputs
from research.probability import (
    MIN_TRAIN_SESSIONS,
    TARGET,
    VERSION,
    fit_frequency,
    forecast_opportunities,
    opportunity_features,
    resolved_outcomes,
    score_forecasts,
    select_opportunities,
)
from research.run import _deduplicate_input, _json_safe, _sha256, archive_research_code, research_code_fingerprint
from research.walkforward import time_folds

EXPERIMENT_PATTERN = r'EXP-\d{4}-\d{2}-\d{2}-PROBABILITY-[a-f0-9]{32}'
HORIZONS = (1, 2)  # با احتساب ورود، دو و سه تاریخ جلسه؛ بدون جست‌وجوی افق تازه.


def _dump(path, value):
    temporary = path.with_name(path.name + '-' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(_json_safe(value), ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _outcomes_for(selected, labels, horizon, boundary):
    joined = selected[['date', 'symbol']].merge(labels, on=['date', 'symbol'], how='left', validate='one_to_one', indicator=True)
    if joined['_merge'].ne('both').any():
        raise ValueError('فرصت بدون ردیف برچسب در منبع محفوظ وجود دارد')
    return resolved_outcomes(joined, horizon, boundary)


def _subset(outcomes, selected):
    return selected[['date', 'symbol']].merge(outcomes, on=['date', 'symbol'], how='left', validate='one_to_one')


def run_probability_research(db, *, source_id, root=None):
    """مدل هر برش قبل از پیوند نتیجه آزمون ثبت می‌شود؛ همه قواعد بدون انتخاب برنده گزارش می‌شوند."""
    root = Path(root) if root is not None else settings.DATA_DIR / 'research'
    source_id, inputs, digests = verified_baseline_inputs(root, source_id)
    source_path = root / (source_id + '.json')
    source_report_sha = _sha256(source_path)
    source = _load_json(source_path)
    if _sha256(source_path) != source_report_sha:
        raise ValueError('گزارش منبع حین خواندن تغییر کرد')
    params = source['params']
    expected_rules = [s.name for s in STRATEGIES]
    start, cutoff = params['periods']['development']
    if (not is_canonical_iso_date(start) or not is_canonical_iso_date(cutoff) or start >= cutoff
            or params['strategies'] != expected_rules or not set(HORIZONS) <= set(params['horizons'])
            or cutoff > settings.RESEARCH_CONFIG['holdout_start'] or start < settings.RESEARCH_CONFIG['dev_start']):
        raise ValueError('قواعد یا مرز منبع پایه با قرارداد احتمال سازگار نیست')
    k, min_value = params['top_k'], params['min_daily_value_rial']
    if (type(k) is not int or not 1 <= k <= 50 or isinstance(min_value, bool)
            or not isinstance(min_value, (int, float)) or not math.isfinite(min_value) or min_value < 0):
        raise ValueError('تعداد انتخاب یا نقدشوندگی منبع نامعتبر است')
    feature_rows = opportunity_features(inputs['features'], inputs['labels'])
    feature_rows = feature_rows.loc[(feature_rows.date >= start) & (feature_rows.date < cutoff)
                                    & (feature_rows.value >= min_value)].reset_index(drop=True)
    calendar = market_calendar(inputs['prices'])
    dev_calendar = calendar[(calendar >= start) & (calendar < cutoff)]
    folds = time_folds(dev_calendar, min_train_sessions=504, test_sessions=252,
                       embargo_sessions=max(HORIZONS)+1, end_exclusive=cutoff)
    source_record = db.conn.execute('SELECT parameters_json FROM experiments WHERE experiment_id=? AND status=?',
                                    (source_id, 'completed')).fetchone()
    if source_record is None or json.loads(source_record['parameters_json']) != params:
        raise ValueError('ثبت منبع پایه با پروتکل محفوظ سازگار نیست')
    experiment_id = f'EXP-{now_tehran_iso()[:10]}-PROBABILITY-{uuid.uuid4().hex}'
    folder = root / experiment_id
    folder.mkdir(parents=True)
    code_sha = research_code_fingerprint()
    for name, digest in digests.items():
        target = folder / (name + '.parquet')
        shutil.copyfile(root / source_id / target.name, target)
        if _sha256(target) != digest:
            raise ValueError('ورودی هنگام ساخت تصویر مستقل تغییر کرد')
        _deduplicate_input(target, root / 'objects')
    protocol = dict(version=VERSION, target=TARGET, registered_at=now_tehran_iso(), source_id=source_id,
                    source_report_sha256=source_report_sha, inputs=digests,
                    source_manifest=_load_json(root / source_id / 'manifest.json'),
                    strategies=expected_rules, horizons=list(HORIZONS), top_k=k, min_daily_value_rial=min_value,
                    development_start=start, end_exclusive=cutoff, folds=[asdict(f) for f in folds],
                    min_resolved_training_sessions=MIN_TRAIN_SESSIONS, probability_estimator='empirical resolved-label frequency',
                    comparator='same training dates; all eligible liquid equities; same target and costs',
                    unknown_training='retained in counts and success-fraction bounds; point estimate conditions on resolved labels',
                    test_resolution='actual exit, or blocked entry, strictly before fold end; late outcomes remain unknown',
                    scenarios=['normal', 'stress'], reported_method_horizon_pairs=len(expected_rules)*len(HORIZONS),
                    reported_scenario_comparisons=len(expected_rules)*len(HORIZONS)*2,
                    earlier_research_trials_count=None, multiple_testing_corrected=False, parameter_search=False,
                    current_database_inputs_revalidated=False, prospective_independence=False,
                    holdout_opened=False, accepted_for_signals=False, code_sha256=code_sha,
                    source_files=archive_research_code(folder / 'code'))
    serialized = json.dumps(_json_safe(protocol), ensure_ascii=False, allow_nan=False)
    if db.log_experiment(experiment_id + '-PROTOCOL', 'probability-protocol', 'universe', serialized, '{}',
                         start_date=start, end_date=cutoff, dataset_version=source_id,
                         features_version='pinned-baseline', model_version=VERSION) is None:
        raise RuntimeError('ثبت پروتکل احتمال پیش از برازش ناموفق بود')
    _dump(folder / 'protocol.json', protocol)
    all_ledgers, all_models = [], []
    for index, fold in enumerate(folds):
        train = feature_rows.loc[(feature_rows.date >= fold.train_start) & (feature_rows.date < fold.train_end_exclusive)]
        test = feature_rows.loc[(feature_rows.date >= fold.test_start) & (feature_rows.date < fold.test_end_exclusive)]
        train_picks = {s.name: select_opportunities(train, s.name, k) for s in STRATEGIES}
        test_picks = {s.name: select_opportunities(test, s.name, k) for s in STRATEGIES}
        forecasts, models = [], []
        for horizon in HORIZONS:
            training_outcomes = _outcomes_for(train, inputs['labels'], horizon, fold.train_end_exclusive)
            base_models = {mode: fit_frequency(training_outcomes, mode) for mode in ('normal', 'stress')}
            for rule in STRATEGIES:
                picked = _subset(training_outcomes, train_picks[rule.name])
                fitted = {mode: fit_frequency(picked, mode) for mode in ('normal', 'stress')}
                models.append(dict(fold=index, strategy=rule.name, horizon=horizon,
                                   train_end_exclusive=fold.train_end_exclusive, models=fitted, base_models=base_models))
                forecasts.append(forecast_opportunities(test_picks[rule.name], fitted, base_models,
                    fold_index=index, horizon=horizon, strategy=rule.name, train_end_exclusive=fold.train_end_exclusive))
        forecast = pd.concat(forecasts, ignore_index=True)
        forecast_path = folder / f'fold-{index:02d}-forecasts.parquet'
        forecast.to_parquet(forecast_path, index=False, compression='zstd')
        _dump(folder / f'fold-{index:02d}-models.json', models)
        frozen_forecast_sha = _sha256(forecast_path)
        # از این نقطه تازه نتیجه آزمون همان برش به پیش‌بینی‌های محفوظ پیوند می‌خورد.
        ledgers = []
        for horizon in HORIZONS:
            outcomes = _outcomes_for(test, inputs['labels'], horizon, fold.test_end_exclusive)
            ledgers.append(forecast.loc[forecast.horizon == horizon].merge(outcomes, on=['date', 'symbol'],
                                                                         how='left', validate='many_to_one'))
        ledger = pd.concat(ledgers, ignore_index=True)
        if _sha256(forecast_path) != frozen_forecast_sha:
            raise ValueError('پیش‌بینی محفوظ حین پیوند نتیجه تغییر کرد')
        ledger.to_parquet(folder / f'fold-{index:02d}-outcomes.parquet', index=False, compression='zstd')
        all_ledgers.append(ledger)
        all_models.extend(models)
    ledger = pd.concat(all_ledgers, ignore_index=True)
    summaries = []
    for horizon in HORIZONS:
        for rule in STRATEGIES:
            selected = ledger.loc[(ledger.horizon == horizon) & (ledger.strategy == rule.name)]
            summaries.append(dict(strategy=rule.name, label_fa=rule.label_fa, horizon=horizon,
                sessions_including_entry=horizon+1, selected_decision_sessions=int(selected.date.nunique()),
                test_sessions=sum(len(dev_calendar[(dev_calendar >= f.test_start) & (dev_calendar < f.test_end_exclusive)]) for f in folds),
                normal=score_forecasts(selected, 'normal'), stress=score_forecasts(selected, 'stress')))
    if research_code_fingerprint() != code_sha:
        raise ValueError('کد احتمال حین آزمایش تغییر کرد؛ نتیجه منتشر نشد')
    if _sha256(source_path) != source_report_sha:
        raise ValueError('گزارش منبع حین آزمایش تغییر کرد؛ نتیجه منتشر نشد')
    artifacts = {p.name: _sha256(p) for p in folder.iterdir() if p.is_file()}
    payload = dict(experiment_id=experiment_id, version=VERSION, created_at=now_tehran_iso(), protocol=protocol,
                   summary=summaries, artifacts_sha256=artifacts, model_records=len(all_models),
                   forecast_rows=len(ledger), accepted_for_signals=False, probability_of_real_trade_profit=None,
                   blockers=['development_data_already_seen', 'unresolved_outcomes_and_informative_censoring',
                             'historical_universe_and_actual_corporate_entitlements_unverified',
                             'queue_fills_capacity_and_shared_cash_not_modelled_by_probability_lab',
                             'historical_fees_and_independent_future_calibration_unverified',
                             'multiple_testing_not_corrected', 'regime_stability_unproven'],
                   limitations=['probabilities are constant within each rule/horizon/fold; not stock-specific forecasts',
                                'Brier skill measures forecast error, not return, calibration alone, or signal admission',
                                'unknown bounds describe missing outcomes, not confidence or future-profit intervals',
                                'normal/stress outcomes reuse a dated frozen daily-proxy input generation',
                                'no best rule is selected; all registered comparisons remain visible'])
    if db.log_experiment(experiment_id, 'probability-evaluation', 'universe', serialized,
                        json.dumps(_json_safe(summaries), ensure_ascii=False, allow_nan=False),
                        start_date=start, end_date=cutoff, dataset_version=source_id,
                        features_version='pinned-baseline', model_version=VERSION) is None:
        raise RuntimeError('ثبت نتیجه احتمال ناموفق بود؛ اشاره‌گر آخرین نتیجه تغییر نکرد')
    _dump(root / (experiment_id + '.json'), payload)
    _dump(root / 'latest-probability.json', dict(experiment_id=experiment_id))
    return payload


def load_probability_report(db, experiment_id, *, root=None):
    root = Path(root) if root is not None else settings.DATA_DIR / 'research'
    if not isinstance(experiment_id, str) or not re.fullmatch(EXPERIMENT_PATTERN, experiment_id):
        raise ValueError('شناسه صریح آزمایش احتمال معتبر نیست')
    report = _load_json(root / (experiment_id + '.json'))
    protocol = report['protocol']
    if (report['version'] != VERSION or protocol['version'] != VERSION or report['experiment_id'] != experiment_id
            or report['accepted_for_signals'] is not False or protocol['accepted_for_signals'] is not False
            or protocol['holdout_opened'] is not False or protocol['prospective_independence'] is not False
            or protocol['end_exclusive'] > settings.RESEARCH_CONFIG['holdout_start']):
        raise ValueError('قرارداد آزمایش احتمال یا مرز توسعه معتبر نیست')
    folder = root / experiment_id
    _verify_inventory(folder / 'code', protocol['source_files'])
    actual = {p.name: _sha256(p) for p in folder.iterdir() if p.is_file()}
    if actual != report['artifacts_sha256'] or _load_json(folder / 'protocol.json') != protocol:
        raise ValueError('شواهد یا پروتکل احتمال تغییر کرده است')
    for name, expected_metrics in ((experiment_id, report['summary']), (experiment_id + '-PROTOCOL', {})):
        row = db.conn.execute('SELECT * FROM experiments WHERE experiment_id=? AND status=?', (name, 'completed')).fetchone()
        if row is None or json.loads(row['parameters_json']) != protocol or json.loads(row['metrics_json']) != expected_metrics:
            raise ValueError('ثبت پروتکل/نتیجه احتمال ناسازگار است')
    ledgers = []
    for index, fold in enumerate(protocol['folds']):
        forecasts = pd.read_parquet(folder / f'fold-{index:02d}-forecasts.parquet')
        outcomes = pd.read_parquet(folder / f'fold-{index:02d}-outcomes.parquet')
        try:
            pd.testing.assert_frame_equal(forecasts.reset_index(drop=True), outcomes[list(forecasts)].reset_index(drop=True),
                                          check_dtype=False, check_exact=True)
        except AssertionError as exc:
            raise ValueError('پیش‌بینی قبل از نتیجه با نسخه پیوندخورده سازگار نیست') from exc
        if (forecasts.duplicated(['date', 'symbol', 'strategy', 'horizon']).any()
                or not forecasts.strategy.isin(protocol['strategies']).all()
                or not forecasts.horizon.isin(HORIZONS).all()
                or not forecasts.train_end_exclusive.eq(fold['train_end_exclusive']).all()
                or any(not is_canonical_iso_date(d) for d in forecasts.date.unique())
                or forecasts.date.lt(fold['test_start']).any() or forecasts.date.ge(fold['test_end_exclusive']).any()
                or any(not pd.isna(at) and (not is_canonical_iso_date(at) or at >= fold['test_end_exclusive'])
                       for at in outcomes.resolved_at)):
            raise ValueError('هویت یا مرز زمانی پیش‌بینی/نتیجه ناسازگار است')
        models = _load_json(folder / f'fold-{index:02d}-models.json')
        if {(m['strategy'], m['horizon']) for m in models} != {(s, h) for s in protocol['strategies'] for h in HORIZONS}:
            raise ValueError('مدل یکی از قواعد از شواهد حذف شده است')
        for model in models:
            subset = forecasts.loc[(forecasts.strategy == model['strategy']) & (forecasts.horizon == model['horizon'])]
            expected = forecast_opportunities(subset[['date', 'symbol', '_score']], model['models'], model['base_models'],
                fold_index=index, horizon=model['horizon'], strategy=model['strategy'], train_end_exclusive=fold['train_end_exclusive'])
            try:
                pd.testing.assert_frame_equal(subset.reset_index(drop=True), expected.reset_index(drop=True), check_dtype=False,
                                              check_exact=True)
            except AssertionError as exc:
                raise ValueError('پیش‌بینی با مدل محفوظ قبل از نتیجه سازگار نیست') from exc
        ledgers.append(outcomes)
    ledger = pd.concat(ledgers, ignore_index=True)
    if (report['forecast_rows'] != len(ledger) or report['model_records'] != len(protocol['folds'])*len(protocol['strategies'])*len(HORIZONS)
            or len(report['summary']) != len(protocol['strategies'])*len(HORIZONS)
            or {(s['strategy'], s['horizon']) for s in report['summary']} != {
                (s, h) for s in protocol['strategies'] for h in HORIZONS}):
        raise ValueError('شمار فرصت، مدل یا مقایسه‌های گزارش ناسازگار است')
    for summary in report['summary']:
        selected = ledger.loc[(ledger.strategy == summary['strategy']) & (ledger.horizon == summary['horizon'])]
        if any(_json_safe(score_forecasts(selected, mode)) != summary[mode] for mode in ('normal', 'stress')):
            raise ValueError('آمار احتمال با پیش‌بینی/نتیجه محفوظ ناسازگار است')
    if actual != {p.name: _sha256(p) for p in folder.iterdir() if p.is_file()}:
        raise ValueError('شواهد احتمال هنگام بازبینی تغییر کرد')
    report['local_evidence_verified'] = True
    report['scores_recomputed'] = True
    return report
