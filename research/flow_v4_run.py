"""خانواده محدود v4؛ نقد مشترک، همه شکست‌ها، کنترل هم‌تعداد و مرز توسعه."""

import json
import uuid
from dataclasses import asdict, replace

import numpy as np
import pandas as pd

from backtest.portfolio import PORTFOLIO_VERSION, PortfolioConfig, replay_portfolio
from config import settings
from core import market_rules
from core.flow_filters import (
    FLOW_FILTER_V4_VERSION,
    V4_FILTERS,
    filter_conditions_v4,
    filter_diagnostics_v4,
    flow_metrics_v4,
)
from core.timeutils import now_tehran_iso
from database.flow_data import load_flow_inputs
from features.provenance import assert_compatible_inputs
from research.flow_run import _rank_diagnostics, _summary, format_flow_report
from research.labels import market_calendar
from research.portfolio_run import _picks, select_from_past, verified_baseline_inputs
from research.probability_run import _dump
from research.run import _deduplicate_input, _sha256, archive_research_code, research_code_fingerprint
from research.uncertainty import compare_equity_curves
from research.walkforward import time_folds

VERSION = 'flow-portfolio-v4'
HORIZONS = (1, 2)
SEEDS = (41, 97, 173)


def observed_market_curve(prices, days, capital):
    """شاخص ناخالص سبد مشاهده‌شده؛ هر ردیف بازده نامعلوم، مسیر بعدی را نامعلوم می‌کند.

    این شاخص محصول قابل خرید یا کنترل هم‌ریسک نیست. نبود عضویت تاریخی رفع نشده؛
    میانگین فقط از ردیف‌های معامله‌شده همان تاریخ است و پوشش جدا حفظ می‌شود.
    """
    p = prices.loc[(prices.volume > 0) & prices.date.isin(days)].copy()
    p['return'] = market_rules.pit_daily_returns(p)
    grouped = p.groupby('date')['return']
    known, total = grouped.count().reindex(days), grouped.size().reindex(days)
    daily = grouped.mean().reindex(days).where(known.eq(total) & total.gt(0))
    # cumprod معمولی بعد از NaN دوباره عدد می‌سازد؛ نامعلومی تجمعی باید باقی بماند.
    curve = capital * (1 + daily).cumprod(skipna=False)
    return pd.DataFrame(dict(date=list(days), equity_rial=curve.to_numpy(), observed_rows=total.to_numpy(),
                             known_return_rows=known.to_numpy()))


def run_flow_v4(db, *, source_id, initial_capital_rial=None, min_train_sessions=504, test_sessions=252,
                replications=20000):
    if source_id is None:
        raise ValueError('منبع BASELINES صریح برای v4 لازم است')
    if type(replications) is not int or not 1000 <= replications <= 100000:
        raise ValueError('تعداد بازنمونه باید بین هزار و صد هزار باشد')
    cfg = dict(settings.RESEARCH_CONFIG)
    root = settings.DATA_DIR / 'research'
    source_id, frozen, source_hashes = verified_baseline_inputs(root, source_id)
    source_path = root / (source_id + '.json')
    source_sha = _sha256(source_path)
    source = json.loads(source_path.read_text(encoding='utf-8'))
    if not set(HORIZONS) <= set(source['params']['horizons']):
        raise ValueError('هر دو افق ثابت ۱ و ۲ باید در منبع موجود باشند')
    own = not db.conn.in_transaction
    if own:
        db.conn.execute('BEGIN')
    try:
        assert_compatible_inputs(db, source['params']['research_provenance']['feature_manifest'], end_exclusive=cfg['holdout_start'])
        prices, clients = load_flow_inputs(db, before=cfg['holdout_start'])
    finally:
        if own:
            db.conn.rollback()
    calendar = market_calendar(prices)
    folds = time_folds(calendar[calendar >= cfg['dev_start']], min_train_sessions=min_train_sessions,
                       test_sessions=test_sessions, embargo_sessions=max(HORIZONS) + 1, end_exclusive=cfg['holdout_start'])
    capital = initial_capital_rial if initial_capital_rial is not None else settings.BACKTEST_CONFIG['initial_capital']
    config = PortfolioConfig(initial_capital_rial=capital, max_positions=cfg['top_k'], max_loss_pct=.10,
        slippage_bps=settings.BACKTEST_CONFIG['default_slippage_bps'],
        participation=min(.05, settings.BACKTEST_CONFIG['max_volume_participation_pct']))
    candidates = [f'{name}-{h}d' for name in V4_FILTERS for h in HORIZONS]
    random_suffixes = ['-random', '-random97', '-random173']
    modes = ['past_baseline', 'past_baseline_stress'] + [c + suffix for c in candidates for suffix in
        ['', '-stress', *random_suffixes, *(s + '-stress' for s in random_suffixes)]]
    # منبع پایه نیز همه قواعد/افق‌هایش را دیده است؛ تعداد کل سابقه پروژه هنوز نامعلوم است.
    attempted = len(modes) + len(source['params']['strategies']) * len(source['params']['horizons'])
    family_comparisons = max(attempted, len(folds) * len(candidates) * 2 * 2 * 3)
    code = research_code_fingerprint()
    experiment_id = f'EXP-{now_tehran_iso()[:10]}-FLOW-{uuid.uuid4().hex}'
    folder = root / experiment_id
    folder.mkdir(parents=True, exist_ok=False)
    inputs = {}
    for name, frame in dict(prices=prices, clients=clients, baseline_features=frozen['features'], baseline_labels=frozen['labels']).items():
        path = folder / (name + '.parquet')
        frame.to_parquet(path, index=False, compression='zstd')
        inputs[name] = _deduplicate_input(path, root / 'objects')
    protocol = dict(version=VERSION, filter_version=FLOW_FILTER_V4_VERSION, portfolio_version=PORTFOLIO_VERSION,
        source_id=source_id, source_hashes=source_hashes, source_report_sha256=source_sha, inputs=inputs,
        config=asdict(config), horizons=list(HORIZONS), random_seeds=list(SEEDS), modes=modes, candidate_modes=candidates,
        definitions={n: list(filter_conditions_v4(pd.DataFrame(), n).columns) for n in V4_FILTERS},
        folds=[asdict(f) for f in folds], holdout_start=cfg['holdout_start'], prospective_independence=False,
        selection='fixed hypotheses; top-k descending decision-day traded value then symbol; no outcome ranking',
        percentile='prior 20 observed sessions traded VALUE; ties/missing rank bounds; NOT company capitalization',
        pullback='surge at t-(n+1), n in [2,3,4]; all n prior sessions negative PIT return, below prior20 volume and strictly decreasing volume',
        random_control='three fixed seeds; same eligible daily candidate counts; normal AND stress; not risk matched',
        blocked_variants={'dip_effective_base_volume': 'not_evaluable: date-specific historical base_volume absent; current metadata never backfilled'},
        historical_base_volume_verified=False, source_price_defects_removed=False,
        uncertainty=dict(block_lengths=[5, 10, 20], replications=replications, seed=41, confidence=.95,
            trials_attempted=attempted, declared_family_comparisons=family_comparisons, historical_total_trials=None,
            trials_scope='all replay modes plus source baseline rule/horizon combinations; earlier project attempts unknown',
            benchmarks=['idle_cash', 'observed_equal_weight_gross_index'], statistic='paired arithmetic daily return difference, separately per fold',
            scope='Bonferroni for this declared family only; no whole-project selection correction'),
        acceptance='all folds known, positive normal and stress net means and edges to past baseline; no loss-floor breach; economic and independent future evidence also required',
        code_sha256=code, source_files=archive_research_code(folder / 'code'))
    if db.log_experiment(experiment_id=experiment_id + '-PROTOCOL', strategy_name='flow-v4-protocol', symbols_tested='universe',
        parameters_json=json.dumps(protocol, ensure_ascii=False, allow_nan=False), metrics_json='{}',
        start_date=cfg['dev_start'], end_date=cfg['holdout_start'], dataset_version=source_id,
        features_version=FLOW_FILTER_V4_VERSION, model_version=VERSION) is None:
        raise RuntimeError('ثبت پروتکل پیش از نتیجه ناموفق بود')
    _dump(folder / 'protocol.json', protocol)
    print('پروتکل v4 پیش از خواندن نتایج ثبت شد.', flush=True)
    metrics = flow_metrics_v4(prices, clients, calendar=calendar)
    metrics = metrics.loc[(metrics.date >= cfg['dev_start']) & (metrics.value >= cfg['min_daily_value_rial'])].reset_index(drop=True)
    diagnostics = {name: filter_diagnostics_v4(metrics, name) for name in V4_FILTERS}
    picks = {name: _rank_diagnostics(metrics, diagnostics[name], cfg['top_k']) for name in V4_FILTERS}
    for name, frame in dict(metrics=metrics, **{f'diagnostics-{n}': d for n, d in diagnostics.items()}).items():
        frame.to_parquet(folder / (name + '.parquet'), index=False, compression='zstd')
    panel = frozen['features'].merge(frozen['labels'], on=['date', 'symbol'], validate='one_to_one')
    panel = panel.loc[(panel.date >= cfg['dev_start']) & (panel.value >= cfg['min_daily_value_rial'])]
    results, accumulated, uncertainties = [], {}, []
    for index, fold in enumerate(folds):
        chosen, training = select_from_past(panel, fold, HORIZONS, cfg['top_k'])
        days = calendar[(calendar >= fold.test_start) & (calendar < fold.test_end_exclusive)]
        selection_end = days[max(0, len(days) - max(HORIZONS) - 1)]
        test = panel.loc[(panel.date >= fold.test_start) & (panel.date < selection_end)]
        prior = prices.loc[prices.date < fold.test_start].groupby('symbol', sort=False).tail(1)
        replay_prices = pd.concat([prior, prices.loc[prices.date.isin(days)]], ignore_index=True)
        benchmark = _picks(test, chosen['strategy'], cfg['top_k']) if chosen else pd.DataFrame(columns=['date', 'symbol', 'rank'])
        jobs = {'past_baseline': (benchmark, replace(config, horizon=chosen['horizon'] if chosen else 1)),
                'past_baseline_stress': (benchmark, replace(config, horizon=chosen['horizon'] if chosen else 1, stress=True))}
        for name in V4_FILTERS:
            selected = picks[name].loc[(picks[name].date >= fold.test_start) & (picks[name].date < selection_end)]
            counts = selected.groupby('date').size()
            randoms = []
            for seed in SEEDS:
                pool = _picks(test, None, cfg['top_k'], seed + index)
                randoms.append(pool.loc[pool['rank'] < pool.date.map(counts).fillna(0)])
            for h in HORIZONS:
                key = f'{name}-{h}d'
                jobs[key] = (selected, replace(config, horizon=h))
                jobs[key + '-stress'] = (selected, replace(config, horizon=h, stress=True))
                for suffix, random in zip(random_suffixes, randoms):
                    jobs[key + suffix] = (random, replace(config, horizon=h))
                    jobs[key + suffix + '-stress'] = (random, replace(config, horizon=h, stress=True))
        record = dict(fold=asdict(fold), chosen_past_baseline=chosen, training=training, modes={})
        curves = {}
        for mode, (selected, configuration) in jobs.items():
            replay = replay_portfolio(replay_prices, selected, calendar, configuration, start=fold.test_start, end=fold.test_end_exclusive)
            replay.metrics['max_loss_from_initial'] = (None if replay.metrics['total_return'] is None else
                max(0., 1 - float(replay.equity.equity_rial.min()) / capital))
            for label, frame in dict(picks=selected, equity=replay.equity, executions=replay.executions, decisions=replay.decisions).items():
                frame.to_parquet(folder / f'fold-{index:02d}-{mode}-{label}.parquet', index=False, compression='zstd')
            record['modes'][mode] = dict(metrics=replay.metrics, assumptions=replay.assumptions)
            accumulated.setdefault(mode, []).append(replay.metrics)
            curves[mode] = replay.equity.set_index('date').reindex(days).reset_index()[['date', 'equity_rial']]
        controls = dict(idle_cash=pd.DataFrame(dict(date=list(days), equity_rial=float(capital))),
                        observed_equal_weight_gross_index=observed_market_curve(prices, days, capital))
        for name, frame in controls.items():
            frame.to_parquet(folder / f'benchmark-{index:02d}-{name}.parquet', index=False, compression='zstd')
        for mode in [c + suffix for c in candidates for suffix in ('', '-stress')]:
            for control, frame in controls.items():
                uncertainties.append(dict(fold=index, mode=mode, benchmark=control,
                    result=compare_equity_curves(curves[mode], frame, initial_capital_rial=capital,
                        expected_sessions=list(days), replications=replications, trials_attempted=attempted,
                        family_comparisons=family_comparisons)))
        results.append(record)
        print(f'v4: برش {index + 1}/{len(folds)} تمام شد.', flush=True)
    if research_code_fingerprint() != code or _sha256(source_path) != source_sha:
        raise RuntimeError('کد یا گزارش منبع هنگام پژوهش تغییر کرد؛ نتیجه منتشر نشد')
    for name, digest in inputs.items():
        if _sha256(folder / (name + '.parquet')) != digest:
            raise RuntimeError('نسل ورودی حین اجرا تغییر کرد')
    summaries = {name: dict(_summary(values), all_fold_mean=None if any(m['total_return'] is None for m in values) else
        float(np.mean([m['total_return'] for m in values])), corporate_action_events=sum(m['corporate_action_events'] for m in values))
        for name, values in accumulated.items()}
    _dump(folder / 'uncertainty.json', uncertainties)
    payload = dict(experiment_id=experiment_id, created_at=now_tehran_iso(), protocol=protocol, results=results,
        summary=summaries, comparisons={}, accepted_for_signals=False, holdout_eligible=False,
        blocked_variants=protocol['blocked_variants'], uncertainty_sha256=_sha256(folder / 'uncertainty.json'),
        filter_coverage={name: dict(rows=len(d), matches=int(d.match.sum()), incomplete_rows=int((d.known_conditions < d.total_conditions).sum()))
                         for name, d in diagnostics.items()},
        artifacts_sha256={p.name: _sha256(p) for p in sorted(folder.glob('*.parquet'))})
    _dump(root / (experiment_id + '.json'), payload)
    (root / (experiment_id + '.md')).write_text(format_flow_report(payload), encoding='utf-8')
    if db.log_experiment(experiment_id=experiment_id, strategy_name='flow-v4-shared-cash', symbols_tested='universe',
        parameters_json=json.dumps(protocol, ensure_ascii=False, allow_nan=False), metrics_json=json.dumps(summaries, allow_nan=False),
        start_date=cfg['dev_start'], end_date=cfg['holdout_start'], dataset_version=source_id,
        features_version=FLOW_FILTER_V4_VERSION, model_version=VERSION) is None:
        raise RuntimeError('ثبت نتیجه ناموفق بود')
    _dump(root / 'latest-flow-v4.json', dict(experiment_id=experiment_id))
    return dict(payload, report_path=str(root / (experiment_id + '.md')))
