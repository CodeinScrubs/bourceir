"""نمای منبع سود نقدی؛ هیچ مبلغ نامزد به ریال یا نقد قابل خرج تبدیل نمی‌شود."""

import pandas as pd
import streamlit as st

from config import settings
from core.timeutils import to_jalali
from ops.dividend_evidence import load_dividend_evidence


def show_dividend_evidence():
    root = settings.DATA_DIR / 'private/dividend-evidence'
    folders = sorted(root.glob('DIVIDENDS-*'), key=lambda p:p.stat().st_mtime_ns, reverse=True)[:10] if root.exists() else []
    if not folders:
        st.info('هنوز منبع سود نقدی بایگانی نشده است؛ افت قیمت مرجع، سود نقدی فرض نمی‌شود.')
        return
    folder = st.selectbox('آرشیو منبع سود نقدی', folders, format_func=lambda p:p.name)
    if not (folder / 'manifest.json').exists():
        st.warning('تلاش ناقص محفوظ است؛ هنوز نتیجه قابل بازبینی ندارد.')
        return
    try:
        result = load_dividend_evidence(folder)
    except (ValueError, OSError) as error:
        st.error('آرشیو منبع قابل تأیید نیست: ' + str(error))
        return
    st.warning('واحد، معنای رقم، هویت تاریخی، استحقاق و وصول تأیید نشده‌اند. این جدول وجه قابل خرج، فیچر یا مجوز معامله نیست.')
    labels = dict(observed_unverified='مشاهده؛ تأییدنشده', empty_response='پاسخ خالی؛ نبود سود ثابت نیست',
                  invalid_schema='ساختار نامعتبر', unavailable='دریافت ناموفق')
    summary, rows = [], []
    for case in result['cases']:
        parsed = case['assessment']
        summary.append({'نماد درخواست': case['request']['symbol'], 'وضعیت': labels[case['status']],
            'ردیف منبع': None if parsed is None else parsed['source_rows'],
            'ردیف قابل خواندن پیش از مرز': None if parsed is None else parsed['development_rows'],
            'نیازمند بررسی تاریخ/ساختار': None if parsed is None else len(parsed['quarantined_rows']),
            'کنارگذاشته‌شده با کنترل مرز': None if parsed is None else parsed['withheld_rows']})
        for row in parsed['rows'] if parsed else []:
            rows.append({'نماد درخواست': case['request']['symbol'], 'ردیف پاسخ': row['source_row'],
                'تاریخ اظهارشده انتشار': to_jalali(row['stated_publication_date']),
                'تاریخ اظهارشده مجمع': to_jalali(row['stated_meeting_date']),
                'پایان سال مالی اظهارشده': to_jalali(row['stated_fiscal_year_end']),
                'رقم نامزد؛ واحد تأییدنشده': row['candidate_dps_amount'] if row['candidate_dps_amount'] is not None else 'نامعلوم',
                'گروه تکراری/اصلاحی': 'بله' if row['duplicate_meeting'] else 'خیر',
                'ارقام معلوم متفاوت': 'بله' if row['conflicting_amounts'] else 'خیر'})
    st.dataframe(pd.DataFrame(summary), hide_index=True)
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True)
    st.caption('تاریخ اظهارشده، زمان اثبات‌شده انتشار نیست. همه نسخه‌های تکراری محفوظ‌اند و با هم جمع نمی‌شوند. '
               'ردیف پس از مرز توسعه نمایش داده نمی‌شود. هش محلی و بازحساب، اصالت یا کامل‌بودن منبع را ثابت نمی‌کنند.')
