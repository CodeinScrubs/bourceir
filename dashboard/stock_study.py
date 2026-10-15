"""نمای گزارش توصیفی؛ ثبت مطالعه، مجوز سیگنال نمی‌سازد."""

import pandas as pd
import streamlit as st

from core.timeutils import to_jalali
from ops.stock_study import load_latest_study


def page_stock_study():
    st.title('📚 مطالعه رشد نمادها')
    st.caption('بازه‌های انتخاب‌شده پس از مشاهده رشد، مطالعه اکتشافی‌اند. گروه مقایسه فعلی و داده‌های پیش از شروع '
               'کمک به بررسی روایت می‌کنند؛ احتمال موفقیت، علت قطعی یا توصیه خرید از آن‌ها ساخته نمی‌شود.')
    try:
        loaded=load_latest_study()
    except (ValueError,OSError,KeyError,TypeError) as exc:
        st.error(str(exc));return
    if not loaded:
        st.info('هنوز مطالعه توصیفی ثبت نشده است. راهنمای ساخت پرونده در docs/research/stock-study.md است.')
        return
    result,folder=loaded
    st.write('زمان گزارش: '+to_jalali(result['created_at'],with_time=True))
    rows=[]
    def number(value):
        return 'نامعلوم' if value is None else f'{value:,.2f}'
    for case in result['cases']:
        window=case.get('window') or {}
        cap=case.get('market_cap') or {}
        rows.append({'نماد':case['symbol'],'صنعت فعلی':case.get('sector_name'),'شروع':to_jalali(case['start']),
                     'پایان':to_jalali(case['end']),'نسبت آخرین قیمت خام':number(window.get('last_price_multiple')),
                     'نسبت شاخص مصنوعی':number(window.get('synthetic_multiple')),
                     'ارزش بازار مشاهده؛ همت':number(cap.get('market_cap_rial')/1e13 if cap.get('market_cap_rial') is not None else None)})
    st.dataframe(pd.DataFrame(rows),hide_index=True)
    st.warning('تعداد سهام و صنعت امروز به گذشته منتقل نشده‌اند. رتبه تاریخی، بازده اقتصادی و امکان اجرا هنوز تأیید نشده‌اند. '
               'خروجی قدیمی پژوهش با تغییر اعتبارسنجی معتبر نمی‌شود؛ نتیجه جدید نیازمند اجرای مستقل و پذیرش جدا است.')
    st.download_button('دریافت گزارش کامل آفلاین با نمودار و کدال',(folder/'report.html').read_bytes(),
                       file_name='brs-stock-study.html',mime='text/html')
    st.download_button('دریافت شواهد عددی مطالعه',(folder/'study.json').read_bytes(),
                       file_name='brs-stock-study.json',mime='application/json')
