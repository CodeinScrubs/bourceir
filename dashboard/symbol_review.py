"""نمایش پوشش و مرز شواهد، مستقل از پیشنهاد معامله."""

import pandas as pd
import streamlit as st

from core.timeutils import to_jalali
from database.db import DatabaseManager
from database.symbol_review import load_symbol_review
from ops import jobs


@st.cache_data(ttl=60, show_spinner=False)
def cached_review(database_path, symbol):
    with DatabaseManager(db_path=database_path, read_only=True) as db:
        return load_symbol_review(db, symbol)


def symbol_review_panel(database_path, symbol):
    with st.expander('🔎 کیفیت شواهد و رویدادهای نماد', expanded=False):
        report = cached_review(str(database_path), symbol)
        cols = st.columns(3)
        cols[0].metric('روز قیمت ثبت‌شده', report['rows'])
        cols[1].metric('شکاف قیمت مرجع', len(report['reference_gaps']))
        cols[2].metric('رویداد شرکتی ثبت‌شده', report['recorded_action_rows'])
        st.caption('شکاف مرجع، افزایش سرمایه یا مجمع تأییدشده نیست. از آن نوع رویداد، سود نقدی، '
                   'حق‌تقدم یا زمان قابل فروش شدن سهم استنتاج نمی‌شود. تطبیق اقتصادی رویدادها کامل نیست.')
        st.write(f"هشدار آماری قیمت: {report['price_anomalies']}؛ حجم: {report['volume_anomalies']}؛ "
                 f"تاریخ یکتای هشدار: {report.get('anomaly_rows', 0)}")
        st.caption('هشدار آماری به معنی خرابی داده نیست و رکورد را حذف نمی‌کند. بازگشایی هم می‌تواند پرش واقعی داشته باشد.')
        if report['extra_client_dates'] or report['missing_client_dates']:
            st.warning(f"آمار حقیقی/حقوقی بدون کندل: {len(report['extra_client_dates'])} روز؛ "
                       f"کندل بدون آمار حقیقی/حقوقی: {len(report['missing_client_dates'])} روز. علت نیاز به بررسی منبع دارد.")
        gap = report.get('longest_calendar_gap')
        if gap:
            st.write(f"بزرگ‌ترین فاصله تقویمی دو کندل: {gap['calendar_days']} روز، از {to_jalali(gap['start'])} "
                     f"تا {to_jalali(gap['end'])}. در میانه این فاصله، {gap['observed_market_sessions_without_symbol']} "
                     'جلسه با قیمت حداقل ۲۰ سهم عادی ثبت شده ولی این نماد کندل ندارد.')
            st.caption('این شمارش تقویم رسمی یا اثبات توقف نماد نیست؛ نبود داده برای کل بازار با توقف اختصاصی فرق دارد.')
        if not report.get('volume_comparable_20d', False) or not report.get('volume_comparable_30d', False):
            st.warning('حداقل یکی از پنجره‌های حجم ۲۰/۳۰ جلسه، مرجع نامعلوم یا شکاف مرجع دارد؛ '
                       'نسبت حجم در آن پنجره نامعلوم است. تعداد برگه سهم قبل و بعد از رویداد هم‌واحد فرض نمی‌شود.')
        if report['reference_gaps']:
            frame = pd.DataFrame(report['reference_gaps']).tail(20)
            frame['date'] = frame.date.map(to_jalali)
            st.dataframe(frame.rename(columns={'date': 'تاریخ', 'ref_price_raw': 'مرجع (ریال)',
                'previous_official_close_rial': 'پایانی قبلی (ریال)', 'reference_ratio': 'مرجع / پایانی قبلی',
                'calendar_gap_days': 'فاصله تقویمی'}), hide_index=True, width='stretch')
        snap = report['snapshot']
        if snap:
            st.write('زمان منبع آخرین تابلو: ' + (to_jalali(snap['provider_timestamp'], with_time=True)
                     if snap.get('provider_timestamp') else 'نامعلوم'))
            if not report['snapshot_current']:
                st.warning('این تابلو تازه نیست؛ صف تاریخی وضعیت فعلی صف یا امکان خروج را ثابت نمی‌کند.')
        else:
            st.info('اسنپ‌شات تابلو موجود نیست؛ وضعیت صف فعلی نامعلوم است.')
        if report['announcements']:
            st.dataframe(pd.DataFrame(report['announcements']).rename(columns={'title': 'عنوان', 'published_at': 'زمان انتشار',
                'captured_at': 'زمان دریافت', 'tracing_no': 'پیگیری', 'url': 'منبع'}), hide_index=True, width='stretch')
        else:
            st.warning('اطلاعیه‌ای برای این نماد در آرشیو محلی ثبت نشده؛ این به معنی نبود خبر یا رویداد نیست.')
        st.code(f'.venv/Scripts/python.exe brs.py codal --symbol {symbol} --pages 12', language='powershell')
        status = jobs.job_status('codal')
        if st.button('دریافت هدفمند اطلاعیه‌های این نماد', disabled=status['running'], key='symbol-codal'):
            jobs.start_job('codal', ['codal', '--symbol', symbol, '--pages', '12'])
            st.rerun()
        if status['started_at']:
            st.caption('آخرین درخواست کدال: ' + ' '.join(status['args']) + (' — در حال اجرا' if status['running'] else ''))
            if status['log_tail']:
                st.code('\n'.join(status['log_tail']), language=None)
        st.caption('دریافت هدفمند سقف صفحه دارد؛ داده مالی تأییدشده، تاریخچه کامل قوانین/عضویت بازار '
                   'و ترتیب اجرای سفارش هنوز کامل نیستند. این بخش سیگنال خرید یا فروش نمی‌سازد.')
