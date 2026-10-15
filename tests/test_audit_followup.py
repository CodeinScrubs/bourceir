"""Reproductions from the October audit. All market/account values are test data."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from backtest.engine import BacktestEngine
from core.timeutils import TEHRAN_TZ, now_tehran
from data_collector.scheduler import DataCollector
from data_collector.tsetmc import TsetmcProvider
from features import store
from research import dip_run
from research.dip import DipRule
from tests.conftest import FakeSession
from tests.test_research_integrity import dip_runner as dip_runner
from tests.test_workbench import quote


def live_book(**changes):
    at = datetime(2026, 10, 14, 10, 0, tzinfo=TEHRAN_TZ)
    snapshot = dict(provider_timestamp=at.isoformat(), captured_at=at.isoformat(),
                    price_limit_up=1030, price_limit_down=970, last_price=1000,
                    bid_price_1=999, bid_vol_1=100, bid_count_1=3,
                    ask_price_1=1000, ask_vol_1=7, ask_count_1=2,
                    buy_queue_volume=0, sell_queue_volume=0, value=1000000, volume=1000)
    snapshot.update(changes)
    return at, snapshot


def test_intraday_buy_queue_blocks_sizing_despite_unlocked_daily_candle():
    at, snapshot = live_book(bid_price_1=1030, bid_vol_1=500000, buy_queue_volume=500000,
                             ask_price_1=0, ask_vol_1=0, ask_count_1=0)
    result = quote(intraday=True, intraday_snapshot=snapshot, at=at, stop_rial=980, target_rial=1020)
    assert result['quantity'] == 0 and result['blockers']


def test_intraday_uses_current_depth_instead_of_yesterdays_lock():
    at, snapshot = live_book()
    result = quote(intraday=True, intraday_snapshot=snapshot, at=at, locked_up=True, stop_rial=980, target_rial=1020)
    assert result['quantity'] == 7 and result['caps']['displayed_ask'] == 7


def test_intraday_participation_counts_the_scenario_execution_price():
    at, snapshot = live_book(ask_vol_1=1000)
    result = quote(intraday=True, intraday_snapshot=snapshot, at=at, daily_value_rial=1e9,
                   stop_rial=980, target_rial=1020, slippage_bps=100)
    assert result['quantity'] == 49
    assert result['entry_cost_rial'] / (1 + result['fees']['buy']) <= snapshot['value'] * .05


def test_intraday_exit_levels_outside_todays_band_are_blocked():
    at, snapshot = live_book()
    result = quote(intraday=True, intraday_snapshot=snapshot, at=at)
    assert result['quantity'] == 0 and any('دامنه' in text for text in result['blockers'])


@pytest.mark.parametrize('changes', [dict(ask_vol_1=None), dict(price_limit_up=None),
                                    dict(captured_at='2026-10-14T09:00:00+03:30'),
                                    dict(provider_timestamp='2026-10-14T09:00:00+03:30'),
                                    dict(ask_price_1=1001), dict(sell_queue_volume=50000)])
def test_incomplete_or_unavailable_live_execution_blocks_plan(changes):
    at, snapshot = live_book(**changes)
    assert quote(intraday=True, intraday_snapshot=snapshot, at=at, stop_rial=980, target_rial=1020)['quantity'] == 0


def test_recent_client_types_revisions_and_holes_are_refetched(db):
    sid = db.upsert_symbol(dict(symbol_fa='آزمون', ins_code='1', instrument_type_code=300))
    days = [(now_tehran().date() - timedelta(days=i)).isoformat() for i in range(9, -1, -1)]
    for day in days:
        db.insert_daily_price(sid, day, 100, 101, 99, 100, volume=10, value=1000,
                              close_final_raw=100, ref_price_raw=100)
    db.upsert_client_types_df(sid, pd.DataFrame(dict(date=days[:7] + days[8:], buy_i_value=1)))
    requested = DataCollector(db=db)._missing_client_type_dates(sid)
    assert days[7] in requested and days[-1] in requested and days[0] not in requested


@pytest.mark.parametrize('drop_reference_column', [False, True])
def test_legacy_engine_rejects_unknown_reference_during_holding(drop_reference_column):
    days = ['2024-01-01', '2024-01-02', '2024-01-03', '2024-01-04']
    frame = pd.DataFrame(dict(date=days, low_raw=99., high_raw=101., close_raw=100.,
                              close_final_raw=100., ref_price_raw=[100., 100., None, 100.],
                              volume=10000, value=1000000.))
    engine = BacktestEngine(initial_capital=10000, default_slippage_bps=0)
    if drop_reference_column:
        frame = frame.drop(columns='ref_price_raw')
    with pytest.raises(ValueError, match='مرجع'):
        engine.run_replay_simulation(frame, 'آزمون', lambda row, _: 'BUY' if row['date'] == days[0] else None)


def test_dip_zero_holding_is_explicitly_rejected():
    with pytest.raises(ValueError, match='مثبت'):
        DipRule(hold=0)


def test_dip_research_rejects_features_from_an_older_raw_snapshot(db, dip_runner):
    sid = db.get_symbol('آزمونالف')['id']
    db.conn.execute('UPDATE daily_prices SET value=value*10 WHERE symbol_id=?', (sid,))
    db.conn.commit()
    with pytest.raises(ValueError, match='خام|منشأ'):
        dip_run.run_dip_research(db, rules=[DipRule()])
    assert not (dip_runner / 'latest-dip.json').exists()


def test_feature_publication_keeps_the_previous_generation_readable(db, dip_runner):
    previous = store.load_manifest()
    assert previous.get('data_file'), 'Feature publication needs an immutable generation pointer'
    old_path = store.store_dir() / previous['data_file']
    original = old_path.read_bytes()
    store.build_feature_store(db)
    assert old_path.read_bytes() == original
    assert store.load_manifest()['data_file'] != previous['data_file']


def test_client_type_revisions_invalidate_stale_features(db, dip_runner):
    sid = db.get_symbol('آزمونالف')['id']
    db.upsert_client_types_df(sid, pd.DataFrame(dict(date=['2024-01-02'], buy_i_value=60000)))
    with pytest.raises(ValueError, match='خام'):
        dip_run.run_dip_research(db, rules=[DipRule()])


def test_baseline_runner_also_rejects_mixed_raw_and_feature_versions(db, dip_runner, monkeypatch):
    from research import run
    monkeypatch.setattr(run, 'RESEARCH_ROOT', dip_runner)
    monkeypatch.setattr(run, 'LABELS_ROOT', dip_runner.parent / 'labels')
    db.conn.execute('UPDATE daily_prices SET value=value*10')
    db.conn.commit()
    with pytest.raises(ValueError, match='خام'):
        run.run_research(db, random_trials=1)
    assert not (dip_runner / 'latest.json').exists()


def test_holdout_raw_changes_do_not_enter_a_development_compatibility_check(db, dip_runner):
    from features.provenance import assert_compatible_inputs
    sid = db.get_symbol('آزمونالف')['id']
    db.insert_daily_price(sid, '2026-10-14', 100, 101, 99, 100, volume=10, value=1000,
                          close_final_raw=100, ref_price_raw=100)
    assert_compatible_inputs(db, store.load_manifest(), end_exclusive='2025-03-21')
    with pytest.raises(ValueError, match='خام'):
        assert_compatible_inputs(db, store.load_manifest())


def test_identical_refetch_does_not_invalidate_inputs_due_to_ingestion_timestamps(db, dip_runner):
    from features.provenance import assert_compatible_inputs
    sid = db.get_symbol('آزمونالف')['id']
    db.upsert_daily_prices_df(sid, db.get_daily_prices_df('آزمونالف'))
    assert_compatible_inputs(db, store.load_manifest())


def test_partial_client_type_response_preserves_rows_and_reports_incomplete(db):
    from core.timeutils import now_tehran
    sid = db.upsert_symbol(dict(symbol_fa='آزمون', ins_code='1', instrument_type_code=300))
    days = [(now_tehran().date() - timedelta(days=i)).isoformat() for i in (2, 1)]
    for day in days:
        db.insert_daily_price(sid, day, 100, 101, 99, 100, volume=10, value=1000,
                              close_final_raw=100, ref_price_raw=100)
    db.upsert_client_types_df(sid, pd.DataFrame(dict(date=days, buy_i_value=1)))
    provider = SimpleNamespace(get_client_types_history=lambda *a, **kw: pd.DataFrame(dict(date=[days[-1]], buy_i_value=2)))
    result = DataCollector(db=db, provider=provider).collect_client_types(['آزمون'])
    assert result['status'] == 'partial' and result['rows_inserted'] == 1
    assert db.get_client_types_df('آزمون').buy_i_value.tolist() == [1, 2]


def test_mixed_universe_response_resolves_missing_types_instead_of_dropping_them():
    from tests.test_hardening import _info, _watch_row
    rows = [_watch_row('آزمونالف', '1', yVal=300), _watch_row('آزمونب', '2')]
    provider = TsetmcProvider(session=FakeSession({'GetMarketWatch': {'marketwatch': rows},
        'GetInstrumentInfo/2': _info('آزمونب', '2', 303, flow=2)}))
    assert {row['symbol_fa'] for row in provider.get_stock_universe()} == {'آزمونالف', 'آزمونب'}
    incomplete = TsetmcProvider(session=FakeSession({'GetMarketWatch': {'marketwatch': rows}}))
    assert incomplete.get_stock_universe() == []


def test_simultaneous_dashboard_starts_launch_only_one_owned_process(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from ops import jobs
    script = tmp_path / 'wait_for_stop.py'
    script.write_text('import time\ntime.sleep(30)\n', encoding='utf-8')
    monkeypatch.setattr(jobs, 'BRS_SCRIPT', script)
    monkeypatch.setattr(jobs, 'JOBS_DIR', tmp_path / 'jobs')
    monkeypatch.setattr(jobs.settings, 'LOG_DIR', tmp_path / 'logs')
    monkeypatch.setattr(jobs, '_OWNED_PROCESSES', {})
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: jobs.start_job('concurrent-test', []), range(2)))
        assert sorted(outcomes) == [False, True]
        assert jobs.job_status('concurrent-test')['running']
    finally:
        jobs.stop_job('concurrent-test')


def test_feature_reader_can_finish_its_generation_while_a_new_one_is_published(db, dip_runner, monkeypatch):
    old = store.load_manifest()
    original = pd.read_parquet
    published = []
    def interleaved(*args, **kwargs):
        if not published:
            published.append(store.build_feature_store(db))
        return original(*args, **kwargs)
    monkeypatch.setattr(pd, 'read_parquet', interleaved)
    frame, received = store.load_feature_snapshot()
    assert not frame.empty and received == old
    assert store.load_manifest()['data_file'] != old['data_file']


@pytest.mark.parametrize('status, expected', [('success', 0), ('partial', 1), ('failed', 1)])
def test_cli_collection_failures_propagate_to_exit_codes(monkeypatch, status, expected):
    import brs
    from data_collector import scheduler
    report = dict(status=status, symbols_succeeded=1 if status != 'failed' else 0,
                  symbols_failed=0 if status == 'success' else 1, errors=[])
    collector = SimpleNamespace(db=SimpleNamespace(close=lambda: None, get_universe_symbols=lambda: ['x']),
        sync_universe=lambda: report, collect_announcements=lambda **kw: report,
        collect_market_snapshot=lambda **kw: report)
    monkeypatch.setattr(scheduler, 'DataCollector', lambda **kw: collector)
    assert brs.cmd_universe(SimpleNamespace()) == expected
    assert brs.cmd_codal(SimpleNamespace(pages=1, symbol=None, judge=False)) == expected
    assert brs.cmd_live(SimpleNamespace(symbols='', force=True, interval=1, cycles=1)) == expected
