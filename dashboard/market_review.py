"""نمایش محلی گزارش جلسه؛ بدون دریافت پنهانی یا پذیرش سیگنال."""

import pandas as pd
import streamlit as st

from config import settings
from core.market_review import STATUS_LABELS_FA
from core.text import to_fa_digits
from core.timeutils import now_tehran, to_jalali
from ops.market_review import load_market_review, render_html


def page_market_review(job_panel):
    st.title('🌐 بررسی بازار امروز')
    st.caption('فقط سهام عادی بورس و فرابورس؛ تاریخ هر سهم مستقل کنترل می‌شود. گزارش توصیفی است و پیشنهاد معامله ندارد.')
    job_panel('market-review', ['market-review'], 'دریافت گزارش جلسه جاری',
              'دیده‌بان، تاریخ و کندل هر سهم و شاخص‌های رسمی را فقط از داده عمومی دریافت و شواهد را حفظ می‌کند؛ '
              'مدت دریافت به پوشش و دسترسی منبع بستگی دارد. هیچ تغییری در حساب یا دیتابیس معاملاتی ایجاد نمی‌کند.')
    root = settings.DATA_DIR / 'private' / 'market-reviews'
    folders = sorted((path.parent for path in root.glob('*/report.json')),
                     key=lambda path: ((path / 'report.json').stat().st_mtime, path.name))
    if not folders:
        st.info('هنوز گزارش جلسه‌ای ثبت نشده است. برای دریافت، دکمه بالا را بزنید.')
        return
    labels = []
    for folder in folders:
        labels.append(folder.name)
    chosen = st.selectbox('گزارش محفوظ', labels, index=len(labels) - 1)
    try:
        report = load_market_review(root / chosen)
    except (ValueError, OSError) as exc:
        st.error(f'گزارش یا شواهد آن قابل تأیید نیست: {exc}')
        return
    st.subheader(f"جلسه {to_jalali(report['session_date'])}")
    st.caption(f"زمان مشاهده: {to_jalali(report['observed_at'], with_time=True)}؛ شناسه {report['run_id']}")
    if report['session_date'] != now_tehran().date().isoformat():
        st.warning('این گزارش مربوط به امروز نیست؛ دریافت تازه به‌صورت خودکار انجام نمی‌شود.')
    if report['status'] != 'observed':
        st.warning('پوشش ناقص است؛ نماد نامعلوم یا نامعتبر در مخرج پوشش باقی می‌ماند و بازده برایش ساخته نمی‌شود.')
    summary = report['summary']
    cols = st.columns(3)
    cols[0].metric('کندل معتبر', to_fa_digits(summary['traded_count']))
    cols[0].caption(to_fa_digits(f"از {summary['scope_count']} نماد داخل دامنه دیده‌بان"))
    cols[1].metric('سهم منفی', to_fa_digits(summary['negative']))
    cols[1].caption(to_fa_digits(f"{summary['positive']} سهم مثبت؛ {summary['unchanged']} سهم بدون تغییر"))
    cols[2].metric('میانه بازده پایانی', 'نامعلوم' if summary['median_closing_return'] is None
                   else '\u2066' + to_fa_digits(f"{100 * summary['median_closing_return']:+.2f}٪") + '\u2069')
    st.caption('قرینه معاملات تک‌قیمت، صف تأییدشده نیست. خالص ارزش حقیقی تاریخ‌دار و زمان مستقل مظنه‌ها هنوز معلوم نیست.')
    st.dataframe(pd.DataFrame([{'وضعیت داده': STATUS_LABELS_FA.get(key, 'وضعیت نامعلوم'), 'شمار': value}
                               for key, value in summary['status_counts'].items()]), hide_index=True, width='stretch')
    st.subheader('صنایع — میانه شرکت‌های پوشش‌داده‌شده')
    frame = pd.DataFrame([{'صنعت': row['name'] or row['code'] or 'نامعلوم', 'دامنه': row['scope_count'],
                          'کندل معتبر': row['traded_count'], 'مثبت': row['positive'], 'منفی': row['negative'],
                          'میانه بازده (%)': None if row['median_closing_return'] is None else row['median_closing_return'] * 100,
                          'ارزش (میلیارد تومان)': None if row['value_rial'] is None else row['value_rial'] / 1e10}
                          for row in report['sectors']])
    st.dataframe(frame, hide_index=True, width='stretch')
    st.caption('این میانه، شاخص رسمی صنعت نیست؛ رتبه‌ها فقط بین نمادهای معتبر پوشش‌داده‌شده‌اند.')
    st.download_button('دریافت گزارش کامل HTML', render_html(report).encode('utf-8'),
                       file_name='BRS-market-' + report['session_date'] + '.html', mime='text/html')
