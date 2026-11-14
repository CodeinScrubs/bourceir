"""Daily support and resistance display for a single ordinary equity."""

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from core.price_levels import LOOKBACK, MIN_HISTORY, RANGE_WINDOW, PriceMap, prepare_level_history, price_map_at
from core.screening import expected_completed_session
from core.text import to_fa_digits
from core.timeutils import now_tehran, to_jalali
from database.db import DatabaseManager
from database.symbol_review import observed_price_sessions

LABELS = {'support': 'حمایت', 'resistance': 'مقاومت', 'inside': 'قیمت داخل ناحیه'}
BOUNDARIES = {
    'reference_change': 'قیمت مرجع با مبنای قبلی پیوسته نبود.',
    'long_gap': 'بین داده‌های نماد شکاف طولانی دیده شد.',
    'invalid_previous': 'دادهٔ یکی از جلسات قبلی برای محاسبه معتبر نبود.',
}


@st.cache_data(ttl=60, show_spinner=False)
def load_price_map(database_path, symbol, at):
    with DatabaseManager(db_path=database_path, read_only=True) as db:
        db.conn.execute('BEGIN')
        expected = expected_completed_session(at, calendar=db.get_market_calendar())
        prices = db.get_daily_prices_df(symbol, end_date=expected, include_price_unit=True)
        if prices.empty:
            return PriceMap('no_history'), expected
        last = prices.date.iloc[-1]
        # Bound the query while allowing a full 180-session window even around a long exchange closure.
        start = (date.fromisoformat(last) - timedelta(days=730)).isoformat()
        observed = observed_price_sessions(db, start, last)
        history = prepare_level_history(prices.loc[prices.date.ge(start)], observed)
        return price_map_at(history, expected), expected


def _number(value, decimals=0):
    return to_fa_digits(f'{value:,.{decimals}f}')


def _evidence(zone):
    if zone.same_role_episodes is None:
        return 'در حال تماس'
    if zone.same_role_episodes >= 2:
        return 'واکنش تکرارشده در همین نقش'
    if zone.same_role_episodes == 1:
        return 'یک واکنش در همین نقش'
    return 'فقط سابقهٔ نقش مقابل'


def price_levels_panel(database_path, symbol):
    st.subheader('سطوح بعدی حمایت و مقاومت')
    at = now_tehran().replace(minute=0, second=0, microsecond=0)
    try:
        levels, expected = load_price_map(str(database_path), symbol, at)
    except ValueError as exc:
        st.warning(f'محاسبهٔ سطح متوقف شد. {exc}')
        return
    if levels.date and levels.date < expected:
        st.warning(f'آخرین دادهٔ قابل بررسی نماد {to_jalali(levels.date)} است. '
                   f'جلسهٔ مورد انتظار برنامه {to_jalali(expected)} است. '
                   'سطح‌ها به تاریخ آخرین داده مربوط‌اند.')
    if levels.state == 'no_history':
        st.info('سابقهٔ جلسهٔ کامل برای محاسبهٔ ناحیه در دسترس نیست.')
        return
    if levels.state == 'invalid_latest':
        st.warning(f'قیمت خام جلسهٔ {to_jalali(levels.date)} ناقص یا ناسازگار است و جدول محاسبه نشد.')
        return
    st.caption(f'مبنا آخرین معاملهٔ {to_jalali(levels.date)} با قیمت {_number(levels.anchor_rial)} ریال است. '
               f'بازهٔ محاسبه از {to_jalali(levels.window_start)} و شامل {_number(levels.bars)} مشاهدهٔ روزانه است.')
    if levels.boundary:
        st.caption(BOUNDARIES[levels.boundary])
    if levels.skipped_sessions:
        st.caption(f'در {_number(levels.skipped_sessions)} جلسهٔ مشاهده‌شدهٔ بازار، کندل نماد در این بازه موجود نیست.')
    if levels.state == 'short_history':
        st.info(f'حداقل {_number(MIN_HISTORY)} کندل هم‌مبنا برای تعیین ناحیه لازم است.')
        return
    if levels.state == 'flat_history':
        st.info('دامنهٔ معمول نوسان در پنجرهٔ اخیر صفر است. پهنای قابل‌اتکایی برای ناحیه تعیین نشد.')
        return

    st.caption(f'میانهٔ دامنهٔ واقعی {_number(RANGE_WINDOW)} مشاهدهٔ اخیر '
               f'{_number(levels.typical_range_rial, 1)} ریال است. '
               'فاصله از آخرین قیمت تا نزدیک‌ترین لبهٔ ناحیه اندازه‌گیری می‌شود.')
    st.caption(f'برای مقایسه، کف و سقف خام همین پنجره به‌ترتیب '
               f'{_number(levels.recent_low_rial)} و {_number(levels.recent_high_rial)} ریال‌اند.')
    rows = []
    for zone in levels.zones:
        rows.append({
            'نوع ناحیه': LABELS[zone.side],
            'از (ریال)': zone.lower_rial,
            'تا (ریال)': zone.upper_rial,
            'فاصله تا ناحیه (%)': zone.distance_pct,
            'واکنش‌های جداگانه': zone.episodes,
            'واکنش در همین نقش': zone.same_role_episodes,
            'واکنش با تأیید ۵ مشاهده': zone.broad_episodes,
            'آخرین واکنش': to_jalali(zone.last_reaction),
            'آخرین تأیید': to_jalali(zone.last_confirmation),
            'شواهد': _evidence(zone),
        })
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width='stretch', column_config={
            'از (ریال)': st.column_config.NumberColumn(format='localized'),
            'تا (ریال)': st.column_config.NumberColumn(format='localized'),
            'فاصله تا ناحیه (%)': st.column_config.NumberColumn(format='%.2f'),
        })
    for side in ('support', 'resistance'):
        if not any(zone.side == side for zone in levels.zones):
            st.info(f'در {_number(LOOKBACK)} مشاهدهٔ اخیر یا بخش هم‌مبنای کوتاه‌تر، '
                    f'ناحیه‌ای برای {LABELS[side]} در این سمت قیمت تأیید نشد.')
    st.caption('واکنش‌ها از سقف‌ها و کف‌های تأییدشده به دست می‌آیند. ستون شواهد تعداد واکنش‌های ثبت‌شده را توصیف می‌کند.')
