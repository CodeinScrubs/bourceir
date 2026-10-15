"""بازسازی خطاهای بازبینی؛ همه داده‌ها فقط در آزمون و دیتابیس موقت‌اند."""

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from config import settings
from core import market_rules
from core.timeutils import TEHRAN_TZ
from data_collector.monitor import DataFreshnessMonitor
from data_collector.validator import DataValidator
from research.baselines import evaluate_picks, pick_top
from research.labels import compute_labels


def _prices(n=12):
    dates = pd.date_range('2025-05-20', periods=n).strftime('%Y-%m-%d').tolist()
    rows = [dict(symbol='x', date=d, low_raw=99.0, high_raw=101.0, close_raw=100.0,
                 close_final_raw=100.0, ref_price_raw=100.0, volume=1000.0, value=100000.0) for d in dates]
    return pd.DataFrame(rows), dates


@pytest.mark.parametrize('bad', [0.0, -1.0, float('inf'), float('nan'), 200000.0])
def test_invalid_turnover_is_not_repaired_into_executable_label(bad):
    prices, dates = _prices()
    prices.loc[1, 'value'] = bad
    labels = compute_labels(prices, horizons=[1], calendar=dates).set_index('date')
    assert pd.isna(labels.loc[dates[0], 'net_ret_1d'])


def test_empty_inferred_calendar_has_a_clear_error():
    prices, _ = _prices()
    with pytest.raises(ValueError, match='تقویم'):
        compute_labels(prices, horizons=[1])


def test_empty_price_panel_is_supported():
    prices, _ = _prices()
    assert compute_labels(prices.iloc[:0], horizons=[1]).empty


def test_cohort_calendar_keeps_days_without_a_signal():
    dates = pd.date_range('2025-01-01', periods=40).strftime('%Y-%m-%d').tolist()
    panel = pd.DataFrame(dict(date=dates, symbol='x', value=1e10, score=np.nan, net_ret_1d=0.1))
    panel.loc[::4, 'score'] = 1.0
    picks = pick_top(panel, panel['score'], 1)
    result = evaluate_picks(picks, pd.Series(0.0, index=dates), 1, 1)
    assert result['days'] == 40
    assert result['mean_cohort'] == pytest.approx(0.025)
    assert pd.isna(result['cagr'])  # بدون شبیه‌سازی نقد، رشد سالانه پرتفوی واقعی اعلام نمی‌شود


def test_missing_exit_is_unknown_instead_of_idle_cash():
    picks = pd.DataFrame({'date':['2025-01-01', '2025-01-02'], 'symbol':['x', 'x'], 'value':[1e10, 1e10],
                          'net_ret_1d':[0.1, np.nan], 'label_status_1d':['complete', 'censored']})
    result = evaluate_picks(picks, pd.Series(0.0, index=picks['date']), 1, 1)
    assert result['unknown_picks'] == 1
    assert result['mean_cohort'] == pytest.approx(0.1)
    assert result['fill_rate'] == pytest.approx(1.0)  # مخرج فقط ورودهایی که وضعیتشان معلوم است


def test_future_provider_time_is_never_healthy():
    now = datetime(2026, 10, 12, 10, 0, tzinfo=TEHRAN_TZ)
    snap = {'provider_timestamp':(now + timedelta(hours=1)).isoformat(), 'last_price':100.0}
    result = DataFreshnessMonitor().inspect_snapshot('x', snap, now=now)
    assert result['status'] == '⚪ Unknown' and not result['is_healthy']


def test_market_reference_never_comes_from_a_different_symbol():
    df = pd.DataFrame({'symbol':['x', 'y'], 'close_final_raw':[100.0, 500.0], 'close_raw':[100.0, 500.0],
                       'ref_price_raw':[100.0, np.nan]})
    assert pd.isna(market_rules.reference_price(df).iloc[1])


@pytest.mark.parametrize('column,bad', [('high_raw',float('inf')), ('volume',1.5), ('volume',float('inf')),
                                      ('value',-1.0), ('date','2026-02-30')])
def test_bad_market_rows_do_not_pass_validation(column, bad):
    prices, _ = _prices()
    prices.loc[0, column] = bad
    clean, rejected = DataValidator.validate_daily_dataframe(prices.iloc[:1])
    assert clean.empty and rejected == 1


def test_backup_names_do_not_overwrite_within_one_second(db):
    a = db.backup(keep=5)
    b = db.backup(keep=5)
    assert a != b and a.exists() and b.exists()


def test_update_feature_publication_policy(monkeypatch):
    """
    نقص هر بخش اصلی مانع انتشار فیچر تازه است. شکست کدال به این فیچرها ربطی ندارد.
    """
    import brs
    from data_collector import scheduler
    cases = [({'history': {'status': 'partial', 'symbols_failed': 1}}, False, 1),
             ({'history': {'status': 'failed', 'symbols_failed': 594}}, False, 1),
             ({'history': {'status': 'success'}, 'codal': {'status': 'failed'}}, True, 1),
             ({'history': {'status': 'success'}}, True, 0)]
    for reports, should_build, code in cases:
        called = []
        collector = SimpleNamespace(db=SimpleNamespace(close=lambda: None), update_all=lambda r=reports, **kw: r)
        monkeypatch.setattr(scheduler, 'DataCollector', lambda c=collector, **kw: c)
        monkeypatch.setattr(brs, '_build_features', lambda db, c=called: c.append(True))
        assert brs.cmd_update(SimpleNamespace(symbols='', full=False, no_features=False)) == code
        assert bool(called) is should_build, reports


def _research_fixture(db, tmp_path, monkeypatch):
    from features import store
    from research import labels as labels_mod
    from research import run as run_mod
    from tests.test_features import synthetic_history
    for i, symbol in enumerate(['فولاد', 'خودرو', 'شپنا']):
        prices, ct = synthetic_history(seed=i)
        sid = db.upsert_symbol({'symbol_fa':symbol, 'ins_code':str(i), 'instrument_type_code':300})
        db.upsert_daily_prices_df(sid, prices)
        db.upsert_client_types_df(sid, ct)
    monkeypatch.setattr(store, 'FEATURES_ROOT', tmp_path / 'features')
    store.build_feature_store(db)
    monkeypatch.setattr(run_mod, 'LABELS_ROOT', tmp_path / 'labels')
    monkeypatch.setattr(run_mod, 'RESEARCH_ROOT', tmp_path / 'research')
    monkeypatch.setattr(labels_mod, 'CALENDAR_MIN_SYMBOLS', 2)
    for key, value in dict(min_daily_value_rial=0, dev_start='2025-01-01', holdout_start='2025-06-01',
                           horizons=[1,3], top_k=2).items():
        monkeypatch.setitem(settings.RESEARCH_CONFIG, key, value)
    return run_mod


def test_sealed_run_cannot_read_holdout_prices(db, tmp_path, monkeypatch):
    run_mod = _research_fixture(db, tmp_path, monkeypatch)
    original = run_mod.compute_labels
    def checked(prices, *args, **kwargs):
        assert prices['date'].max() < settings.RESEARCH_CONFIG['holdout_start']
        return original(prices, *args, **kwargs)
    monkeypatch.setattr(run_mod, 'compute_labels', checked)
    run_mod.run_research(db, random_trials=2)


def test_repeated_research_has_distinct_pinned_experiments(db, tmp_path, monkeypatch):
    run_mod = _research_fixture(db, tmp_path, monkeypatch)
    first = run_mod.run_research(db, random_trials=2)
    report = Path(first['report_path']).read_bytes()
    second = run_mod.run_research(db, random_trials=3)
    assert first['experiment_id'] != second['experiment_id']
    assert len(db.get_experiments()) == 2
    assert Path(first['report_path']).read_bytes() == report
    folder = tmp_path / 'research' / first['experiment_id']
    assert (folder / 'features.parquet').exists() and (folder / 'labels.parquet').exists()
    assert len(list((tmp_path / 'research' / 'objects').glob('*.parquet'))) == 3


def test_holdout_unsealing_is_recorded_and_cannot_be_repeated(db, tmp_path, monkeypatch):
    run_mod = _research_fixture(db, tmp_path, monkeypatch)
    run_mod.run_research(db, unseal_holdout=True, random_trials=2, holdout_strategy="momentum_5d", holdout_horizon=1)
    with pytest.raises(ValueError, match='مهر'):
        run_mod.run_research(db, unseal_holdout=True, random_trials=2, holdout_strategy="momentum_5d", holdout_horizon=1)

@pytest.mark.parametrize('price,quantity,volume', [(np.nan, 10, 1000), (np.inf, 10, 1000),
                                                  (100, 1.5, 1000), (100, True, 1000), (100, 10, 0)])
def test_invalid_orders_do_not_mutate_cash(price, quantity, volume):
    from backtest.engine import BacktestEngine
    engine = BacktestEngine(initial_capital=100000)
    assert engine.execute_buy('x', price, quantity, '2025-01-01', daily_volume=volume) is None
    assert engine.cash == 100000 and not engine.trades


def test_participation_cap_is_cumulative_for_the_symbol_session():
    from backtest.engine import BacktestEngine
    engine = BacktestEngine(initial_capital=100000)
    assert engine.execute_buy('x', 100, 50, '2025-01-01', daily_volume=1000).quantity == 50
    assert engine.execute_buy('x', 100, 50, '2025-01-01', daily_volume=1000) is None


def test_participation_also_caps_actual_turnover():
    from backtest.engine import BacktestEngine
    engine = BacktestEngine(initial_capital=100000)
    trade = engine.execute_buy('x', 100, 100, '2025-01-01', daily_volume=10000, daily_value=50000)
    assert trade.gross_value <= 2500 and trade.quantity < 100


def test_failed_next_session_buy_does_not_turn_into_later_entry():
    from backtest.engine import BacktestEngine
    prices, _ = _prices(3)
    prices.loc[1, ['volume', 'value']] = 0.0
    engine = BacktestEngine(initial_capital=100000)
    engine.run_replay_simulation(prices, 'x', lambda row, eng: 'BUY' if row['date'] == prices.iloc[0]['date'] else None)
    assert not engine.trades


def test_feature_output_checksum_is_enforced(db, tmp_path, monkeypatch):
    from features import store
    from tests.test_features import synthetic_history
    prices, _ = synthetic_history()
    sid = db.upsert_symbol({'symbol_fa':'فولاد','ins_code':'1','instrument_type_code':300})
    db.upsert_daily_prices_df(sid, prices)
    store.build_feature_store(db, root=tmp_path / 'features')
    file = store.feature_data_path(store.load_manifest(root=tmp_path / 'features'), root=tmp_path / 'features')
    file.write_bytes(file.read_bytes() + b'tamper')
    with pytest.raises(ValueError, match='اثر انگشت'):
        store.load_feature_store(root=tmp_path / 'features')


def test_cached_fund_is_still_outside_collection_scope(db):
    from data_collector.scheduler import DataCollector
    from data_collector.tsetmc import TsetmcProvider
    from tests.conftest import FakeSession
    db.upsert_symbol({'symbol_fa':'اهرم','ins_code':'33','instrument_type_code':305})
    collector = DataCollector(db=db, provider=TsetmcProvider(session=FakeSession({})))
    assert collector._resolve('اهرم') is None


def test_raw_first_day_zero_reference_does_not_block_the_whole_symbol():
    prices, _ = _prices()
    prices.loc[0, 'ref_price_raw'] = 0.0
    clean, rejected = DataValidator.validate_daily_dataframe(prices)
    assert rejected == 0 and len(clean) == len(prices)


def test_development_outcomes_stay_purged_when_holdout_is_explicitly_open():
    from research.run import research_panel
    prices, dates = _prices(30)
    labels = compute_labels(prices, horizons=[3], calendar=dates)
    features = prices[['symbol','date']]
    cutoff = dates[10]
    panel = research_panel(labels, features, dates[0], cutoff, 0)
    assert panel.loc[panel['planned_exit_date_3d'] >= cutoff, 'net_ret_3d'].isna().all()
    assert panel.loc[panel['planned_exit_date_3d'] >= cutoff, 'label_status_3d'].eq('censored').all()


def test_artifact_json_is_standard_and_unknown_metrics_are_null(db, tmp_path, monkeypatch):
    import json
    run_mod = _research_fixture(db, tmp_path, monkeypatch)
    out = run_mod.run_research(db, random_trials=2)
    file = Path(out['report_path']).with_suffix('.json')
    def reject_constant(value):
        raise AssertionError(value)
    saved = json.loads(file.read_text(encoding='utf-8'), parse_constant=reject_constant)
    stats = next(iter(saved['results']['development']['horizons']['1']['strategies'].values()))
    assert stats['cagr'] is None and stats['portfolio_simulated'] is False


def test_session_does_not_assign_stale_instrument_to_current_market_date(market_watch_rows):
    from data_collector.tsetmc import TsetmcProvider
    from tests.conftest import FakeSession
    rows = [dict(market_watch_rows[0]), dict(market_watch_rows[1], dEven=20261010)]
    provider = TsetmcProvider(session=FakeSession({'GetMarketWatch':{'marketwatch':rows},
        'GetClosingPriceInfo':{'closingPriceInfo':{'finalLastDate':20261011}}}))
    out = provider.get_session_candles()
    assert {r['ins_code'] for r in out['candles']} == {rows[0]['insCode']}


def test_missing_depth_does_not_claim_a_zero_queue(market_watch_rows):
    from data_collector.tsetmc import TsetmcProvider
    row = dict(market_watch_rows[0], blDs=[])
    snap = TsetmcProvider.snapshot_from_market_watch(row, None, '2026-10-11T10:00:00+03:30')
    assert snap['buy_queue_volume'] is None and snap['sell_queue_volume'] is None


def test_missing_reference_does_not_become_a_flat_price_path():
    from features.daily import compute_daily_features
    prices, dates = _prices(60)
    prices.loc[40, 'ref_price_raw'] = np.nan
    features = compute_daily_features(prices).set_index('date')
    assert pd.isna(features.loc[dates[40], 'ret_3d'])
    assert pd.isna(features.loc[dates[40], 'dist_high_20d'])
    assert pd.isna(features.loc[dates[41], 'rsi_14d'])


@pytest.mark.parametrize('config', [{'research':{'top_k':1.5}}, {'research':{'random_trials':0}},
                                  {'research':{'horizons':[1,1]}}, {'research':{'holdout_start':'2025-02-30'}},
                                  {'governance':{'prevent_lookahead_bias':False}}])
def test_invalid_research_and_disabled_guards_are_rejected(config):
    with pytest.raises(ValueError):
        settings.validate_config(config)


def test_market_end_is_exclusive_even_in_utc_environment():
    from core.timeutils import is_market_open
    assert is_market_open(datetime(2026,10,12,9,0))
    assert not is_market_open(datetime(2026,10,12,12,30))


def test_live_honors_recorded_holiday_and_finishes_requested_cycle(monkeypatch):
    import brs
    from core import timeutils
    from data_collector import scheduler
    calls = []
    database = SimpleNamespace(get_universe_symbols=lambda: [{'id':1}], close=lambda: None,
                               is_trading_day=lambda day: False)
    collector = SimpleNamespace(db=database, collect_market_snapshot=lambda **kw: calls.append(True))
    monkeypatch.setattr(scheduler, 'DataCollector', lambda **kw: collector)
    monkeypatch.setattr(timeutils, 'is_market_open', lambda at=None: True)
    monkeypatch.setattr(brs.time, 'sleep', lambda seconds: pytest.fail('requested cycle must terminate'))
    assert brs.cmd_live(SimpleNamespace(symbols='', interval=30, cycles=1, force=False)) == 0
    assert not calls


def test_holdout_requires_a_candidate_chosen_before_results(db, tmp_path, monkeypatch):
    run_mod = _research_fixture(db, tmp_path, monkeypatch)
    with pytest.raises(ValueError, match='از قبل'):
        run_mod.run_research(db, unseal_holdout=True, random_trials=2)
    assert not (tmp_path / 'research' / 'holdout-unsealed.json').exists()


def test_holdout_only_evaluates_the_declared_candidate(db, tmp_path, monkeypatch):
    run_mod = _research_fixture(db, tmp_path, monkeypatch)
    out = run_mod.run_research(db, unseal_holdout=True, random_trials=2,
                               holdout_strategy='buyer_power', holdout_horizon=3)
    results = out['results']['holdout']['horizons']
    assert set(results) == {'3'} and set(results['3']['strategies']) == {'buyer_power'}


def test_real_archive_audit_reports_bad_rows_without_changing_them(db):
    from ops.data_audit import audit_database
    prices, _ = _prices(2)
    sid = db.upsert_symbol({'symbol_fa':'x','ins_code':'1','instrument_type_code':300})
    db.upsert_daily_prices_df(sid, prices)
    db.conn.execute('UPDATE daily_prices SET value=volume*200 WHERE symbol_id=?', (sid,))
    db.conn.commit()
    before = db.conn.total_changes
    report = audit_database(db)
    assert report['daily_prices']['vwap_outside_range'] == 2
    assert report['overall'] == 'warn' and db.conn.total_changes == before


def test_archive_audit_does_not_create_a_missing_database(tmp_path, monkeypatch):
    from ops.data_audit import run_audit
    path = tmp_path / 'absent.db'
    monkeypatch.setattr(settings, 'DB_PATH', path)
    report = run_audit(tmp_path / 'audit.json')
    assert report['overall'] == 'error' and not path.exists()
