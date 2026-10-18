"""میز روزانه مشترک خانه و بازار؛ هیچ دریافت شبکه یا نوشتن حساب پنهان ندارد."""

import hashlib

import pandas as pd
import streamlit as st

from config import settings
from core.flow_filters import FLOW_FILTER_V4_VERSION
from core.timeutils import now_tehran, to_jalali
from database.db import DatabaseManager
from database.flow_data import load_flow_inputs
from ops.operator_status import operator_status


@st.cache_data(ttl=60, show_spinner=False)
def _market_history(path):
    with DatabaseManager(db_path=path, read_only=True) as db:
        p, c = load_flow_inputs(db, tail_per_symbol=65, include_inactive=False)
    return p, c


@st.cache_data(ttl=3600, show_spinner=False)
def _cached_v4(digest, version, _prices, _clients):
    from core.flow_filters import flow_metrics_v4
    return flow_metrics_v4(_prices, _clients)


def _v4_input_digest(prices, clients):
    digest = hashlib.sha256()
    digest.update(repr(settings.BACKTEST_CONFIG['lock_min_move_pct']).encode())
    for name in ('flow_filters', 'client_quality', 'market_rules', 'symbol_review', 'text'):
        digest.update((settings.BASE_DIR / 'core' / (name + '.py')).read_bytes().replace(b'\r\n', b'\n'))
    for frame in (prices, clients):
        digest.update(repr(list(zip(frame.columns, map(str, frame.dtypes)))).encode())
        digest.update(pd.util.hash_pandas_object(frame, index=True).to_numpy().tobytes())
    return digest.hexdigest()


def latest_v4_metrics(path):
    p, c = _market_history(path)
    if p.empty:
        return pd.DataFrame()
    # همه ردیف‌ها در کلیدند؛ نمونه‌گیری خودکار hash دیتافریم‌های بزرگ کافی نیست.
    metrics = _cached_v4(_v4_input_digest(p, c), FLOW_FILTER_V4_VERSION, p, c)
    date = metrics.date.max()
    return metrics.loc[metrics.date == date].reset_index(drop=True)


def operator_panel():
    st.subheader('میز روزانه — داده، فرضیه و دو دفتر')
    path = str(settings.DB_PATH)
    p, c = _market_history(path)
    date = None if p.empty else str(p.date.max())
    market = pd.DataFrame() if p.empty else p.loc[p.date == date].merge(
        c, on=['date', 'symbol'], how='left', validate='one_to_one')
    if st.button('بررسی فرضیه‌های پژوهشی روی داده موجود', key='operator-evaluate'):
        st.session_state['operator-evaluated-path'] = path
    evaluated = st.session_state.get('operator-evaluated-path') == path
    metrics = pd.DataFrame()
    if evaluated:
        with st.spinner('تطبیق فرضیه‌ها با تاریخچه بازار؛ بار اول ممکن است چند دقیقه طول بکشد…'):
            metrics = latest_v4_metrics(path)
    with st.spinner('بررسی محلی پوشش داده و دفترها…'):
        with DatabaseManager(read_only=True) as db:
            report = operator_status(db, metrics, market, at=now_tehran())
    a, b, c = st.columns(3)
    a.metric('نامزد روش پذیرفته‌شده', 0)
    b.metric('منطبق با فرضیه‌های پژوهشی', len(report['candidates']) if evaluated else 'بررسی نشده')
    c.metric('پورتفوی مجازی', len(report['paper_portfolios']))
    st.caption(f"جلسه کامل مورد انتظار: {to_jalali(report['expected_session'])} | روز داده: {to_jalali(date)}")
    if date is None or date != report['expected_session']:
        st.warning('فیلترها آرشیوی‌اند؛ داده تازه برای نامزد امروز نداریم. گزارش عمومی جلسه ممکن است منبع و تاریخ دیگری داشته باشد.')
    pulse = report['market']
    if pulse:
        st.caption(f"پوشش روز فیلتر: {pulse['count']}/{pulse['expected_count']}؛ فاقد ردیف: {pulse['missing_count']}؛ "
                   f"بازده نامعلوم: {pulse['return_unknown_count']}؛ جریان حقیقی معتبر: {pulse['flow_coverage']}/{pulse['count']}")
    st.info('هیچ‌یک از این فرضیه‌ها مجوز خرید نیست. اندازه معامله بدون برنامه دستی، بودجه معلوم و حساب تطبیق‌شده ساخته نمی‌شود.')
    with st.expander('فرضیه‌های منطبق و دلیل مسدودبودن اندازه معامله', expanded=False):
        if report['candidates']:
            st.dataframe(pd.DataFrame([{'نماد': r['symbol'], 'فرضیه': r['hypothesis'],
                'علت توقف': '؛ '.join(r['blockers']), 'سقف‌های محاسبه': 'نامعلوم' if r['caps'] is None else
                    '؛ '.join(f'{key}: {"نامعلوم" if value is None else value}' for key, value in r['caps'].items())}
                for r in report['candidates']]), hide_index=True, width='stretch')
        else:
            st.write('در روز داده موجود، هیچ نمادی با همه شروط معلومِ این دو فرضیه منطبق نیست؛ این نتیجه درباره تمام روش‌های ممکن نیست.'
                     if evaluated else 'تطبیق فرضیه‌ها هنوز درخواست نشده است؛ این وضعیت به معنی نبود نامزد نیست.')
        if report['account_risk_blockers']:
            st.caption('کنترل مظنه تازه پوزیشن‌های دفتر: ' + '؛ '.join(report['account_risk_blockers']))
    with st.expander('وضعیت محلی پورتفوی مجازی و حساب فارابی', expanded=False):
        if report['paper_portfolios']:
            st.dataframe(pd.DataFrame([{'دفتر مجازی': p['name'], 'ارزش (تومان)': None if p['equity_rial'] is None else p['equity_rial'] / 10,
                'سود/زیان (تومان)': None if p['pnl_rial'] is None else p['pnl_rial'] / 10,
                'وضعیت': p.get('error') or ('نسخه/ارزش نامعلوم؛ بررسی در صفحه پورتفوی' if p['integrity_unknown'] else 'گزارش دفتر قابل محاسبه'),
                'موقعیت عادی': p.get('open_positions'), 'خارج دامنه/فقط مشاهده': p.get('passive_positions')}
                for p in report['paper_portfolios']]), hide_index=True, width='stretch')
        else:
            st.write('پورتفوی مجازی افتتاح نشده است؛ از صفحه «پورتفوی مجازی» شروع کن.')
        if report['broker']:
            broker = report['broker']
            scope = broker['scope']
            st.write(f"برداشت فارابی: {to_jalali(broker['captured_at'], with_time=True)} | داخل دامنه ۳۰۰/۳۰۳: {scope['in_scope']}؛ "
                     f"خارج دامنه: {scope['outside_scope']}؛ نوع نامعلوم: {scope['unknown_scope']}")
            st.caption(f"موقعیت خرد با وزن حداکثر ۰٫۱٪ از ارزش گزارش‌شده سبد: {scope['micro_positions']}. این برچسب پیشنهاد فروش نیست. "
                       'تازگی قیمت عمومی، تازگی برداشت یا کامل‌بودن تاریخچه حساب را اثبات نمی‌کند.')
        else:
            st.write('گزارش سبد فارابی وارد نشده است.')
