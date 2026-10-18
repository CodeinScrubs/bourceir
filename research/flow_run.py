"""Fixed public-filter hypotheses, shared-cash replay, and development-only comparisons."""

import json
import os
import uuid
from dataclasses import asdict, replace

import numpy as np
import pandas as pd

from backtest.portfolio import PORTFOLIO_VERSION, PortfolioConfig, replay_portfolio
from config import settings
from core.flow_filters import (
    FILTER_NAMES,
    FLOW_FILTER_VERSION,
    RESEARCH_FILTERS,
    filter_conditions,
    filter_diagnostics,
    flow_metrics,
)
from core.timeutils import now_tehran_iso
from database.flow_data import load_flow_inputs
from features.provenance import assert_compatible_inputs
from research.labels import market_calendar
from research.portfolio_run import _picks, select_from_past, verified_baseline_inputs
from research.run import _deduplicate_input, _json_safe, _sha256, archive_research_code, research_code_fingerprint
from research.walkforward import time_folds

FLOW_RESEARCH_VERSION = 'flow-portfolio-v3'


def ranked_matches(metrics, name, k):
    return _rank_diagnostics(metrics, filter_diagnostics(metrics, name), k)


def _rank_diagnostics(metrics, diagnostics, k):
    """Rank an already computed mask; no second filter name can disagree with it."""
    if not diagnostics[['symbol', 'date']].equals(metrics[['symbol', 'date']]):
        raise ValueError('تشخیص فیلتر باید دقیقاً با نماد، تاریخ و ترتیب ورودی یکسان باشد')
    picked = metrics.loc[diagnostics['match'], ['date', 'symbol', 'value']].copy()
    picked = picked.sort_values(['date', 'value', 'symbol'], ascending=[True, False, True])
    picked['rank'] = picked.groupby('date').cumcount()
    return picked.loc[picked['rank'] < k, ['date', 'symbol', 'rank']]


def _summary(metrics):
    valid = [m for m in metrics if m['total_return'] is not None]
    returns = [m['total_return'] for m in valid]
    return dict(folds=len(metrics), valid_folds=len(valid), positive_folds=sum(r > 0 for r in returns),
        mean_fold_return=float(np.mean(returns)) if returns else None,
        median_fold_return=float(np.median(returns)) if returns else None,
        worst_fold_drawdown=max((m['max_drawdown'] for m in valid), default=None),
        worst_initial_loss=max((m['max_loss_from_initial'] for m in valid), default=None),
        folds_with_open_positions=sum(m['open_positions'] > 0 for m in metrics),
        mean_of_valid_folds_only=True)


def run_flow_research(db, *, source_id=None, initial_capital_rial=None, min_train_sessions=504,
                      test_sessions=252, horizons=(2, 3), random_seed=41, family='v3', replications=20000):
    if family == 'v4':
        from research.flow_v4_run import run_flow_v4
        return run_flow_v4(db, source_id=source_id, initial_capital_rial=initial_capital_rial,
                          min_train_sessions=min_train_sessions, test_sessions=test_sessions, replications=replications)
    if family != 'v3':
        raise ValueError('خانواده فیلتر باید v3 یا v4 باشد')
    cfg = dict(settings.RESEARCH_CONFIG)
    horizons = tuple(horizons)
    if not horizons or any(isinstance(h, bool) or h not in cfg['horizons'] or not isinstance(h, int) for h in horizons):
        raise ValueError('افق معتبر پژوهش پایه لازم است')
    root = settings.DATA_DIR / 'research'
    source_id, frozen, source_hashes = verified_baseline_inputs(root, source_id)
    source = json.loads((root/f'{source_id}.json').read_text(encoding='utf-8'))
    manifest = source['params']['research_provenance']['feature_manifest']
    own_snapshot = not db.conn.in_transaction
    if own_snapshot:
        db.conn.execute('BEGIN')
    try:
        # The new sidecar and old baseline must refer to the same raw prices AND client types.
        assert_compatible_inputs(db, manifest, end_exclusive=cfg['holdout_start'])
        prices, clients = load_flow_inputs(db, before=cfg['holdout_start'])
    finally:
        if own_snapshot:
            db.conn.rollback()
    calendar = market_calendar(prices)
    folds = time_folds(calendar[calendar >= cfg['dev_start']], min_train_sessions=min_train_sessions,
        test_sessions=test_sessions, embargo_sessions=max(horizons)+1, end_exclusive=cfg['holdout_start'])
    capital = initial_capital_rial if initial_capital_rial is not None else settings.BACKTEST_CONFIG['initial_capital']
    configuration = PortfolioConfig(initial_capital_rial=capital, max_positions=cfg['top_k'],
        slippage_bps=settings.BACKTEST_CONFIG['default_slippage_bps'],
        participation=min(.05, settings.BACKTEST_CONFIG['max_volume_participation_pct']), max_loss_pct=.10)
    code_sha = research_code_fingerprint()
    experiment_id = f'EXP-{now_tehran_iso()[:10]}-FLOW-{uuid.uuid4().hex}'
    folder = root/experiment_id
    folder.mkdir(parents=True)
    input_hashes = {}
    for name, frame in dict(prices=prices, clients=clients, baseline_features=frozen['features'],
                             baseline_labels=frozen['labels']).items():
        path = folder/f'{name}.parquet'
        frame.to_parquet(path, index=False, compression='zstd')
        input_hashes[name] = _deduplicate_input(path, root/'objects')
    protocol = dict(version=FLOW_RESEARCH_VERSION, filter_version=FLOW_FILTER_VERSION,
        portfolio_version=PORTFOLIO_VERSION, source_id=source_id, source_hashes=source_hashes, inputs=input_hashes,
        config=asdict(configuration), horizons=list(horizons), random_seed=random_seed,
        folds=[asdict(f) for f in folds], filters=list(RESEARCH_FILTERS),
        definitions={name: list(filter_conditions(pd.DataFrame(), name).columns) for name in FILTER_NAMES},
        ranking='fixed descending decision-day value; symbol tie-break; top-k; no outcome tuning',
        windows='3/5 per-capita arithmetic daily means include today; 20/30 volume/value windows exclude today; full windows',
        volume_comparability='all n reference transitions between today and the n previous volumes must be known and equal to 1; otherwise ratio unknown; no share-count inference',
        unavailable='original nine-condition filter, free-float history, market-mean definition, historical actual price limits, intraday book',
        benchmark='best of the existing eight baselines chosen using only purged, embargoed past labels; otherwise cash',
        random_control='one seed; same daily candidate count; liquid universe; descriptive, not a significance test',
        holdout_start=cfg['holdout_start'], prospective_independence=False,
        universe='current known ordinary-equity registry, including inactive; historical membership incomplete',
        code_sha256=code_sha, source_files=archive_research_code(folder/'code'))
    registered = db.log_experiment(experiment_id=experiment_id+'-PROTOCOL', strategy_name='flow-filter-protocol',
        symbols_tested='universe', parameters_json=json.dumps(_json_safe(protocol), ensure_ascii=False, allow_nan=False),
        metrics_json='{}', start_date=cfg['dev_start'], end_date=cfg['holdout_start'], dataset_version=source_id,
        features_version=FLOW_FILTER_VERSION, model_version=FLOW_RESEARCH_VERSION)
    if registered is None:
        raise RuntimeError('ثبت پروتکل پیش از ارزیابی ناموفق بود')
    (folder/'protocol.json').write_text(json.dumps(_json_safe(protocol), ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    print('پروتکل ثابت پیش از محاسبه نتیجه ثبت شد.', flush=True)
    metrics = flow_metrics(prices, clients)
    metrics = metrics.loc[(metrics.date >= cfg['dev_start']) & (metrics.date < cfg['holdout_start'])
                           & (metrics.value >= cfg['min_daily_value_rial'])].reset_index(drop=True)
    panel = frozen['features'].merge(frozen['labels'], on=['date', 'symbol'], validate='one_to_one')
    panel = panel.loc[(panel.date >= cfg['dev_start']) & (panel.value >= cfg['min_daily_value_rial'])]
    diagnostics = {name: filter_diagnostics(metrics, name) for name in FILTER_NAMES}
    for name, frame in diagnostics.items():
        frame.to_parquet(folder/f'diagnostics-{name}.parquet', index=False, compression='zstd')
    metrics.to_parquet(folder/'metrics.parquet', index=False, compression='zstd')
    all_picks = {name: _rank_diagnostics(metrics, diagnostics[name], cfg['top_k']) for name in RESEARCH_FILTERS}
    results, accumulated = [], {}
    for index, fold in enumerate(folds):
        chosen, training = select_from_past(panel, fold, horizons, cfg['top_k'])
        prior = prices.loc[prices.date < fold.test_start].groupby('symbol', sort=False).tail(1)
        test_prices = prices.loc[(prices.date >= fold.test_start) & (prices.date < fold.test_end_exclusive)]
        replay_prices = pd.concat([prior, test_prices], ignore_index=True)
        test_calendar = calendar[(calendar >= fold.test_start) & (calendar < fold.test_end_exclusive)]
        selection_end = test_calendar[max(0, len(test_calendar)-max(horizons)-1)]
        test_panel = panel.loc[(panel.date >= fold.test_start) & (panel.date < selection_end)]
        random_pool = _picks(test_panel, None, cfg['top_k'], random_seed+index)
        benchmark = _picks(test_panel, chosen['strategy'], cfg['top_k']) if chosen else pd.DataFrame(columns=['date', 'symbol', 'rank'])
        jobs = {'past_baseline': (benchmark, replace(configuration, horizon=chosen['horizon'] if chosen else horizons[0])),
                'past_baseline_stress': (benchmark, replace(configuration, horizon=chosen['horizon'] if chosen else horizons[0], stress=True))}
        for name, picked in all_picks.items():
            selected = picked.loc[(picked.date >= fold.test_start) & (picked.date < selection_end)]
            counts = selected.groupby('date').size()
            random = random_pool.loc[random_pool['rank'] < random_pool.date.map(counts).fillna(0)]
            for horizon in horizons:
                key = f'{name}-{horizon}d'
                jobs[key] = (selected, replace(configuration, horizon=horizon))
                jobs[key+'-stress'] = (selected, replace(configuration, horizon=horizon, stress=True))
                jobs[key+'-random'] = (random, replace(configuration, horizon=horizon))
        record = dict(fold=asdict(fold), chosen_past_baseline=chosen, training=training, modes={})
        for mode, (picks, config) in jobs.items():
            replay = replay_portfolio(replay_prices, picks, calendar, config, start=fold.test_start, end=fold.test_end_exclusive)
            replay.metrics['max_loss_from_initial'] = (None if replay.metrics['total_return'] is None else
                max(0.0, 1-float(replay.equity['equity_rial'].min())/config.initial_capital_rial))
            prefix = f'fold-{index:02d}-{mode}'
            for label, frame in dict(picks=picks, equity=replay.equity, executions=replay.executions, decisions=replay.decisions).items():
                frame.to_parquet(folder/f'{prefix}-{label}.parquet', index=False, compression='zstd')
            record['modes'][mode] = dict(metrics=replay.metrics, assumptions=replay.assumptions)
            accumulated.setdefault(mode, []).append(replay.metrics)
        results.append(record)
        print(f'برش {index+1}/{len(folds)} تمام شد.', flush=True)
    if research_code_fingerprint() != code_sha:
        raise RuntimeError('کد پژوهش هنگام اجرا تغییر کرد؛ نتیجه منتشر نشد')
    for name, digest in input_hashes.items():
        path = folder/f'{name}.parquet'
        if _sha256(path) != digest:
            raise RuntimeError('ورودی ثابت پژوهش هنگام اجرا تغییر کرد؛ نتیجه منتشر نشد')
    comparisons = {}
    for name in RESEARCH_FILTERS:
        for horizon in horizons:
            mode = f'{name}-{horizon}d'
            compared = [mode, mode+'-stress', mode+'-random', 'past_baseline', 'past_baseline_stress']
            known = [i for i, r in enumerate(results) if all(r['modes'][key]['metrics']['total_return'] is not None for key in compared)]
            values = {key: [results[i]['modes'][key]['metrics']['total_return'] for i in known] for key in compared}
            comparisons[mode] = dict(common_fold_indices=known, all_folds_known=len(known) == len(folds),
                common_mean_returns={key: float(np.mean(v)) if v else None for key, v in values.items()},
                normal_mean_edge_to_past_baseline=float(np.mean(np.array(values[mode])-values['past_baseline'])) if known else None,
                stress_mean_edge_to_past_baseline=float(np.mean(np.array(values[mode+'-stress'])-values['past_baseline_stress'])) if known else None)
    payload = dict(experiment_id=experiment_id, created_at=now_tehran_iso(), protocol=protocol, results=results,
        comparisons=comparisons,
        summary={mode: _summary(values) for mode, values in accumulated.items()}, accepted_for_signals=False,
        filter_coverage={name: dict(rows=len(d), matches=int(d.match.sum()), incomplete_rows=int((d.known_conditions < d.total_conditions).sum()))
                         for name, d in diagnostics.items()},
        artifacts_sha256={p.name: _sha256(p) for p in sorted(folder.glob('*.parquet'))})
    report_path = root/f'{experiment_id}.md'
    report_path.write_text(format_flow_report(payload), encoding='utf-8')
    (root/f'{experiment_id}.json').write_text(json.dumps(_json_safe(payload), ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    if db.log_experiment(experiment_id=experiment_id, strategy_name='flow-filters-shared-cash', symbols_tested='universe',
        parameters_json=json.dumps(_json_safe(protocol), ensure_ascii=False, allow_nan=False),
        metrics_json=json.dumps(_json_safe(payload['summary']), ensure_ascii=False, allow_nan=False),
        start_date=cfg['dev_start'], end_date=cfg['holdout_start'], dataset_version=source_id,
        features_version=FLOW_FILTER_VERSION, model_version=FLOW_RESEARCH_VERSION) is None:
        raise RuntimeError('ثبت نتیجه ناموفق بود؛ اشاره‌گر آخرین نتیجه تغییر نکرد')
    temporary = root/f'latest-flow-{uuid.uuid4().hex}.tmp'
    temporary.write_text(json.dumps(dict(experiment_id=experiment_id)), encoding='utf-8')
    os.replace(temporary, root/'latest-flow.json')
    return dict(payload, report_path=str(report_path))


def format_flow_report(payload):
    lines = ['# آزمایش فیلترهای جریان حقیقی با سرمایه مشترک', '', f"شناسه: {payload['experiment_id']}", '',
        'فقط دوره توسعه قبلاً دیده‌شده؛ حساب هر برش مستقل است. این اعداد بازده سالانه یا سود حساب واقعی نیستند.',
        'افق از جلسه ورود حساب می‌شود؛ تصمیم پایان روز، خرید جلسه بعد. هیچ فیلتر پذیرفته‌شده یا احتمال سود اعلام نمی‌شود.', '',
        '| حالت | برش معلوم | برش مثبت | میانگین برش معلوم | میانه | بیشترین افت از اوج | بیشترین زیان از سرمایه اولیه |',
        '| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    def pct(value):
        return 'نامعلوم' if value is None else f'{value:+.2%}'
    for mode, summary in payload['summary'].items():
        lines.append(f"| {mode} | {summary['valid_folds']}/{summary['folds']} | {summary['positive_folds']} | "
            f"{pct(summary['mean_fold_return'])} | {pct(summary['median_fold_return'])} | "
            f"{pct(summary['worst_fold_drawdown'])} | {pct(summary['worst_initial_loss'])} |")
    if payload['protocol']['version'] == 'flow-portfolio-v4':
        lines += ['', 'دو فرضیه چندعاملی مستقل از v3؛ سه بذر ثابت با شمار نامزد برابر، در اجرای عادی و فشار.',
                  'حجم مبنای تاریخی در ورودی موجود نیست؛ نوع DIP وابسته به آن قابل ارزیابی اعلام نشده است.',
                  'همه نتایج، کران نامعلوم و عدم قطعیت بلوکی محفوظ‌اند؛ شاخص هم‌وزنِ ناخالص، محصول قابل خرید یا کنترل هم‌ریسک نیست.',
                  'تعداد آزمون در خانواده حاضر ثبت شده؛ کل جست‌وجوهای قبلی پروژه معلوم نیست و اصلاح کامل انتخاب ادعا نمی‌شود.',
                  'هیچ مجوز سیگنال یا بازکردن دوره نهایی ایجاد نشده است.']
        return '\n'.join(lines) + '\n'
    lines += ['', 'مقایسه مستقیم فقط روی برش‌های مشترک معلوم معتبر است؛ میانگین برش‌های معلوم با مخرج متفاوت رتبه مالی نمی‌سازد.',
        'قواعد، پنجره‌ها، افق‌ها و مرتب‌سازی پیش از نتیجه ثابت‌اند؛ برتری بهترین حالتِ دیده‌شده، آزمون مستقل نیست.',
        'فیلتر ۹ شرطی اصلی به علت شناوری تاریخی، سقف مجاز روز و تعریف میانگین بازار تکمیل نشده؛ جایگزینی پنهان انجام نشده است.',
        'شکست سقف روی تمام ۶۰ جلسه قبلی و شاخص علّی محاسبه شده؛ این نسخه با کد هشت روز منتخب در پیوست یکسان نیست.',
        'صف، هزینه، نقد مشترک، حجم حداکثر ۵٪ و فشار خرید سقف/فروش کف در موتور لحاظ شده‌اند؛ OHLC اجرای واقعی را ثابت نمی‌کند.',
        'اقدام شرکتی مصنوعی است؛ حقوق واقعی، سود نقدی، زمان وصول و عضویت تاریخی کامل جامعه تأیید نشده‌اند.',
        'حد زیان ۱۰٪ فقط ورود تازه را متوقف می‌کند؛ تضمین سقف زیان یا نقدشوندگی نیست. کنترل تصادفی یک بذر دارد و آزمون معنی‌داری نیست.']
    lines += ['', '| فرضیه | برش مشترک معلوم | اختلاف میانگین با پایه منتخب گذشته | اختلاف در فشار اجرا |',
              '| --- | ---: | ---: | ---: |']
    for mode, comparison in payload['comparisons'].items():
        lines.append(f"| {mode} | {len(comparison['common_fold_indices'])} | "
                     f"{pct(comparison['normal_mean_edge_to_past_baseline'])} | {pct(comparison['stress_mean_edge_to_past_baseline'])} |")
    return '\n'.join(lines)+'\n'
