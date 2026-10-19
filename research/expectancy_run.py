"""پژوهش بازده شرطی با سرمایه مشترک؛ مدل و نامزد قبل از نتیجه محفوظ می‌شوند."""

import argparse
import json
import math
import re
import shutil
import uuid
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.portfolio import PORTFOLIO_VERSION, PortfolioConfig, replay_portfolio
from config import settings
from core.research_review import mode_summary, paired_comparison
from core.timeutils import is_canonical_iso_date, now_tehran_iso
from ops.research_review import _checked_mode, _load_json, _verify_inventory, read_development_parquet
from research.baselines import STRATEGIES
from research.expectancy import (
    MIN_ROWS,
    MIN_SESSIONS,
    PREDICTORS,
    RIDGE_PENALTY,
    fit_expectancy,
    fixed_hypothesis_masks,
    forecast_errors,
    forecast_expectancy,
)
from research.expectancy_diagnostics import checked_workers, compare_jobs
from research.flow_run import _summary
from research.flow_v4_run import observed_market_curve
from research.labels import market_calendar
from research.portfolio_run import _picks, select_from_past, verified_baseline_inputs
from research.probability import opportunity_features
from research.probability_run import _dump, _outcomes_for
from research.run import _deduplicate_input, _json_safe, _sha256, archive_research_code, research_code_fingerprint
from research.walkforward import TimeFold, time_folds

VERSION = 'expectancy-portfolio-v3'
CANDIDATES = ('conditional-2d', 'conditional-1d', 'relative-trend-2d', 'orderly-pullback-2d')
SEEDS = (41, 97, 173)
PATTERN = r'EXP-\d{4}-\d{2}-\d{2}-EXPECTANCY-[a-f0-9]{32}'


def expected_modes():
    modes = {'past_baseline', 'past_baseline_stress', 'idle_cash'}
    for candidate in CANDIDATES:
        modes.update((candidate, candidate + '-stress'))
        for seed in SEEDS:
            modes.update((f'{candidate}-random{seed}', f'{candidate}-random{seed}-stress'))
    return modes


def _fold_uncertainties(curves, controls, days, index, config, parameters, workers):
    names = [(candidate+suffix, control) for candidate in CANDIDATES
             for suffix in ('', '-stress') for control in controls]
    requests = [(curves[name], controls[control], dict(initial_capital_rial=config.initial_capital_rial,
        expected_sessions=list(days), block_lengths=parameters['block_lengths'],
        replications=parameters['replications'], trials_attempted=parameters['trials_attempted'],
        family_comparisons=parameters['family_comparisons'])) for name, control in names]
    diagnostics = compare_jobs(requests, workers=workers)
    return [dict(fold=index, mode=name, benchmark=control, result=result)
            for (name, control), result in zip(names, diagnostics, strict=True)]


def _rank(frame, score, mask, k):
    eligible = frame.loc[mask, ['date', 'symbol']].assign(score=score.loc[mask])
    eligible = eligible.loc[np.isfinite(eligible.score)].sort_values(['date', 'score', 'symbol'], ascending=[True, False, True])
    eligible['rank'] = eligible.groupby('date').cumcount()
    return eligible.loc[eligible['rank'] < k, ['date', 'symbol', 'rank']].reset_index(drop=True)


def _random(pool, selected, k, seed):
    counts = selected.groupby('date').size()
    chosen = _picks(pool, None, k, seed)
    chosen = chosen.loc[chosen['rank'] < chosen.date.map(counts).fillna(0)]
    if chosen.groupby('date').size().to_dict() != counts.to_dict():
        raise ValueError('شمار روزانه نامزد و کنترل تصادفی برابر نیست')
    return chosen


def _fold_jobs(train, test, forecast, training_labels, decision_prices, calendar, fold, config, index):
    """انتخاب فقط از پیش بینی، خام روز تصمیم و نتیجه آموزش؛ بدون نتیجه آزمون."""
    if training_labels.date.ge(fold.train_end_exclusive).any():
        raise ValueError('نتیجه آزمون نباید در انتخاب نامزد یا پایه در دسترس باشد')
    days = calendar[(calendar >= fold.test_start) & (calendar < fold.test_end_exclusive)]
    selection_end = days[max(0, len(days) - 3)]
    score = forecast[['normal_expected_net', 'stress_expected_net']].min(axis=1, skipna=False)
    gate = forecast.normal_expected_net.gt(0) & forecast.stress_expected_net.gt(0) & forecast.date.lt(selection_end)
    conditional = _rank(forecast, score, gate, config.max_positions)
    masks = fixed_hypothesis_masks(test, decision_prices)
    picks = {'conditional-2d': conditional, 'conditional-1d': conditional.copy(),
        'relative-trend-2d': _rank(test, test.rel_ret_5d, masks['relative_trend'] & test.date.lt(selection_end), config.max_positions),
        'orderly-pullback-2d': _rank(test, test.value, masks['orderly_pullback'] & test.date.lt(selection_end), config.max_positions)}
    pool = test.loc[test.date.lt(selection_end)]
    panel = train.merge(training_labels.drop(columns=['value']), on=['date', 'symbol'], validate='one_to_one')
    chosen, training = select_from_past(panel, fold, (1, 2), config.max_positions)
    baseline = _picks(pool, chosen['strategy'], config.max_positions) if chosen else pd.DataFrame(columns=['date', 'symbol', 'rank'])
    jobs = dict(idle_cash=(pd.DataFrame(columns=['date', 'symbol', 'rank']), config),
        past_baseline=(baseline, replace(config, horizon=chosen['horizon'] if chosen else 2)),
        past_baseline_stress=(baseline, replace(config, horizon=chosen['horizon'] if chosen else 2, stress=True)))
    for name, selected in picks.items():
        horizon = 1 if name == 'conditional-1d' else 2
        for stress in (False, True):
            suffix = '-stress' if stress else ''
            jobs[name + suffix] = (selected, replace(config, horizon=horizon, stress=stress))
            for seed in SEEDS:
                jobs[f'{name}-random{seed}' + suffix] = (_random(pool, selected, config.max_positions, seed+index), replace(config, horizon=horizon, stress=stress))
    if set(jobs) != expected_modes():
        raise ValueError('حالت‌های اجرا با پروتکل برابر نیستند')
    return jobs, chosen, training, days


def run_expectancy(db, *, source_id, trials_attempted, root=None, replications=20000,
                   min_train_sessions=504, test_sessions=252, diagnostic_workers=1):
    checked_workers(diagnostic_workers)
    if source_id is None:
        raise ValueError('منبع BASELINES صریح لازم است')
    root = Path(root) if root is not None else settings.DATA_DIR / 'research'
    source_id, inputs, digests = verified_baseline_inputs(root, source_id)
    source_file = root / (source_id + '.json')
    source_sha = _sha256(source_file)
    source = _load_json(source_file)
    if _sha256(source_file) != source_sha:
        raise ValueError('گزارش منبع هنگام خواندن تغییر کرد')
    params = source['params']
    minimum_trials = len(expected_modes()) + len(params['strategies']) * len(params['horizons'])
    if type(trials_attempted) is not int or trials_attempted < minimum_trials:
        raise ValueError(f'تعداد صریح کل تلاش‌ها لازم است؛ حداقل دامنه این اجرا {minimum_trials} حالت است')
    if type(replications) is not int or not 1000 <= replications <= 100000:
        raise ValueError('بازنمونه‌گیری باید بین هزار و صد هزار باشد')
    if params['strategies'] != [s.name for s in STRATEGIES] or not {1, 2} <= set(params['horizons']):
        raise ValueError('هر دو افق۱ و۲ در منبع پایه لازم‌اند')
    start, cutoff = params['periods']['development']
    if (not is_canonical_iso_date(start) or not is_canonical_iso_date(cutoff) or start >= cutoff
            or start < settings.RESEARCH_CONFIG['dev_start'] or cutoff > settings.RESEARCH_CONFIG['holdout_start']
            or params.get('holdout_selection')):
        raise ValueError('منبع خارج از دامنه توسعه مجاز است')
    if (type(params['top_k']) is not int or not 1 <= params['top_k'] <= 50
            or isinstance(params['min_daily_value_rial'], bool)
            or not isinstance(params['min_daily_value_rial'], (int, float))
            or not math.isfinite(params['min_daily_value_rial']) or params['min_daily_value_rial'] < 0):
        raise ValueError('تعداد نامزد یا نقدشوندگی منبع نامعتبر است')
    registered = db.conn.execute('SELECT parameters_json FROM experiments WHERE experiment_id=? AND status=?',
                                 (source_id, 'completed')).fetchone()
    if registered is None or json.loads(registered['parameters_json']) != params:
        raise ValueError('ثبت منبع پایه با فایل محفوظ سازگار نیست')
    features = opportunity_features(inputs['features'], inputs['labels'], prices=inputs['prices'])
    nontrading = features.attrs['observed_nontrading_feature_rows']
    features = features.loc[features.date.ge(start) & features.date.lt(cutoff) & features.value.ge(params['min_daily_value_rial'])].reset_index(drop=True)
    calendar = market_calendar(inputs['prices'])
    folds = time_folds(calendar[(calendar >= start) & (calendar < cutoff)], min_train_sessions=min_train_sessions,
                       test_sessions=test_sessions, embargo_sessions=3, end_exclusive=cutoff)
    capital = settings.BACKTEST_CONFIG['initial_capital']
    config = PortfolioConfig(initial_capital_rial=capital, max_positions=params['top_k'], max_loss_pct=.10,
        slippage_bps=settings.BACKTEST_CONFIG['default_slippage_bps'],
        participation=min(.05, settings.BACKTEST_CONFIG['max_volume_participation_pct']))
    family = max(trials_attempted * 3, len(folds) * len(CANDIDATES) * 2 * 2 * 3)
    code = research_code_fingerprint()
    experiment = f'EXP-{now_tehran_iso()[:10]}-EXPECTANCY-{uuid.uuid4().hex}'
    folder = root / experiment
    folder.mkdir(parents=True, exist_ok=False)
    for name, digest in digests.items():
        target = folder / (name + '.parquet')
        shutil.copyfile(root / source_id / target.name, target)
        if _sha256(target) != digest:
            raise ValueError('تصویر ورودی هنگام کپی تغییر کرد')
        _deduplicate_input(target, root / 'objects')
    protocol = dict(version=VERSION, source_id=source_id, source_report_sha256=source_sha, inputs=digests,
        observed_nontrading_feature_rows=nontrading,
        development_start=start, min_daily_value_rial=params['min_daily_value_rial'],
        portfolio_version=PORTFOLIO_VERSION, config=asdict(config), predictors=list(PREDICTORS), penalty=RIDGE_PENALTY,
        min_model_sessions=MIN_SESSIONS, min_model_rows=MIN_ROWS, primary_candidate='conditional-2d',
        candidate_modes=list(CANDIDATES), modes=sorted(expected_modes()), random_seeds=list(SEEDS),
        model_target='normal and stress net returns at horizon2; equal total training weight per decision date',
        model_support='complete-case training; no imputation; no prediction outside observed training range',
        prediction_error_primary_weight='equal total weight per resolved forecast date',
        archive_review='rebuild all picks and configurations, replay all execution curves, rerun uncertainty',
        diagnostic_workers=diagnostic_workers, diagnostics_scheduling='spawn; ordered independent comparisons; unchanged per-comparison seed',
        selection='conditional normal and stress expected net >0; rank minimum estimate, symbol tie; top-k',
        exit_control='conditional-1d uses exactly the conditional-2d decision picks; no adaptive live exit claim',
        fixed_hypotheses='docs/research/conditional-expectancy-protocol-fa.md',
        folds=[asdict(f) for f in folds], holdout_start=cutoff, prospective_independence=False,
        windows=dict(min_train_sessions=min_train_sessions, test_sessions=test_sessions, embargo_sessions=3),
        source_strategy_horizon_pairs=len(params['strategies']) * len(params['horizons']),
        uncertainty=dict(block_lengths=[5, 10, 20], replications=replications, trials_attempted=trials_attempted,
                         family_comparisons=family, historical_total_trials_verified=False),
        modes_in_this_run=len(expected_modes()), minimum_disclosed_trials=minimum_trials,
        accepted_for_signals=False, holdout_eligible=False, code_sha256=code, source_files=archive_research_code(folder / 'code'))
    encoded = json.dumps(_json_safe(protocol), ensure_ascii=False, allow_nan=False)
    if db.log_experiment(experiment + '-PROTOCOL', 'expectancy-protocol', 'universe', encoded, '{}',
        start_date=start, end_date=cutoff, dataset_version=source_id, features_version='daily-v7', model_version=VERSION) is None:
        raise RuntimeError('ثبت پروتکل قبل از برازش ناموفق بود')
    _dump(folder / 'protocol.json', protocol)
    accumulated, results, uncertainties, errors = {}, [], [], []
    for index, fold in enumerate(folds):
        train = features.loc[features.date.ge(fold.train_start) & features.date.lt(fold.train_end_exclusive)]
        test = features.loc[features.date.ge(fold.test_start) & features.date.lt(fold.test_end_exclusive)]
        training_outcomes = _outcomes_for(train, inputs['labels'], 2, fold.train_end_exclusive)
        model = fit_expectancy(train, training_outcomes, train_end_exclusive=fold.train_end_exclusive)
        forecast = forecast_expectancy(test, model)
        _dump(folder / f'fold-{index:02d}-model.json', model)
        forecast_path = folder / f'fold-{index:02d}-forecasts.parquet'
        forecast.to_parquet(forecast_path, index=False, compression='zstd')
        forecast_hash = _sha256(forecast_path)
        training_labels = inputs['labels'].loc[inputs['labels'].date.lt(fold.train_end_exclusive)]
        jobs, chosen, training, days = _fold_jobs(train, test, forecast, training_labels, inputs['prices'],
                                                 calendar, fold, config, index)
        picks_hashes = {}
        for name, (selected, _) in jobs.items():
            path = folder / f'fold-{index:02d}-{name}-picks.parquet'
            selected.to_parquet(path, index=False, compression='zstd')
            picks_hashes[path.name] = _sha256(path)
        # اولین اتصال نتیجه آزمون پس از حفظ مدل و پیش‌بینی؛ مجهول پایان برش حفظ می‌شود.
        testing_outcomes = _outcomes_for(test, inputs['labels'], 2, fold.test_end_exclusive)
        errors.append(dict(fold=index, scores=forecast_errors(forecast, testing_outcomes, model)))
        testing_outcomes.to_parquet(folder / f'fold-{index:02d}-outcomes.parquet', index=False, compression='zstd')
        prior = inputs['prices'].loc[inputs['prices'].date < fold.test_start].groupby('symbol', sort=False).tail(1)
        prices = pd.concat([prior, inputs['prices'].loc[inputs['prices'].date.isin(days)]], ignore_index=True)
        record, curves = dict(fold=asdict(fold), chosen_past_baseline=chosen, training=training, modes={}), {}
        for name, (selected, cfg) in jobs.items():
            replay = replay_portfolio(prices, selected, calendar, cfg, start=fold.test_start, end=fold.test_end_exclusive)
            replay.metrics['max_loss_from_initial'] = None if replay.metrics['total_return'] is None else max(0., 1-float(replay.equity.equity_rial.min())/capital)
            for kind, frame in dict(equity=replay.equity, executions=replay.executions, decisions=replay.decisions).items():
                frame.to_parquet(folder / f'fold-{index:02d}-{name}-{kind}.parquet', index=False, compression='zstd')
            record['modes'][name] = dict(metrics=replay.metrics, assumptions=replay.assumptions)
            accumulated.setdefault(name, []).append(replay.metrics)
            curves[name] = replay.equity.set_index('date').reindex(days).rename_axis('date').reset_index()[['date', 'equity_rial']]
        controls = dict(idle_cash=curves['idle_cash'], observed_equal_weight_gross_index=observed_market_curve(inputs['prices'], days, capital))
        for control, curve in controls.items():
            curve.to_parquet(folder / f'benchmark-{index:02d}-{control}.parquet', index=False)
        fold_uncertainties = _fold_uncertainties(curves, controls, days, index, config, protocol['uncertainty'], diagnostic_workers)
        uncertainties.extend(fold_uncertainties)
        if _sha256(forecast_path) != forecast_hash or any(_sha256(folder / name) != h for name, h in picks_hashes.items()):
            raise ValueError('پیش‌بینی پس از مشاهده نتیجه تغییر کرد')
        results.append(record)
        _dump(folder / f'fold-{index:02d}-checkpoint.json',
              dict(result=record, prediction_error=errors[-1], uncertainty=fold_uncertainties))
        print(f'برش بازده شرطی {index+1}/{len(folds)} پایان یافت.', flush=True)
    if research_code_fingerprint() != code or _sha256(source_file) != source_sha:
        raise ValueError('کد یا منبع هنگام اجرا تغییر کرد')
    for name, digest in digests.items():
        if _sha256(folder / (name + '.parquet')) != digest:
            raise ValueError('ورودی محفوظ هنگام اجرا تغییر کرد')
    summaries = {name: dict(_summary(rows), all_fold_mean=None if any(r['total_return'] is None for r in rows) else
        float(np.mean([r['total_return'] for r in rows])), corporate_action_events=sum(r['corporate_action_events'] for r in rows)) for name, rows in accumulated.items()}
    _dump(folder / 'uncertainty.json', uncertainties)
    _dump(folder / 'prediction-errors.json', errors)
    payload = dict(experiment_id=experiment, protocol=protocol, results=results, summary=summaries,
        accepted_for_signals=False, holdout_eligible=False,
        uncertainty_sha256=_sha256(folder / 'uncertainty.json'), prediction_errors_sha256=_sha256(folder / 'prediction-errors.json'),
        models_sha256={p.name: _sha256(p) for p in folder.glob('fold-*-model.json')},
        checkpoints_sha256={p.name: _sha256(p) for p in folder.glob('fold-*-checkpoint.json')},
        artifacts_sha256={p.name: _sha256(p) for p in folder.glob('*.parquet')})
    if db.log_experiment(experiment, 'conditional-expectancy', 'universe', encoded,
        json.dumps(_json_safe(summaries), ensure_ascii=False, allow_nan=False), start_date=start, end_date=cutoff,
        dataset_version=source_id, features_version='daily-v7', model_version=VERSION) is None:
        raise RuntimeError('ثبت نتیجه ناموفق بود')
    _dump(root / (experiment + '.json'), payload)
    review = review_expectancy(db, experiment, root=root)
    _dump(folder / 'review.json', review)
    (root / (experiment + '.md')).write_text(format_expectancy(review), encoding='utf-8')
    _dump(root / 'latest-expectancy.json', dict(experiment_id=experiment))
    return dict(payload, review=review, report_path=str(root / (experiment + '.md')))


def _assert_frame(actual, expected, message):
    # Parquet برای جدول کاملا خالی، نوع شاخص ستون را از RangeIndex به Index تبدیل می کند.
    if actual.shape == expected.shape == (0, 0):
        return
    try:
        pd.testing.assert_frame_equal(actual.reset_index(drop=True), expected.reset_index(drop=True),
                                      check_dtype=False, rtol=1e-9, atol=1e-12)
    except AssertionError as exc:
        raise ValueError(message) from exc


def _same_json(actual, expected):
    if isinstance(expected, dict):
        return (isinstance(actual, dict) and set(actual) == set(expected)
                and all(_same_json(actual[k], v) for k, v in expected.items()))
    if isinstance(expected, list):
        return (isinstance(actual, list) and len(actual) == len(expected)
                and all(_same_json(a, b) for a, b in zip(actual, expected)))
    if isinstance(expected, float):
        return (type(actual) in (int, float) and math.isfinite(actual)
                and math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-12))
    return type(actual) is type(expected) and actual == expected


def review_expectancy(db, experiment_id, *, root=None):
    review_code = research_code_fingerprint()
    root = Path(root) if root is not None else settings.DATA_DIR / 'research'
    if not isinstance(experiment_id, str) or not re.fullmatch(PATTERN, experiment_id):
        raise ValueError('شناسه آزمایش بازده شرطی نامعتبر است')
    path, folder = root / (experiment_id + '.json'), root / experiment_id
    digest, payload = _sha256(path), _load_json(path)
    p = payload['protocol']
    checked_workers(p.get('diagnostic_workers'))
    if (p['version'] != VERSION or p['holdout_start'] > settings.RESEARCH_CONFIG['holdout_start']
            or p['modes'] != sorted(expected_modes()) or p['candidate_modes'] != list(CANDIDATES)
            or p['random_seeds'] != list(SEEDS) or p['primary_candidate'] != 'conditional-2d'
            or p['predictors'] != list(PREDICTORS) or p['penalty'] != RIDGE_PENALTY
            or p['min_model_sessions'] != MIN_SESSIONS or p['min_model_rows'] != MIN_ROWS
            or p.get('archive_review') != 'rebuild all picks and configurations, replay all execution curves, rerun uncertainty'
            or p.get('diagnostics_scheduling') != 'spawn; ordered independent comparisons; unchanged per-comparison seed'
            or p['prospective_independence'] is not False or payload['accepted_for_signals'] is not False
            or payload['holdout_eligible'] is not False or _load_json(folder / 'protocol.json') != p):
        raise ValueError('قرارداد یا مرز آزمایش ناسازگار است')
    for name, metrics in ((experiment_id, payload['summary']), (experiment_id+'-PROTOCOL', {})):
        row = db.conn.execute('SELECT * FROM experiments WHERE experiment_id=? AND status=?', (name, 'completed')).fetchone()
        if row is None or json.loads(row['parameters_json']) != p or json.loads(row['metrics_json']) != metrics:
            raise ValueError('ثبت آزمایش با فایل محفوظ سازگار نیست')
    artifacts = _verify_inventory(folder, payload['artifacts_sha256'], parquet=True)
    if (set(p['inputs']) != {'features', 'labels', 'prices'} or
            any(artifacts.get(name+'.parquet') != h for name, h in p['inputs'].items()) or
            set(payload['models_sha256']) != {f'fold-{i:02d}-model.json' for i in range(len(p['folds']))} or
            set(payload['checkpoints_sha256']) != {f'fold-{i:02d}-checkpoint.json' for i in range(len(p['folds']))}):
        raise ValueError('فهرست ورودی یا مدل‌ها با پروتکل سازگار نیست')
    _verify_inventory(folder / 'code', p['source_files'])
    def sidecars():
        for file, expected in dict(**payload['models_sha256'], **payload['checkpoints_sha256'], **{'uncertainty.json': payload['uncertainty_sha256'],
                'prediction-errors.json': payload['prediction_errors_sha256']}).items():
            if _sha256(folder / file) != expected:
                raise ValueError('مدل یا گزارش جانبی تغییر کرده است')
    sidecars()
    if len(payload['results']) != len(p['folds']) or not p['folds']:
        raise ValueError('برش حذف شده یا خالی است')
    frozen = {name: read_development_parquet(folder / (name+'.parquet'), h, p['holdout_start'])
              for name, h in p['inputs'].items()}
    features = opportunity_features(frozen['features'], frozen['labels'], prices=frozen['prices'])
    if features.attrs['observed_nontrading_feature_rows'] != p['observed_nontrading_feature_rows']:
        raise ValueError('شمار عدم معامله مشاهده‌شده با پروتکل برابر نیست')
    features = features.loc[features.date.ge(p['development_start']) & features.date.lt(p['holdout_start'])
                            & features.value.ge(p['min_daily_value_rial'])].reset_index(drop=True)
    calendar = market_calendar(frozen['prices'])
    expected_folds = time_folds(calendar[(calendar >= p['development_start']) & (calendar < p['holdout_start'])],
                               end_exclusive=p['holdout_start'], **p['windows'])
    if (p['windows']['embargo_sessions'] != 3 or p['folds'] != [asdict(fold) for fold in expected_folds]
            or p['modes_in_this_run'] != len(expected_modes())
            or p['minimum_disclosed_trials'] != len(expected_modes()) + p['source_strategy_horizon_pairs']
            or p['uncertainty']['trials_attempted'] < p['minimum_disclosed_trials']):
        raise ValueError('پنجره زمانی یا شمار حالت های ثبت شده سازگار نیست')
    config = PortfolioConfig(**p['config'])
    uncertainty_config = p['uncertainty']
    expected_family = max(uncertainty_config['trials_attempted'] * 3, len(p['folds']) * len(CANDIDATES) * 2 * 2 * 3)
    if (uncertainty_config['family_comparisons'] != expected_family
            or uncertainty_config['block_lengths'] != [5, 10, 20]
            or uncertainty_config['historical_total_trials_verified'] is not False
            or p['accepted_for_signals'] is not False or p['holdout_eligible'] is not False):
        raise ValueError('خانواده مقایسه یا محدودیت پژوهش با قرارداد برابر نیست')
    errors = _load_json(folder / 'prediction-errors.json')
    if len(errors) != len(p['folds']):
        raise ValueError('گزارش خطای پیش‌بینی یک برش موجود نیست')
    checked, last_end, recomputed_uncertainties = [], None, []
    for index, (fold, result) in enumerate(zip(p['folds'], payload['results'])):
        if (fold != result['fold'] or not fold['train_start'] < fold['train_end_exclusive'] < fold['test_start'] < fold['test_end_exclusive'] <= p['holdout_start']
                or last_end is not None and fold['test_start'] < last_end or set(result['modes']) != expected_modes()):
            raise ValueError('ترتیب یا حالت‌های برش ناسازگار است')
        last_end = fold['test_end_exclusive']
        model = _load_json(folder / f'fold-{index:02d}-model.json')
        train = features.loc[features.date.ge(fold['train_start']) & features.date.lt(fold['train_end_exclusive'])]
        test = features.loc[features.date.ge(fold['test_start']) & features.date.lt(fold['test_end_exclusive'])]
        expected_model = fit_expectancy(train, _outcomes_for(train, frozen['labels'], 2, fold['train_end_exclusive']),
                                       train_end_exclusive=fold['train_end_exclusive'])
        arrays = {'coefficients', 'center', 'scale', 'minimum', 'maximum', 'unconditioned_mean'}
        if set(model) != set(expected_model):
            raise ValueError('مدل با قرارداد برازش گذشته سازگار نیست')
        for key, value in expected_model.items():
            if key in arrays and value is not None:
                try:
                    np.testing.assert_allclose(model[key], value, rtol=1e-9, atol=1e-12)
                except (AssertionError, TypeError, ValueError) as exc:
                    raise ValueError('مدل با برازش مجدد گذشته سازگار نیست') from exc
            elif model[key] != value:
                raise ValueError('مدل با برازش مجدد گذشته سازگار نیست')
        predicted = forecast_expectancy(test, model)
        file = folder / f'fold-{index:02d}-forecasts.parquet'
        saved = read_development_parquet(file, artifacts[file.name], p['holdout_start'])
        try:
            pd.testing.assert_frame_equal(saved.reset_index(drop=True), predicted.reset_index(drop=True),
                                          check_dtype=False, rtol=1e-9, atol=1e-12)
        except AssertionError as exc:
            raise ValueError('پیش‌بینی با مدل قبل از نتیجه سازگار نیست') from exc
        actual_outcomes = _outcomes_for(test, frozen['labels'], 2, fold['test_end_exclusive'])
        file = folder / f'fold-{index:02d}-outcomes.parquet'
        try:
            pd.testing.assert_frame_equal(read_development_parquet(file, artifacts[file.name], p['holdout_start']).reset_index(drop=True),
                                          actual_outcomes.reset_index(drop=True), check_dtype=False, check_exact=True)
        except AssertionError as exc:
            raise ValueError('نتیجه با برچسب و مرز محفوظ سازگار نیست') from exc
        if _json_safe(dict(fold=index, scores=forecast_errors(saved, actual_outcomes, model))) != errors[index]:
            raise ValueError('گزارش خطای پیش‌بینی با محاسبه دوباره سازگار نیست')
        temporal_fold = TimeFold(**fold)
        training_labels = frozen['labels'].loc[frozen['labels'].date.lt(fold['train_end_exclusive'])]
        jobs, chosen, training, days = _fold_jobs(train, test, predicted, training_labels, frozen['prices'],
                                                 calendar, temporal_fold, config, index)
        if not _same_json(result['chosen_past_baseline'], _json_safe(chosen)) or not _same_json(result['training'], _json_safe(training)):
            raise ValueError('انتخاب پایه با نتیجه آموزش پیش از مرز سازگار نیست')
        prior = frozen['prices'].loc[frozen['prices'].date.lt(fold['test_start'])].groupby('symbol', sort=False).tail(1)
        replay_prices = pd.concat([prior, frozen['prices'].loc[frozen['prices'].date.isin(days)]], ignore_index=True)
        curves = {}
        for name, (picks, configuration) in jobs.items():
            file = folder / f'fold-{index:02d}-{name}-picks.parquet'
            _assert_frame(read_development_parquet(file, artifacts[file.name], p['holdout_start'], require_date=False),
                          picks, 'انتخاب محفوظ با مدل، قاعده یا کنترل تصادفی برابر نیست')
            replay = replay_portfolio(replay_prices, picks, calendar, configuration,
                                      start=fold['test_start'], end=fold['test_end_exclusive'])
            replay.metrics['max_loss_from_initial'] = (None if replay.metrics['total_return'] is None else
                max(0., 1-float(replay.equity.equity_rial.min()) / config.initial_capital_rial))
            for kind, frame in dict(equity=replay.equity, executions=replay.executions, decisions=replay.decisions).items():
                file = folder / f'fold-{index:02d}-{name}-{kind}.parquet'
                _assert_frame(read_development_parquet(file, artifacts[file.name], p['holdout_start'],
                              require_date=kind == 'equity'), frame, 'خروجی محفوظ با بازاجرای معاملات برابر نیست')
            if (not _same_json(result['modes'][name]['metrics'], _json_safe(replay.metrics))
                    or not _same_json(result['modes'][name]['assumptions'], _json_safe(replay.assumptions))):
                raise ValueError('آمار یا فرض های اجرا با بازپخش برابر نیست')
            curves[name] = replay.equity.set_index('date').reindex(days).rename_axis('date').reset_index()[['date', 'equity_rial']]
        controls = dict(idle_cash=curves['idle_cash'],
                        observed_equal_weight_gross_index=observed_market_curve(frozen['prices'], days, config.initial_capital_rial))
        for control, curve in controls.items():
            file = folder / f'benchmark-{index:02d}-{control}.parquet'
            _assert_frame(read_development_parquet(file, artifacts[file.name], p['holdout_start']), curve,
                          'معیار محفوظ با محاسبه دوباره برابر نیست')
        fold_uncertainties = _fold_uncertainties(curves, controls, days, index, config, uncertainty_config, p['diagnostic_workers'])
        recomputed_uncertainties.extend(fold_uncertainties)
        checkpoint = dict(result=result, prediction_error=errors[index], uncertainty=fold_uncertainties)
        if not _same_json(_load_json(folder / f'fold-{index:02d}-checkpoint.json'), _json_safe(checkpoint)):
            raise ValueError('نتیجه میانی برش با بازاجرای کامل برابر نیست')
        modes = {name: _checked_mode(folder, index, name, record, fold, p, artifacts) for name, record in result['modes'].items()}
        for name, record in modes.items():
            declared = payload['summary'][name]
            if declared['folds'] != len(p['folds']):
                raise ValueError('مخرج برش‌های خلاصه ناسازگار است')
        checked.append(dict(modes=modes))
    summaries = {name: mode_summary([f['modes'][name]['metrics'] for f in checked]) for name in expected_modes()}
    for name, summary in summaries.items():
        for field, key in (('folds', 'folds'), ('valid_folds', 'known_folds'), ('positive_folds', 'positive_folds'),
                           ('mean_fold_return', 'mean_known_fold_return'), ('worst_fold_drawdown', 'worst_known_drawdown'),
                           ('all_fold_mean', 'all_fold_mean'), ('corporate_action_events', 'corporate_action_events')):
            actual, declared = summary[key], payload['summary'][name][field]
            if (actual is None) != (declared is None) or actual is not None and not np.isclose(actual, declared, rtol=1e-9, atol=1e-10):
                raise ValueError('خلاصه برش‌ها با منحنی ناسازگار است')
    for index in range(len(checked)):
        a = read_development_parquet(folder / f'fold-{index:02d}-conditional-1d-picks.parquet',
            artifacts[f'fold-{index:02d}-conditional-1d-picks.parquet'], p['holdout_start'], require_date=False)
        b = read_development_parquet(folder / f'fold-{index:02d}-conditional-2d-picks.parquet',
            artifacts[f'fold-{index:02d}-conditional-2d-picks.parquet'], p['holdout_start'], require_date=False)
        if not a.equals(b):
            raise ValueError('کنترل خروج باید نامزدهای یکسان داشته باشد')
    sidecars()
    if not _same_json(_load_json(folder / 'uncertainty.json'), _json_safe(recomputed_uncertainties)):
        raise ValueError('گزارش عدم قطعیت با بازنمونه گیری دوباره برابر نیست')
    _verify_inventory(folder, artifacts, parquet=True)
    _verify_inventory(folder / 'code', p['source_files'])
    if (_sha256(path) != digest or _load_json(folder / 'protocol.json') != p
            or research_code_fingerprint() != review_code):
        raise ValueError('گزارش هنگام بررسی تغییر کرد')
    return dict(experiment_id=experiment_id, summaries=summaries,
        comparisons={c: {key: paired_comparison(checked, c, other) for key, other in
            [('stress', c+'-stress'), ('past_baseline', 'past_baseline'), ('idle_cash', 'idle_cash')]}
                     for c in CANDIDATES}, accepted_for_signals=False, holdout_eligible=False,
        curve_metrics_recomputed=True, archived_hashes_verified=True, model_retrained_by_review=True,
        forecasts_and_errors_recomputed=True,
        selections_and_random_controls_recomputed=True, execution_replayed=True, uncertainty_recomputed=True,
        current_review_code_sha256=review_code,
        blockers=['unverified_historical_universe_fees_and_economic_entitlements', 'unknown_or_synthetic_execution',
                  'historical_trial_ledger_and_independent_future_not_established', 'bootstrap_is_diagnostic_not_acceptance'])


def format_expectancy(review):
    def percent(value):
        return 'نامعلوم' if value is None else f'{value*100:+.3f}%'
    lines = ['# پژوهش بازده خالص شرطی', '', review['experiment_id'], '',
        '| روش | برش معلوم/کل | میانگین همه برش‌ها | فشار | عبور اقدام شرکتی عادی/فشار |', '|---|---:|---:|---:|---:|']
    for name in CANDIDATES:
        a, b = review['summaries'][name], review['summaries'][name+'-stress']
        lines.append(f"| {name} | {a['known_folds']}/{a['folds']} | {percent(a['all_fold_mean'])} | {percent(b['all_fold_mean'])} | {a['corporate_action_events']}/{b['corporate_action_events']} |")
    lines += ['', 'میانگین حساب‌های مستقل، CAGR یا احتمال سود نیست؛ مجهول‌ها و عدم اجرا در JSON حفظ شده‌اند.',
              'مدل، پیش‌بینی و کنترل‌ها محفوظ‌اند؛ روش پذیرفته نشده و holdout باز نشده است.']
    return '\n'.join(lines) + '\n'


def main(argv=None):
    from database.db import DatabaseManager

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source')
    parser.add_argument('--review')
    parser.add_argument('--trials-attempted', type=int)
    parser.add_argument('--replications', type=int, default=20000)
    parser.add_argument('--workers', type=int, default=4, help='1 to 4 independent bootstrap processes; same numerical results')
    args = parser.parse_args(argv)
    if args.review and (args.source is not None or args.trials_attempted is not None):
        raise ValueError('بازبینی و اجرای منبع تازه دو کار جدا هستند')
    if not args.review and (args.source is None or args.trials_attempted is None):
        raise ValueError('منبع BASELINES و تعداد صریح کل تلاش ها لازم است')
    if not settings.DB_PATH.is_file():
        raise ValueError('دیتابیس موجود پژوهش لازم است؛ پایگاه خالی ساخته نمی شود')
    with DatabaseManager(read_only=bool(args.review)) as db:
        if args.review:
            print(json.dumps(review_expectancy(db, args.review), ensure_ascii=False, indent=2, allow_nan=False))
        else:
            result = run_expectancy(db, source_id=args.source, trials_attempted=args.trials_attempted,
                                    replications=args.replications, diagnostic_workers=args.workers)
            print(result['report_path'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
