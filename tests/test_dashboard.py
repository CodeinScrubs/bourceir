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
from ops import jobs
from tests.conftest import ROOT_DIR, FakeSession

DASHBOARD = str(ROOT_DIR / "dashboard" / "app.py")
ROOT_APP = str(ROOT_DIR / "app.py")

PAGES = ["🏠 خانه", "🔎 فهرست بررسی", '🧪 آزمایشگاه فیلترها', "🛡️ برنامه و دفتر معاملات", '💼 پورتفوی مجازی', "🏦 حساب و تطبیق فارابی", "📊 بررسی نماد", "📡 دیده‌بان زنده",
         '🗓️ دفتر مشاهده آینده', "📰 اطلاعیه‌های کدال", "🔬 پژوهش", "📚 مطالعه رشد نمادها", "🩺 سلامت سامانه", "ℹ️ راهنما"]


@pytest.fixture
def dashboard_db(tmp_path, monkeypatch):
    path = tmp_path / "dash.db"
    monkeypatch.setattr(settings, "DB_PATH", path)
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path / 'data')
    monkeypatch.setattr(jobs, 'JOBS_DIR', tmp_path / 'jobs')
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


def test_symbol_chart_modes_and_evidence_panel_do_not_infer_economic_events(dashboard_db):
    from tests.test_flow_filters import history
    p, _ = history(45)
    p.loc[25:, [col for col in p if col.endswith('_raw')]] *= .5
    with DatabaseManager(db_path=dashboard_db) as db:
        sid = db.upsert_symbol(dict(symbol_fa='کیان', ins_code='1', instrument_type_code=300))
        db.upsert_daily_prices_df(sid, p.drop(columns='symbol'))
    at = AppTest.from_file(DASHBOARD, default_timeout=60).run()
    at.sidebar.radio[0].set_value('📊 بررسی نماد').run()
    assert not at.exception
    mode = next(r for r in at.radio if r.label == 'نوع قیمت نمودار')
    assert mode.value == 'تعدیل نمایشی بر مبنای مرجع'
    assert any(m.label == 'رویداد شرکتی ثبت‌شده' and str(m.value) == '0' for m in at.metric)
    assert any('افزایش سرمایه یا مجمع تأییدشده نیست' in c.value for c in at.caption)
    assert any('نبود پرچم نیز اثبات امکان خروج نیست' in c.value for c in at.caption)
    mode.set_value('قیمت خام روز').run()
    assert not at.exception
    assert next(r for r in at.radio if r.label == 'نوع قیمت نمودار').value == 'قیمت خام روز'


def test_flow_lab_explains_incomplete_original_filter(dashboard_db, closing_price_daily):
    db = DatabaseManager(db_path=dashboard_db)
    provider = TsetmcProvider(session=FakeSession({'GetClosingPriceDailyList': {'closingPriceDaily': closing_price_daily}}))
    sid = db.upsert_symbol(dict(symbol_fa='فولاد', ins_code='test', instrument_type_code=300))
    db.upsert_daily_prices_df(sid, provider.get_symbol_history('test'))
    db.close()
    at = AppTest.from_file(DASHBOARD, default_timeout=60).run()
    at.sidebar.radio[0].set_value('🧪 آزمایشگاه فیلترها').run()
    at.selectbox[0].set_value('special_original').run()
    assert not at.exception
    assert any('فیلتر ۹ شرطی کامل نیست' in w.value for w in at.warning)
    checks = at.dataframe[1].value
    assert len(checks) == 9 and (checks['وضعیت'] == 'نامعلوم').sum() >= 4


def test_flow_report_has_readable_control_labels_and_action_counts(dashboard_db, tmp_path, monkeypatch):
    import json

    from backtest.portfolio import PORTFOLIO_VERSION
    from core.flow_filters import FLOW_FILTER_VERSION
    from research.flow_run import FLOW_RESEARCH_VERSION
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    root = tmp_path/'research'
    root.mkdir()
    mode = 'retail_acceleration-3d-random'
    (root/'latest-flow.json').write_text(json.dumps(dict(experiment_id='flow-test')), encoding='utf-8')
    payload = dict(protocol=dict(config=dict(initial_capital_rial=1_000_000_000),
        version=FLOW_RESEARCH_VERSION, filter_version=FLOW_FILTER_VERSION,
        portfolio_version=PORTFOLIO_VERSION), summary={mode: {}}, results=[dict(modes={mode: dict(metrics=dict(
        corporate_action_events=3, open_positions=1, stale_mark_days=4))})])
    payload['summary']['past_baseline_stress'] = {}
    payload['results'][0]['modes']['past_baseline_stress'] = dict(metrics=dict(corporate_action_events=0,
        open_positions=0, stale_mark_days=0))
    (root/'flow-test.json').write_text(json.dumps(payload), encoding='utf-8')
    (root/'flow-test.md').write_text(f'| {mode} | +1.25% |', encoding='utf-8')
    at = AppTest.from_file(DASHBOARD, default_timeout=60).run()
    at.sidebar.radio[0].set_value('🔬 پژوهش').run()
    assert not at.exception
    texts = '\n'.join(m.value for m in at.markdown)
    assert 'تقویت سرانه / ۳ جلسه / کنترل تصادفی' in texts
    assert 'گذر از رویداد مصنوعی' in texts and '| 3 | 1 | 4 |' in texts
    assert 'پایه منتخب گذشته / فشار اجرا' in texts
    assert 'تماماً نقد فرضی' in texts and 'سود حساب واقعی نیست' in texts
    assert '\u2066+1.25%\u2069' in texts
    assert mode not in texts


def test_running_portfolio_job_has_a_label_and_does_not_crash(dashboard_db, monkeypatch):
    monkeypatch.setattr(jobs, 'job_status', lambda name: dict(running=name == 'research-portfolio',
        started_at=None, log_tail=[], returncode=None))
    at = AppTest.from_file(DASHBOARD, default_timeout=60).run()
    at.sidebar.radio[0].set_value('🔬 پژوهش').run()
    assert not at.exception
    assert any('پرتفوی' in item.value for item in at.info)


@pytest.mark.parametrize('pointer, params', [('latest-dip.json', dict(version='dip-v2')),
    ('latest.json', dict(raw_feature_inputs_verified=True, features=dict(version='daily-v4'), labels=dict(version='labels-v3')))])
def test_legacy_research_reports_are_marked_as_archived(dashboard_db, tmp_path, monkeypatch, pointer, params):
    import json
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    folder = tmp_path / 'research'
    folder.mkdir()
    (folder / pointer).write_text(json.dumps(dict(experiment_id='old-dip')), encoding='utf-8')
    (folder / 'old-dip.json').write_text(json.dumps(dict(params=params)), encoding='utf-8')
    (folder / 'old-dip.md').write_text('Historical research report', encoding='utf-8')
    at = AppTest.from_file(DASHBOARD, default_timeout=60).run()
    at.sidebar.radio[0].set_value('🔬 پژوهش').run()
    assert not at.exception
    assert any('گزارش قدیمی' in item.value for item in at.markdown)


def test_portfolio_hypothesis_selection_shows_its_own_latest_report(dashboard_db, tmp_path, monkeypatch):
    import json

    from backtest.portfolio import PORTFOLIO_VERSION
    from research.dip import TIME_EXIT_PRICE_BASIS
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    folder = tmp_path/'research'
    folder.mkdir()
    for selector, text in [('buy_queue', 'Synthetic queue hypothesis result'), ('buyer_power', 'Synthetic buyer-power hypothesis result')]:
        (folder/f'latest-portfolio-{selector}.json').write_text(json.dumps(dict(experiment_id=selector)), encoding='utf-8')
        payload = dict(protocol=dict(portfolio_version=PORTFOLIO_VERSION,
            dip=dict(selector=selector, time_exit_price_basis=TIME_EXIT_PRICE_BASIS)))
        (folder/f'{selector}.json').write_text(json.dumps(payload), encoding='utf-8')
        (folder/f'{selector}.md').write_text(text, encoding='utf-8')
    (folder/'latest-portfolio.json').write_text(json.dumps(dict(experiment_id='buyer_power')), encoding='utf-8')
    at = AppTest.from_file(DASHBOARD, default_timeout=60).run()
    at.sidebar.radio[0].set_value('🔬 پژوهش').run()
    assert not at.exception
    assert any('Synthetic queue hypothesis result' in item.value for item in at.markdown)
    assert not any('Synthetic buyer-power hypothesis result' in item.value for item in at.markdown)
    next(w for w in at.selectbox if w.label == 'فرضیه انتخاب نامزدهای خرید در اصلاح').set_value('قدرت خریدار').run()
    assert not at.exception
    assert any('Synthetic buyer-power hypothesis result' in item.value for item in at.markdown)
    assert not any('Synthetic queue hypothesis result' in item.value for item in at.markdown)
    # A mislabeled pointer must not display another hypothesis under the selected name.
    (folder/'latest-portfolio-buy_queue.json').write_text(json.dumps(dict(experiment_id='buyer_power')), encoding='utf-8')
    next(w for w in at.selectbox if w.label == 'فرضیه انتخاب نامزدهای خرید در اصلاح').set_value('صف خرید قفل در روز تصمیم').run()
    assert not at.exception
    assert any('با فرضیه انتخابی سازگار نیست' in item.value for item in at.markdown)
    assert not any('Synthetic buyer-power hypothesis result' in item.value for item in at.markdown)


def test_research_page_while_job_is_running(dashboard_db, monkeypatch):
    from ops import jobs
    monkeypatch.setattr(jobs, 'job_status', lambda name: dict(running=name == 'research', started_at=None,
                                                            log_tail=[], args=[]))
    at = AppTest.from_file(DASHBOARD, default_timeout=60).run()
    at.sidebar.radio[0].set_value('🔬 پژوهش').run()
    assert not at.exception
    assert any('پژوهش' in i.value for i in at.info)


def test_live_filter_without_matches_does_not_format_nat(dashboard_db):
    with DatabaseManager(db_path=dashboard_db) as db:
        sid = db.upsert_symbol({'symbol_fa':'فولاد', 'ins_code':'1', 'instrument_type_code':300})
        db.insert_snapshot(sid, {'captured_at':'2026-10-12T10:00:00+03:30', 'last_price':100,
                                'close_price':100, 'ref_price':100, 'volume':1000, 'value':100000,
                                'buy_queue_volume':0, 'sell_queue_volume':0})
    at = AppTest.from_file(DASHBOARD, default_timeout=60).run()
    at.sidebar.radio[0].set_value('📡 دیده‌بان زنده').run()
    at.radio[0].set_value('فقط صف خرید').run()
    assert not at.exception
    assert any('هیچ نمادی' in i.value for i in at.info)
