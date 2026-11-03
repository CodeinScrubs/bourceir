"""نمای اولیه میز، محاسبه سنگین فرضیه‌ها را به کاربر نسبت نمی‌دهد."""

import pandas as pd
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from config import settings
from database.db import DatabaseManager
from ops import jobs


@pytest.fixture(autouse=True)
def isolate_monitor_state(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path/'data')
    monkeypatch.setattr(jobs, 'JOBS_DIR', tmp_path/'jobs')


def test_operator_initial_view_does_not_evaluate_market_hypotheses(tmp_path, monkeypatch):
    from dashboard import operator_status as ui

    path = tmp_path / 'operator.db'
    with DatabaseManager(db_path=path):
        pass
    monkeypatch.setattr(settings, 'DB_PATH', path)
    calls = []

    def evaluate(database_path):
        calls.append(database_path)
        return pd.DataFrame()

    monkeypatch.setattr(ui, 'latest_v4_metrics', evaluate)
    st.cache_data.clear()
    at = AppTest.from_string('from dashboard.operator_status import operator_panel\noperator_panel()').run()
    assert not at.exception and not calls
    assert any(m.value == 'بررسی نشده' for m in at.metric)
    assert any('به معنی نبود نامزد نیست' in t.value for t in at.markdown)
    at.button[0].click().run()
    assert not at.exception and calls == [str(path)]
    assert any(m.label == 'منطبق با فرضیه‌های پژوهشی' and m.value == '0' for m in at.metric)
    st.cache_data.clear()


def test_v4_cache_identity_uses_all_rows_and_effective_lock_setting(monkeypatch):
    from dashboard.operator_status import _v4_input_digest

    prices = pd.DataFrame({'value': range(60001)})
    clients = pd.DataFrame({'value': [None] * 60001})
    original = _v4_input_digest(prices, clients)
    prices.loc[30001, 'value'] += 1
    changed = _v4_input_digest(prices, clients)
    assert changed != original
    monkeypatch.setitem(settings.BACKTEST_CONFIG, 'lock_min_move_pct', .006)
    assert _v4_input_digest(prices, clients) != changed


def test_home_warns_when_a_paper_portfolio_uses_an_older_runtime(tmp_path, monkeypatch):
    from database.paper import PaperRepository

    path = tmp_path/'paper-warning.db'
    with DatabaseManager(db_path=path) as db:
        PaperRepository(db).create(name='دفتر موقت', capital_rial=100_000,
                                   rationale='سناریوی آزمون', mode='manual')
    monkeypatch.setattr(settings, 'DB_PATH', path)
    monkeypatch.setitem(settings.TRADING_HOURS, 'end', '13:30')
    st.cache_data.clear()
    try:
        app = AppTest.from_string('from dashboard.operator_status import operator_panel\noperator_panel()').run()
        assert not app.exception
        assert any('ثبت معامله تازه' in item.value and 'متوقف' in item.value for item in app.warning)
    finally:
        st.cache_data.clear()


def test_broker_capture_age_rejects_future_and_invalid_timestamps():
    from dashboard.operator_status import _broker_capture_current

    now = '2026-11-02T10:00:00+03:30'
    assert _broker_capture_current('2026-11-02T09:45:00+03:30', now)
    assert not _broker_capture_current('2026-11-02T09:44:59+03:30', now)
    assert not _broker_capture_current('2026-11-02T10:00:01+03:30', now)
    assert not _broker_capture_current('2026-11-02T10:00:00', now)


def test_operator_displays_only_five_for_attention_and_keeps_all_matches(tmp_path, monkeypatch):
    from core.screening import expected_completed_session
    from dashboard import operator_status as ui
    from tests.test_operator_status import _matching_metrics

    path = tmp_path / 'queue.db'
    with DatabaseManager(db_path=path):
        pass
    monkeypatch.setattr(settings, 'DB_PATH', path)
    rows = []
    for index in range(7):
        frame = _matching_metrics()
        frame['symbol'], frame['date'] = f'نماد{index}', expected_completed_session()
        frame['value'] = settings.RESEARCH_CONFIG['min_daily_value_rial'] * (index+1)
        rows.append(frame)
    monkeypatch.setattr(ui, 'latest_v4_metrics', lambda _: pd.concat(rows, ignore_index=True))
    st.cache_data.clear()
    try:
        app = AppTest.from_string('from dashboard.operator_status import operator_panel\noperator_panel()').run()
        app.button[0].click().run()
        assert not app.exception
        queues = [item.value for item in app.dataframe if 'نماد برای بررسی' in item.value.columns]
        assert len(queues) == 1 and len(queues[0]) == 5
        assert queues[0]['نماد برای بررسی'].iloc[0] == 'نماد6'
        all_matches = [item.value for item in app.dataframe if 'علت توقف' in item.value.columns]
        assert len(all_matches) == 1 and len(all_matches[0]) == 7
        assert any('پیش‌بینی سود نیست' in item.value for item in app.caption)
    finally:
        st.cache_data.clear()
