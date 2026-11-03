"""Small local overview of monitoring health, distinct from data freshness."""

import streamlit as st

from config import settings
from core.timeutils import now_tehran, to_jalali
from ops import jobs
from ops.command_health import command_health


@st.fragment(run_every=15)
def command_center_panel():
    """Show failures at the home desk; this read-only view never starts collectors."""
    try:
        report = command_health(settings.DATA_DIR/'private',
                                radar_worker=jobs.job_status('opportunity-radar'),
                                live_worker=jobs.job_status('live'), at=now_tehran())
    except (OSError, ValueError, TypeError, KeyError) as error:
        st.warning('وضعیت پایش محلی قابل بررسی نیست: '+str(error))
        return
    st.subheader('وضعیت پایش')
    st.caption('وضعیت فرایندهای ثبت‌شده در داشبورد است؛ اجراهای دستی مستقل ممکن است در این بخش دیده نشوند.')
    radar = report['radar']
    columns = st.columns(3)
    labels = {'running':'در حال اجرا', 'stopped':'فعال بودن احراز نشد', 'unknown':'نامعلوم'}
    columns[0].metric('فرایند رادار', labels[radar['worker']])
    columns[1].metric('پایش عمومی', labels[report['public_live_worker']])
    board_labels = {'current':'وضعیت تازه', 'stale':'متوقف یا کهنه', 'absent':'شروع نشده', 'invalid':'نامعتبر'}
    columns[2].metric('پایش تابلو فارابی', board_labels[report['board_state']])
    if radar['heartbeat_at']:
        st.caption('آخرین ضربان رادار: '+to_jalali(radar['heartbeat_at'], with_time=True)+
                   '؛ تازگی ضربان '+('تأیید شد' if radar['heartbeat_fresh'] else 'تأیید نشد')+
                   '؛ آخرین جلسه قیمت ثبت‌شده: '+
                   (to_jalali(radar['price_session_date']) if radar['price_session_date'] else 'نامعلوم'))
    else:
        st.caption('ضربان رادار موجود نیست یا زمان آن قابل بررسی نیست.')
    if radar['worker'] != 'running' or not radar['heartbeat_fresh']:
        st.warning('رادار فعال و دارای ضربان تازه تأیید نشده است؛ صفحه «رادار فرصت‌های پژوهشی» را بررسی کن.')
    if radar['recorded_status'] in ('degraded', 'error'):
        st.warning('آخرین وضعیت ثبت‌شده رادار ناقص یا خطادار است؛ جزئیات را در صفحه رادار ببین.')
    if radar['news_window_complete'] is False:
        st.warning('آخرین پیمایش بازه خبر کامل تأیید نشده است؛ ممکن است اطلاعیه‌ای جا افتاده باشد.')
    if report['board_state'] == 'current':
        st.caption(f"تعداد تابلوهای دارای برداشت معتبر در آخرین وضعیت: {report['board_valid_extracts']}؛ "
                   'تازگی مستقل صف و تحویل صدا باید جدا بررسی شوند.')
    else:
        st.caption('وضعیت دیده‌بان مستقل فارابی فعال و تازه تأیید نشده است؛ برای این مسیر WATCH_FARABI.bat را اجرا کن.')
    st.caption('پنل‌های نصب‌شده در مرورگر داخلی جداگانه‌اند و وضعیت آن‌ها از این نشانگر قابل تأیید نیست.')
    if report['codal_document_source'] == 'blocked':
        st.warning('گردآوری اسناد کدال برای پژوهش تاریخی متوقف است؛ نشانگر خطا نیاز به بازبینی منبع دارد.')
    elif report['codal_document_source'] == 'invalid':
        st.warning('وضعیت توقف منبع اسناد کدال نامعتبر یا ناخواناست؛ دریافت موفق تأیید نمی‌شود.')
    st.caption('اجرای فرایند یا تازگی ضربان، تازگی قیمت و صف، تحویل هشدار یا امکان معامله را ثابت نمی‌کند.')
