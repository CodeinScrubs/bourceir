"""A stopped recorder and an old successful scan must remain visibly distinct."""

from datetime import timedelta

import pytest
from streamlit.testing.v1 import AppTest

from core.opportunity_radar import REASONS, stamp
from core.timeutils import to_jalali
from dashboard import opportunity_radar as panel

NOW = stamp('2026-10-25T13:45:00+03:30')


@pytest.mark.parametrize('at,fresh',[
    ('2026-10-25T13:45:00+03:30',True),
    ('2026-10-25T13:42:00+03:30',True),
    ('2026-10-25T13:41:59+03:30',False),
    ('2026-10-24T07:27:35+03:30',False),
    ('2026-10-25T13:45:01+03:30',False),
    ('2026-10-25T13:45:00',False),
    ('invalid',False),
    (None,False),
])
def test_runtime_does_not_upgrade_stopped_worker_or_invalid_heartbeat(at,fresh):
    for running in (True,False):
        health=panel.runtime_health(dict(running=running),dict(heartbeat_at=at),checked_at=NOW)
        assert health['running'] is running
        assert health['heartbeat_fresh'] is fresh


def test_old_collecting_heartbeat_does_not_display_active_collection(monkeypatch):
    previous=(NOW-timedelta(days=1)).isoformat()
    report=dict(scope=[],rows=[],price_session_date='2026-10-24',price_observed_at=previous,
        news_coverage=dict(window_start=previous,checkpoint_through=previous,window_scan_satisfied=True),
        source_coverage=dict(source_scope_count=0,known_missing_from_watch=[]))
    monkeypatch.setattr(panel,'status',lambda:dict(active=True,events=[],latest=dict(report=report,episodes=[]),
        heartbeat=dict(status='collecting',heartbeat_at=previous)))
    monkeypatch.setattr(panel.jobs,'job_status',lambda _:dict(running=False))
    monkeypatch.setattr(panel,'now_tehran',lambda:NOW)
    app=AppTest.from_string('from dashboard.opportunity_radar import radar_view\nradar_view()').run()
    assert not app.exception
    captions=[item.value for item in app.caption]
    warnings=[item.value for item in app.warning]
    assert any('وضعیت فعلی فرایند پایش: متوقف' in text for text in captions)
    assert any('مرز زمانیِ آخرین پیمایش محفوظ خبر' in text for text in captions)
    assert any('پایش پس‌زمینه فعال نیست' in text for text in warnings)
    assert any('ضربان فرایند کهنه یا نامعلوم' in text for text in warnings)
    assert not any('دریافت در حال انجام است' in item.value for item in app.info)


def source_row(status='stale_session', *, last_observed_date='2026-10-24', date='2026-10-25', code='123'):
    return dict(symbol='آزمون',ins_code=code,status=status,last_observed_date=last_observed_date,date=date,
                reasons={key:None for key in REASONS})


@pytest.mark.parametrize('invalid',[None,'invalid','2026-02-30','2026-10-24T12:00:00+03:30',True])
def test_stale_requested_date_never_supplies_missing_or_invalid_source_session(invalid):
    row=source_row(last_observed_date=invalid)
    summary=panel.price_session_summary([row],requested_session='2026-10-25')
    assert summary==dict(requested_session='2026-10-25',latest_source_session=None,valid_target_candles=0)


def test_observed_candle_can_supply_source_session_but_stale_target_cannot():
    rows=[source_row('observed',last_observed_date=None),source_row(last_observed_date='2026-10-23',code='124')]
    summary=panel.price_session_summary(rows,requested_session='2026-10-25')
    assert summary['latest_source_session']=='2026-10-25' and summary['valid_target_candles']==1
    assert panel.price_session_summary(rows,requested_session='2026-10-26')['valid_target_candles']==0
    assert panel.price_session_summary(rows,requested_session='invalid')['requested_session'] is None


def render_price_report(monkeypatch, rows, *, price_retry=None):
    report=dict(scope=[dict(ins_code=row['ins_code']) for row in rows],rows=rows,
        price_session_date=NOW.date().isoformat(),price_observed_at=NOW.isoformat(),
        news_coverage={},source_coverage=dict(source_scope_count=0,known_missing_from_watch=[]))
    if price_retry is not None:
        report['price_retry']=price_retry
    monkeypatch.setattr(panel,'status',lambda:dict(active=True,events=[],latest=dict(report=report,episodes=[]),
        heartbeat=dict(status='degraded',heartbeat_at=NOW.isoformat())))
    monkeypatch.setattr(panel.jobs,'job_status',lambda _:dict(running=True))
    monkeypatch.setattr(panel,'now_tehran',lambda:NOW)
    return AppTest.from_string('from dashboard.opportunity_radar import radar_view\nradar_view()').run()


def test_requested_today_with_all_stale_rows_displays_source_date_and_zero_valid_warning(monkeypatch):
    rows=[source_row(last_observed_date='2026-10-24'),source_row(last_observed_date='2026-10-23',code='124')]
    app=render_price_report(monkeypatch,rows)
    assert not app.exception
    captions=[item.value for item in app.caption]
    warnings=[item.value for item in app.warning]
    assert any('جلسهٔ مورد بررسی: '+to_jalali('2026-10-25') in text for text in captions)
    assert any('آخرین جلسهٔ موجود در دادهٔ منبع: '+to_jalali('2026-10-24') in text for text in captions)
    assert any('زمان دریافت پاسخ، تاریخ جلسهٔ منبع را تأیید نمی‌کند' in text for text in captions)
    assert any('هیچ کندل معتبر تأیید نشده' in text and to_jalali('2026-10-24') in text for text in warnings)
    assert any('قیمت‌های منبع مربوط به امروز نیستند' in text for text in warnings)
    assert next(item for item in app.metric if item.label=='کندل معتبر در جلسهٔ مورد بررسی').value=='0'
    assert app.dataframe[0].value['آخرین_جلسه_منبع'].tolist()==[to_jalali('2026-10-24'),to_jalali('2026-10-23')]


def test_invalid_source_date_is_unknown_in_rendered_header_and_not_replaced_by_receipt_date(monkeypatch):
    app=render_price_report(monkeypatch,[source_row(last_observed_date='2026-13-24')])
    assert not app.exception
    assert any('آخرین جلسهٔ موجود در دادهٔ منبع: نامعلوم' in item.value for item in app.caption)
    assert any('هیچ کندل معتبر تأیید نشده' in item.value and 'نامعلوم' in item.value for item in app.warning)
    assert app.dataframe[0].value['آخرین_جلسه_منبع'].tolist()==['نامعلوم']


def test_daily_history_budget_is_visible_as_recorded_deferral(monkeypatch):
    deadline='2026-10-26T00:00:00+03:30'
    app=render_price_report(monkeypatch,[source_row()],price_retry=dict(
        deferred=True,daily_limit_reached=True,not_before=deadline))
    assert not app.exception
    assert any('سقف روزانهٔ دریافت تاریخچه' in item.value
               and to_jalali(deadline,with_time=True) in item.value for item in app.warning)


@pytest.mark.parametrize('deadline',[None,'bad'])
def test_daily_history_budget_with_unknown_deadline_does_not_crash(monkeypatch,deadline):
    app=render_price_report(monkeypatch,[source_row()],price_retry=dict(
        deferred=True,daily_limit_reached=True,not_before=deadline))
    assert not app.exception
    assert any('سقف روزانهٔ دریافت تاریخچه' in item.value and 'نامعلوم' in item.value
               for item in app.warning)
