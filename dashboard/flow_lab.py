"""Explainable ordinary-equity daily filters; no execution, ranking score or probability."""

import pandas as pd
import streamlit as st

from core.flow_filters import FILTER_NAMES, FLOW_FILTER_VERSION, filter_conditions, filter_diagnostics, flow_metrics
from core.flow_intervals import estimate_retail_intervals
from core.screening import current_daily_row
from core.timeutils import to_jalali
from database.db import DatabaseManager
from database.flow_data import load_flow_inputs


@st.cache_data(ttl=60, show_spinner=False)
def _latest_metrics(database_path, version):
    with DatabaseManager(db_path=database_path, read_only=True) as db:
        prices, clients = load_flow_inputs(db, tail_per_symbol=65, include_inactive=False)
    if prices.empty:
        return pd.DataFrame()
    metrics = flow_metrics(prices, clients)
    # One common as-of date: never mix each symbol's last session into a market cross-section.
    return metrics.loc[metrics.date == metrics.date.max()].reset_index(drop=True)


def page_flow_lab():
    from config import settings
    st.title('🧪 آزمایشگاه فیلترها')
    st.caption('تطبیق شرط با دادهٔ روزانهٔ ثبت‌شده؛ خروجی توصیه خرید، احتمال صف خرید یا پیش‌بینی سود نیست.')
    with st.spinner('در حال تطبیق داده روزانه و شرط‌ها…'):
        metrics = _latest_metrics(str(settings.DB_PATH), FLOW_FILTER_VERSION)
    if metrics.empty:
        st.info('ابتدا تاریخچه واقعی قیمت و حقیقی/حقوقی را دریافت کن.')
        return
    name = st.selectbox('فیلتر توصیفی', list(FILTER_NAMES), format_func=FILTER_NAMES.get)
    date = str(metrics.date.max())
    st.write(f'روز مشترک داده: {to_jalali(date)} — {len(metrics)} نماد عادی فعال دارای ردیف در این روز')
    coverage = int(metrics['buyer_power_value'].notna().sum())
    st.caption(f'قدرت ارزشی قابل محاسبه: {coverage} از {len(metrics)} نماد؛ آمار مفقود یا شمارنده نامعتبر، نامعلوم باقی می‌ماند.')
    with DatabaseManager(read_only=True) as db:
        calendar = db.get_market_calendar()
    if not current_daily_row(date, calendar=calendar):
        st.warning('این روز، آخرین جلسه کامل مورد انتظار نیست؛ نتایج آرشیوی‌اند.')
    st.info('نسخه روزانه فقط پس از تکمیل جلسه قابل تفسیر است. برای الگوی منفی به مثبت در ساعت ۹ تا ۱۰:۳۰، '
            'ترتیب معاملات، تاریخچه هم‌ساعت و دفتر سفارش تازه لازم است؛ داده پایان روز جای آن‌ها را نمی‌گیرد.')
    st.caption('در نسخه v2، پنجره حجم دارای شکاف مرجع یا مرجع نامعلوم، قابل مقایسه فرض نمی‌شود؛ '
               'نسبت حجم آن نامعلوم می‌ماند. از مرجع، تعداد سهم پس از مجمع حدس زده نمی‌شود.')
    diagnostics = filter_diagnostics(metrics, name)
    c1, c2, c3 = st.columns(3)
    c1.metric('منطبق با همه شروط معلوم', int(diagnostics.match.sum()))
    c2.metric('دارای حداقل یک شرط نامعلوم', int((diagnostics.known_conditions < diagnostics.total_conditions).sum()))
    c3.metric('دارای حداقل یک شرط ردشده', int((diagnostics.failed_conditions > 0).sum()))
    if name == 'special_original':
        st.warning('نسخه دقیق فیلتر ۹ شرطی کامل نیست: شناوری تاریخی، سقف مجاز همان روز و تعریف/جامعه میانگین بازار '
                   'موجود نیستند. شروط آن با میانگین خود سهم یا دامنه ثابت ۳٪ جایگزین نشده‌اند.')
    only = st.checkbox('فقط نمادهای منطبق با همه شروط معلوم', value=False)
    table = diagnostics.merge(metrics, on=['symbol', 'date'], validate='one_to_one')
    if only:
        table = table.loc[table.match]
    table = table.sort_values(['match', 'value', 'symbol'], ascending=[False, False, True])
    view = table[['symbol', 'status', 'known_conditions', 'total_conditions', 'buyer_power_value',
                  'buyer_power_volume', 'volume_ratio_20d', 'volume_ratio_30d', 'missing_conditions']].rename(columns={
        'symbol': 'نماد', 'status': 'نتیجه شرط‌ها', 'known_conditions': 'شرط معلوم', 'total_conditions': 'کل شرط‌ها',
        'buyer_power_value': 'قدرت ارزشی', 'buyer_power_volume': 'قدرت حجمی', 'volume_ratio_20d': 'حجم / ۲۰ جلسه قبل',
        'volume_ratio_30d': 'حجم / ۳۰ جلسه قبل', 'missing_conditions': 'شرط نامعلوم'})
    for label in ('قدرت ارزشی', 'قدرت حجمی', 'حجم / ۲۰ جلسه قبل', 'حجم / ۳۰ جلسه قبل'):
        view[label] = view[label].map(lambda v: 'نامعلوم' if pd.isna(v) else f'{v:.3f}')
    st.dataframe(view, hide_index=True, width='stretch')
    if not table.empty:
        symbol = st.selectbox('جزئیات شرط‌های نماد', table.symbol.tolist())
        selected = metrics.loc[metrics.symbol == symbol]
        checks = filter_conditions(selected, name).iloc[0]
        st.dataframe(pd.DataFrame({'شرط': checks.index, 'وضعیت': [
            'نامعلوم' if pd.isna(v) else 'برقرار' if v else 'برقرار نیست' for v in checks]}),
            hide_index=True, width='stretch')
        values = selected.iloc[0]
        st.caption('کیفیت ارزش حقیقی: خرید '+str(values.get('client_buy_value_quality'))+
                   '؛ فروش '+str(values.get('client_sell_value_quality'))+
                   '. جمع هر سمت و قیمت ضمنی با داده روز کنترل می‌شود؛ ناسازگاری به مقدار نامعلوم تبدیل می‌شود.')
        st.caption('سرانه ارزشی از ارزش واقعی خرید/فروش حقیقی تقسیم بر تعداد همان سمت محاسبه شده؛ '
                   'قدرت حجمی از حجم محاسبه می‌شود. برابر بودن آن‌ها فرض نشده است.')
        caps = {label: values.get(key) for label, key in {
            'سرانه خرید امروز (ریال)': 'per_capita_buy_rial', 'میانگین حسابی ۳ جلسه (ریال)': 'per_capita_buy_mean_3d_rial',
            'میانگین حسابی ۵ جلسه (ریال)': 'per_capita_buy_mean_5d_rial',
            'ارزش تجمیعی / تعداد تجمیعی ۳ جلسه (ریال)': 'per_capita_buy_pooled_3d_rial',
            'خالص ارزش خرید حقیقی (ریال)': 'net_real_value_rial'}.items()}
        st.dataframe(pd.DataFrame({'شاخص': caps.keys(), 'مقدار': caps.values()}), hide_index=True, width='stretch')
    st.caption('میانگین سرانه ۳ و ۵ جلسه شامل امروز است. میانگین حجم/ارزش ۲۰ و ۳۰ جلسه امروز را شامل نمی‌شود. '
               'هر پنجره کامل لازم است؛ مشاهده مفقود حذف یا صفر نمی‌شود. تعداد تجمیعی، شخص یکتای چندروزه نیست. '
               'تمام سقف‌های ۶۰ جلسه با شاخص علّی مقایسه می‌شوند؛ امتیاز ۰ تا ۱۰۰ و احتمال موفقیت ساخته نشده است.')


def interval_panel(symbols):
    with st.expander('برآورد تغییر شمارنده‌های حقیقی — پژوهشی'):
        st.caption('حجم تغییرکرده ÷ افزایش شمار حقیقی‌ها × قیمت آخرِ انتهای بازه؛ مشاهده سفارش منفرد نیست. '
                   'حجم بدون افزایش شمار فقط در بازه‌های پیوسته انباشته می‌شود؛ بازنگری شمارنده یا فاصله آرشیو بیش از ۴۵ ثانیه آن را قطع می‌کند. '
                   'پایش فعلی ۳۰ثانیه‌ای است و معادل نمونه‌برداری ۵ یا ۱۵ثانیه‌ای سایت دیگر نیست.')
        symbol = st.selectbox('نماد بررسی شمارنده', sorted(symbols))
        threshold = st.radio('آستانه برآورد سرانه (میلیون تومان)', [200, 100], horizontal=True)
        with DatabaseManager(read_only=True) as db:
            records = db.conn.execute("""SELECT sn.*, s.symbol_fa FROM snapshots sn
                JOIN symbols s ON s.id=sn.symbol_id WHERE s.symbol_fa=? AND sn.source='tsetmc'
                ORDER BY sn.captured_at DESC, sn.id DESC LIMIT 200""", (symbol,)).fetchall()
        report = estimate_retail_intervals([dict(r) for r in reversed(records)], threshold_rial=threshold*10_000_000)
        if report['events']:
            view = pd.DataFrame(report['events'])
            view['سمت'] = view.side.map({'buy': 'خرید', 'sell': 'فروش'})
            view['از'] = view.start.map(lambda x: to_jalali(x, with_time=True))
            view['تا'] = view.end.map(lambda x: to_jalali(x, with_time=True))
            view['برآورد سرانه (تومان)'] = view.estimated_per_new_count_rial/10
            st.dataframe(view[['سمت', 'از', 'تا', 'برآورد سرانه (تومان)', 'delta_count', 'delta_volume']].rename(
                columns={'delta_count': 'افزایش شمار', 'delta_volume': 'تغییر حجم'}), hide_index=True, width='stretch')
        else:
            st.info('در حداکثر ۲۰۰ مشاهده آخرِ این نماد، برآورد معتبر بالای آستانه ثبت نشد؛ این به معنی نبود خرید/فروش درشت نیست.')
        reasons = {'invalid_snapshot': 'مشاهده نامعتبر', 'new_session': 'روز جدید', 'nonmonotonic_clock': 'ترتیب ساعت ناسازگار',
                   'archive_gap': 'فاصله آرشیو', 'counter_revision_or_reset': 'اصلاح یا بازنشانی شمارنده'}
        skipped = '؛ '.join(f'{reasons[k]}: {v}' for k, v in report['rejected_segments'].items()) or 'موردی ندارد'
        st.caption(f"بخش‌های کنارگذاشته‌شده: {skipped} — ساعت مستقل شمارنده‌های حقیقی و پوشش کامل جلسه تأیید نشده‌اند. "
                   'فاصله ردیف‌های ذخیره‌شده به‌تنهایی قطعی پایش را ثابت نمی‌کند.')
