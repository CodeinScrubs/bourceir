"""اجرای انتهابه‌انتها روی DB موقت؛ خانواده ثابت، هم‌تعدادی و بازبین مستقل منحنی."""

import json

import pandas as pd
import pytest

from config import settings
from ops.research_review import review_research
from research.flow_v4_run import observed_market_curve, run_flow_v4

pytest_plugins = ['tests.test_flow_research']


def test_v4_pipeline_registers_before_results_keeps_all_controls_and_passes_review(db, flow_baseline, monkeypatch):
    from features.store import build_feature_store
    from research.run import run_research

    _, root = flow_baseline
    monkeypatch.setitem(settings.RESEARCH_CONFIG, 'horizons', [1, 2])
    # سه اندازه متفاوت در جامعه آزمون؛ اولین نمادِ رتبه کوچک بازگشت آخر روز دارد.
    for scale, symbol in zip((1, 3, 5), ('الف', 'ب', 'ج')):
        sid = db.get_symbol(symbol)['id']
        prices, clients = db.get_daily_prices_df(symbol), db.get_client_types_df(symbol)
        prices[['value', 'volume']] *= scale
        for col in [c for c in clients if c.endswith(('_volume', '_value'))]:
            clients[col] *= scale
        if symbol == 'الف':
            for index in (112, 145):
                prices.loc[index, 'close_final_raw'] = 1000.
                prices.loc[index + 1, 'ref_price_raw'] = 1000.
        db.upsert_daily_prices_df(sid, prices)
        db.upsert_client_types_df(sid, clients)
    build_feature_store(db)
    source = run_research(db, random_trials=1)
    events = []
    log = db.log_experiment

    def registered(**kwargs):
        events.append(kwargs['experiment_id'])
        return log(**kwargs)

    monkeypatch.setattr(db, 'log_experiment', registered)
    result = run_flow_v4(db, source_id=source['experiment_id'], min_train_sessions=104, test_sessions=30, replications=1000)
    assert events == [result['experiment_id'] + '-PROTOCOL', result['experiment_id']]
    assert result['protocol']['horizons'] == [1, 2] and result['protocol']['random_seeds'] == [41, 97, 173]
    assert not result['holdout_eligible'] and not result['accepted_for_signals']
    assert result['blocked_variants']['dip_effective_base_volume'].startswith('not_evaluable:')
    folder = root / 'research' / result['experiment_id']
    count = 0
    for path in folder.glob('fold-*-intraday_reversal_accumulation-1d-picks.parquet'):
        picked = pd.read_parquet(path)
        count += len(picked)
        for suffix in ('-stress', '-random', '-random97', '-random173', '-random-stress', '-random97-stress', '-random173-stress'):
            control = pd.read_parquet(path.with_name(path.name.replace('-1d-picks', '-1d' + suffix + '-picks')))
            pd.testing.assert_series_equal(picked.groupby('date').size(), control.groupby('date').size())
    assert count > 0
    report = review_research(db, result['experiment_id'])
    assert report['verified']['curve_metrics_recomputed'] and report['uncertainty_sha256_verified']
    assert len(report['candidates']) == 4
    assert not (root / 'research' / 'holdout-unsealed.json').exists()
    declared = json.loads((folder / 'protocol.json').read_text(encoding='utf-8'))
    assert declared == result['protocol']
    # تغییر فایل جانبی نیز بازبین را متوقف می‌کند.
    with (folder / 'uncertainty.json').open('a') as handle:
        handle.write(' ')
    with pytest.raises(ValueError, match='عدم قطعیت'):
        review_research(db, result['experiment_id'])


def test_equal_weight_index_does_not_drop_unknown_observed_market_returns():
    prices = pd.DataFrame(dict(symbol=['الف', 'ب', 'الف', 'ب'], date=['2024-01-01'] * 2 + ['2024-01-02'] * 2,
        close_final_raw=[110., None, 120., 120.], ref_price_raw=[100.] * 4, volume=[1] * 4))
    curve = observed_market_curve(prices, ['2024-01-01', '2024-01-02'], 100.)
    assert curve.equity_rial.isna().all()
    assert curve.known_return_rows.tolist() == [1, 2]


def test_v4_failed_registration_never_computes_filters_or_publishes(db, flow_baseline, monkeypatch):
    # عدم وجود افق ۱ در fixture قدیمی پیش از ثبت هم متوقف می‌شود.
    source, root = flow_baseline
    with pytest.raises(ValueError, match='هر دو افق'):
        run_flow_v4(db, source_id=source['experiment_id'])
    assert not (root / 'research' / 'latest-flow-v4.json').exists()
