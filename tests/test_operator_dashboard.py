"""نمای اولیه میز، محاسبه سنگین فرضیه‌ها را به کاربر نسبت نمی‌دهد."""

import pandas as pd
import streamlit as st
from streamlit.testing.v1 import AppTest

from config import settings
from database.db import DatabaseManager


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
