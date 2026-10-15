"""Whole-account risk and provenance regressions; synthetic data stays in temporary databases."""

import json
from datetime import datetime

import pandas as pd
import pytest

from core import market_rules
from core.journal import summarize_executions
from core.risk import RiskProfile
from core.timeutils import TEHRAN_TZ
from database.db import DatabaseManager
from database.workbench import WorkbenchRepository
from tests.test_dashboard import dashboard_db as dashboard_db
from tests.test_research_integrity import dip_runner as dip_runner


@pytest.mark.parametrize('held_quote', ['fresh', 'loss_floor', 'missing', 'stale'])
def test_intraday_ui_sizes_against_all_existing_positions(dashboard_db, monkeypatch, held_quote):
    from streamlit.testing.v1 import AppTest

    from tests.test_dashboard import DASHBOARD

    clock = datetime(2026, 10, 14, 10, 30, tzinfo=TEHRAN_TZ)
    for module in ('core.screening', 'core.risk', 'dashboard.workbench'):
        monkeypatch.setattr(f'{module}.now_tehran', lambda: clock, raising=False)
    profile = RiskProfile(1_000_000, 20_000)
    with DatabaseManager(db_path=dashboard_db) as db:
        repo = WorkbenchRepository(db)
        repo.save_profile(profile)
        for index, symbol in enumerate(['الف', 'ب']):
            sid = db.upsert_symbol(dict(symbol_fa=symbol, ins_code=str(index), instrument_type_code=300))
            db.insert_daily_price(sid, '2026-10-13', 1000, 1010, 990, 1000,
                                  close_final_raw=1000, ref_price_raw=1000, volume=100000, value=100000000)
            last = 970 if symbol == 'ب' and held_quote == 'loss_floor' else 1000
            if symbol == 'ب' and held_quote == 'missing':
                continue
            quote_time = '2026-10-14T10:28:00+03:30' if symbol == 'ب' and held_quote == 'stale' else clock.isoformat()
            db.insert_snapshot(sid, dict(captured_at=quote_time, provider_timestamp=quote_time,
                last_price=last, close_price=last, ref_price=1000, high_price=1000, low_price=970,
                price_limit_up=1030, price_limit_down=970, volume=100000, value=100000000,
                buy_queue_volume=0, sell_queue_volume=0, bid_price_1=last, bid_vol_1=1000,
                bid_count_1=2, ask_price_1=last, ask_vol_1=1000, ask_count_1=2))
        key = repo.save_plan(symbol='ب', mode='paper', horizon='swing', entry_rial=1000,
            stop_rial=999, target_rial=1030, rationale='Synthetic prior-session plan',
            calculation=dict(quantity=800, blockers=[]))
        repo.record_execution(mode='paper', symbol='ب', side='buy', quantity=800, price_rial=1000,
            executed_at='2026-10-13T10:00:00+03:30', rationale='Synthetic fill', plan_id=key)
        archived = summarize_executions(repo.executions('paper'), profile, {'ب': 1000})
        fallen = summarize_executions(repo.executions('paper'), profile, {'ب': 970})
        assert not archived['loss_limit_reached'] and fallen['loss_limit_reached']
    app = AppTest.from_file(DASHBOARD, default_timeout=60).run()
    app.sidebar.radio[0].set_value('🛡️ برنامه و دفتر معاملات').run()
    next(w for w in app.selectbox if w.label == 'نماد برنامه').set_value('الف')
    for label, value in {'قیمت ورود (تومان برای هر سهم)': 100.,
                         'قیمت سناریوی خروج با زیان (تومان)': 98.,
                         'قیمت هدف خودت (تومان)': 102.,
                         'بودجه زیان همین معامله (تومان)': 100}.items():
        next(w for w in app.number_input if w.label == label).set_value(value)
    app.run()
    assert not app.exception
    sizes = [w.value for w in app.markdown if 'حداکثر تعداد در این سناریو' in w.value]
    assert bool(sizes) == (held_quote == 'fresh'), [w.value for w in app.warning] + sizes
    assert any(w.label == 'ثبت برنامه دستی' for w in app.button) == (held_quote == 'fresh')
    if held_quote == 'fresh':
        next(w for w in app.text_area if w.label == 'دلیل انتخاب سهم و شرط نامعتبر شدن برنامه').set_value('Synthetic new plan')
        app.run()
        next(w for w in app.button if w.label == 'ثبت برنامه دستی').click().run()
        assert not app.exception
        with DatabaseManager(db_path=dashboard_db, read_only=True) as db:
            saved = next(p for p in WorkbenchRepository(db).plans('paper') if p['symbol_fa'] == 'الف')
            calculation = json.loads(saved['calculation_json'])
        evidence = calculation['account_risk_evidence']
        assert evidence['evaluated_at'] == clock.isoformat()
        assert set(evidence['observations']) == {'ب'}
        assert evidence['equity_rial'] == pytest.approx(989990.4)
        assert evidence['observations']['ب']['snapshot']['last_price'] == 1000
        assert calculation['calculation_version'] == 'manual-plan-v4'


def test_feature_research_rejects_a_different_recorded_lock(db, tmp_path):
    from features.provenance import assert_compatible_inputs
    from features.store import build_feature_store

    sid = db.upsert_symbol(dict(symbol_fa='آزمون', ins_code='1', instrument_type_code=300))
    dates = pd.date_range('2024-01-01', periods=50).strftime('%Y-%m-%d')
    db.upsert_daily_prices_df(sid, pd.DataFrame(dict(date=dates, open_raw=1000, high_raw=1010,
        low_raw=990, close_raw=1000, close_final_raw=1000, ref_price_raw=1000,
        volume=1000, value=1000000)))
    manifest = build_feature_store(db, root=tmp_path / 'features')
    manifest['provenance']['requirements_lock_sha256'] = '0' * 64
    with pytest.raises(ValueError, match='وابستگی|محیط'):
        assert_compatible_inputs(db, manifest, end_exclusive='2025-03-21')


def test_missing_official_close_remains_unknown_even_when_last_trade_exists():
    frame = pd.DataFrame(dict(close_raw=[110., 120.], close_final_raw=[100., None], ref_price_raw=100.))
    assert market_rules.official_close(frame).iloc[0] == 100.
    assert pd.isna(market_rules.official_close(frame).iloc[1])
    assert pd.isna(market_rules.pit_daily_returns(frame).iloc[1])


@pytest.mark.parametrize('value', [None, 0, -1, float('inf'), 'invalid'])
def test_bad_official_close_is_never_replaced_by_last_trade(value):
    frame = pd.DataFrame(dict(close_raw=[100.], close_final_raw=[value], ref_price_raw=[100.]))
    assert market_rules.official_close(frame).isna().all()
    assert market_rules.official_close(frame.drop(columns='close_final_raw')).isna().all()


def test_research_rejects_a_different_recorded_computation_environment(db, dip_runner):
    from features.provenance import assert_compatible_inputs
    from features.store import load_manifest
    manifest = load_manifest()
    manifest['provenance']['feature_environment']['packages']['numpy'] = 'different'
    with pytest.raises(ValueError, match='وابستگی|محیط'):
        assert_compatible_inputs(db, manifest)


def test_installed_numeric_version_must_match_the_lock(db, tmp_path, monkeypatch):
    from features import provenance, store
    actual = provenance.version
    monkeypatch.setattr(provenance, 'version', lambda name: 'different' if name == 'numpy' else actual(name))
    with pytest.raises(ValueError, match='numpy'):
        store.build_feature_store(db, root=tmp_path / 'features')
    assert not (tmp_path / 'features').exists()


def test_unrelated_installed_tools_do_not_change_the_feature_environment(monkeypatch):
    from features import provenance
    original = provenance.version
    expected = provenance.feature_environment()
    def installed(name):
        assert name in provenance.COMPUTATION_PACKAGES
        return original(name)
    monkeypatch.setattr(provenance, 'version', installed)
    assert provenance.feature_environment() == expected


def test_research_fingerprint_detects_environment_changes_without_a_source_edit(monkeypatch):
    from research import run
    before = run.research_code_fingerprint()
    environment = run.feature_environment()
    environment['requirements_lock_sha256'] = 'changed'
    monkeypatch.setattr(run, 'feature_environment', lambda: environment)
    assert run.research_code_fingerprint() != before


def test_environment_change_during_build_cannot_replace_the_manifest(db, dip_runner, monkeypatch):
    from features import store
    previous = store.load_manifest()
    real = store.feature_environment
    calls = []
    def environment():
        result = real()
        if calls:
            result['requirements_lock_sha256'] = 'changed'
        calls.append(True)
        return result
    monkeypatch.setattr(store, 'feature_environment', environment)
    with pytest.raises(RuntimeError, match='وابستگی'):
        store.build_feature_store(db)
    assert store.load_manifest() == previous


def test_empty_symbol_selection_does_not_implicitly_build_the_entire_universe(db, tmp_path):
    from features import store
    sid = db.upsert_symbol(dict(symbol_fa='الف', ins_code='1', instrument_type_code=300))
    db.insert_daily_price(sid, '2024-01-01', 100, 101, 99, 100, volume=10, value=1000,
                          close_final_raw=100, ref_price_raw=100)
    manifest = store.build_feature_store(db, symbols=[], root=tmp_path)
    assert manifest['rows'] == 0 and manifest['symbols'] == 0
    assert all(not partitions for partitions in manifest['provenance']['raw_inputs']['tables'].values())


def test_missing_day_does_not_compress_the_calendar_for_statistical_inference():
    from research.baselines import tolerant_t
    values = pd.Series([.01, -.02, .03, .01, .02] * 12)
    assert pd.notna(tolerant_t(values, 2))
    values.iloc[20] = float('nan')
    assert pd.isna(tolerant_t(values, 2))


def test_all_open_positions_need_fresh_quotes_and_saved_evidence():
    from core.account_risk import intraday_account_state, reserved_position_risk
    from tests.test_audit_followup import live_book
    clock, book = live_book()
    events = [dict(symbol_fa=s, quantity=10, price_rial=1000, fee_rial=0, side='buy', plan_id=s)
              for s in ('الف', 'ب')]
    plans = [dict(id=s, stop_rial=980) for s in ('الف', 'ب')]
    profile = RiskProfile(100000, 10000)
    result = intraday_account_state(events, profile, plans, {'الف': book, 'ب': book}, at=clock)
    assert result['current'] and result['state']['equity_rial'] == pytest.approx(80000 + 20 * 999 * .9912)
    expected = 20 * .9912 * (999 - 980 * .999)
    assert result['reserved_risk_rial'] == pytest.approx(expected)
    assert reserved_position_risk(result['state'], plans) == pytest.approx(expected)
    result = intraday_account_state(events, profile, plans, {'الف': book}, at=clock)
    assert not result['current'] and result['state']['equity_rial'] is None
    assert result['reserved_risk_rial'] is None and result['state']['unknown_marks'] == ['ب']
    assert result['evidence']['observations']['ب']['snapshot'] is None


@pytest.mark.parametrize('changes', [dict(bid_price_1=None), dict(bid_vol_1=0), dict(sell_queue_volume=1000),
                                    dict(price_limit_up=None), dict(bid_price_1=1031),
                                    dict(ask_price_1=998), dict(provider_timestamp='2026-10-13T10:00:00+03:30')])
def test_unavailable_exit_quotes_for_a_holding_block_new_account_risk(changes):
    from core.account_risk import intraday_account_state
    from tests.test_audit_followup import live_book
    clock, book = live_book(**changes)
    result = intraday_account_state([dict(symbol_fa='الف', quantity=10, price_rial=1000, fee_rial=0, side='buy')],
                                    RiskProfile(100000, 10000), [], {'الف': book}, at=clock)
    assert not result['current'] and result['evidence']['blockers']
