# -*- coding: utf-8 -*-
"""
آزمون خودکار داشبورد با Streamlit AppTest: همه صفحه‌ها روی دیتابیس خالی و دیتابیس دارای داده
بدون خطا اجرا می‌شوند. (باگ صفحه خالی در اجرای دوم app.py هم اینجا آزموده می‌شود.)
"""

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from config import settings
from data_collector.scheduler import DataCollector
from data_collector.tsetmc import TsetmcProvider
from database.db import DatabaseManager
from tests.conftest import ROOT_DIR, FakeSession

DASHBOARD = str(ROOT_DIR / "dashboard" / "app.py")
ROOT_APP = str(ROOT_DIR / "app.py")

PAGES = ["🏠 خانه", "📊 بررسی نماد", "📡 دیده‌بان زنده", "📰 اطلاعیه‌های کدال", "🩺 سلامت سامانه", "ℹ️ راهنما"]


@pytest.fixture
def dashboard_db(tmp_path, monkeypatch):
    path = tmp_path / "dash.db"
    monkeypatch.setattr(settings, "DB_PATH", path)
    st.cache_data.clear()       # کش Streamlit بین آزمون‌ها (در یک فرآیند) مشترک است
    st.cache_resource.clear()
    yield path
    st.cache_data.clear()
    st.cache_resource.clear()


def _run_all_pages(script: str) -> AppTest:
    at = AppTest.from_file(script, default_timeout=60).run()
    assert not at.exception, at.exception
    assert any("BRS" in t.value for t in at.title)  # صفحه خانه
    for page in PAGES:
        at.sidebar.radio[0].set_value(page).run()
        assert not at.exception, (page, at.exception)
    return at


def test_dashboard_pages_on_empty_database(dashboard_db):
    at = _run_all_pages(DASHBOARD)
    at.sidebar.radio[0].set_value("🏠 خانه").run()
    assert any("هنوز داده‌ای ثبت نشده" in w.value for w in at.warning)


def test_dashboard_pages_with_data(dashboard_db, market_watch_rows, closing_price_daily, client_type_all, monkeypatch):
    class _NoCodal:
        def get_latest(self, pages=3, symbol_fa=None):
            return []

    db = DatabaseManager(db_path=dashboard_db)
    collector = DataCollector(db=db, codal=_NoCodal(), provider=TsetmcProvider(session=FakeSession({
        "GetMarketWatch": {"marketwatch": market_watch_rows},
        "GetClientTypeAll": {"clientTypeAllDto": list(client_type_all.values())},
        "GetClosingPriceDailyList": {"closingPriceDaily": closing_price_daily},
        "GetClientTypeHistory": {"clientType": []},
    })))
    collector.sync_universe()
    collector.collect_history(symbols=["فولاد"])
    collector.collect_market_snapshot()
    db.close()

    from features import store

    monkeypatch.setattr(store, "FEATURES_ROOT", dashboard_db.parent / "features")
    store.build_feature_store(DatabaseManager(db_path=dashboard_db))
    at = _run_all_pages(DASHBOARD)
    at.sidebar.radio[0].set_value("📊 بررسی نماد").run()
    assert any("قیمت پایانی" in m.label for m in at.metric)
    feature_table = at.dataframe[-1].value
    assert "قدرت خریدار" in set(feature_table["فیچر"])
    at.sidebar.radio[0].set_value("📡 دیده‌بان زنده").run()
    table = at.dataframe[0].value.set_index("نماد")
    assert set(table.index) == {"فولاد", "خزامیا"}  # هر دو سهم داخل دامنه ثبت شده‌اند
    assert table.loc["فولاد", "قدرت خریدار"] == 0.78


def test_root_app_renders_on_every_run(dashboard_db):
    """app.py ریشه باید در هر اجرا داشبورد را بسازد، نه فقط بار اول."""
    for _ in range(2):
        at = AppTest.from_file(ROOT_APP, default_timeout=60).run()
        assert not at.exception
        assert any("BRS" in t.value for t in at.title)
