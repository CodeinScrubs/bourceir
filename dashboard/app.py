# -*- coding: utf-8 -*-
"""
داشبورد فارسی BRS — دستیار نوسان‌گیری بورس و فرابورس

اجرا: فایل START.bat را دوبار کلیک کنید (یا: streamlit run app.py)
همه کارها (به‌روزرسانی داده، پایش زنده، اطلاعیه‌ها) با دکمه انجام می‌شود؛ نیازی به خط فرمان نیست.
"""

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import altair as alt  # noqa: E402
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from config import settings  # noqa: E402
from core import market_rules  # noqa: E402
from core.text import to_fa_digits  # noqa: E402
from core.timeutils import is_market_open, now_tehran, to_jalali  # noqa: E402
from dashboard.broker import page_broker  # noqa: E402
from dashboard.capital_capacity import page_capital_capacity  # noqa: E402
from dashboard.flow_lab import interval_panel, page_flow_lab  # noqa: E402
from dashboard.forward_observations import page_forward_observations  # noqa: E402
from dashboard.market_review import page_market_review  # noqa: E402
from dashboard.operator_status import operator_panel  # noqa: E402
from dashboard.paper import page_paper  # noqa: E402
from dashboard.probability import page_probability  # noqa: E402
from dashboard.query_screener import page_query_screener  # noqa: E402
from dashboard.stock_study import page_stock_study  # noqa: E402
from dashboard.workbench import page_shortlist, page_workbench  # noqa: E402
from data_collector.monitor import DataFreshnessMonitor  # noqa: E402
from database.db import DatabaseManager  # noqa: E402
from features.labels_fa import FEATURE_LABELS_FA, format_feature  # noqa: E402
from features.store import load_manifest, load_symbol_features  # noqa: E402
from ops import jobs  # noqa: E402

st.set_page_config(page_title="BRS — دستیار نوسان‌گیری بورس", page_icon="📈", layout="wide")

# ─── راست‌چین و فونت فارسی ───
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;600;800&display=swap');
    html, body, .stApp, [data-testid="stSidebar"], button, input, textarea, select {
        font-family: 'Vazirmatn', Tahoma, sans-serif !important;
    }
    .stApp, [data-testid="stSidebar"] { direction: rtl; }
    .stApp p, .stApp li, .stApp h1, .stApp h2, .stApp h3, .stApp label, .stApp .stMarkdown { text-align: right; }
    [data-testid="stMetricValue"] { direction: rtl; }
    [data-testid="stDataFrame"], .vega-embed { direction: ltr; }
    .brs-card { padding: .8rem 1rem; border-radius: .6rem; border: 1px solid rgba(128,128,128,.25); margin-bottom: .6rem; }
    .brs-muted { opacity: .7; font-size: .9rem; }
    @media (max-width: 640px) {
        [data-testid="stSidebar"][aria-expanded="true"] {
            position: fixed !important; right: 0 !important; left: auto !important;
            width: 18rem !important; min-width: 18rem !important; max-width: 18rem !important;
            height: 100dvh !important; z-index: 1000002; flex-shrink: 0 !important;
        }
        [data-testid="stSidebar"][aria-expanded="false"] {
            display: none !important;
        }
        [data-testid="stMainBlockContainer"] { padding-left: 1rem; padding-right: 1rem; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ═══════════════════════════════════════════════
# دسترسی به داده
# ═══════════════════════════════════════════════

@st.cache_resource
def ensure_database() -> bool:
    """یک بار در شروع برنامه: ساخت دیتابیس یا اعمال مایگریشن‌های جدید."""
    DatabaseManager().close()
    return True


def ro_db() -> DatabaseManager:
    """اتصال فقط‌خواندنی کوتاه‌مدت؛ هیچ تداخلی با پایش زنده یا به‌روزرسانی هم‌زمان ندارد."""
    return DatabaseManager(read_only=True)


def query_df(sql: str, params: tuple = ()) -> pd.DataFrame:
    with ro_db() as db:
        return pd.read_sql_query(sql, db.conn, params=params)


def fa_num(value, digits: int = 0) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    formatted = to_fa_digits(f"{value:,.{digits}f}")
    return '\u2066' + formatted + '\u2069' if value < 0 else formatted


def billion_toman(rial_value) -> float:
    """تبدیل ریال به میلیارد تومان (فقط برای نمایش؛ ذخیره‌سازی همیشه ریالی است)."""
    return None if rial_value is None else rial_value / 10 / 1e9


@st.cache_data(ttl=60)
def symbol_list() -> list:
    with ro_db() as db:
        return [s["symbol_fa"] for s in db.get_universe_symbols()]


@st.cache_data(ttl=60)
def market_pulse() -> dict:
    """نبض بازار در آخرین روز معاملاتی ثبت‌شده (فقط سهام داخل دامنه پروژه)."""
    from core.market_summary import summarize_daily_market
    from core.timeutils import now_tehran

    with ro_db() as db:
        ids = [s["id"] for s in db.get_universe_symbols()]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    last = query_df(f"SELECT MAX(date) AS d FROM daily_prices WHERE source = 'tsetmc' AND date <= ? "
                    f"AND symbol_id IN ({placeholders});", (now_tehran().date().isoformat(), *ids))["d"].iloc[0]
    if not last:
        return {}
    df = query_df(
        f"""
        SELECT dp.*, ct.buy_i_value, ct.sell_i_value, ct.buy_i_count, ct.sell_i_count,
               ct.buy_i_volume, ct.sell_i_volume, ct.buy_n_volume, ct.sell_n_volume,
               ct.buy_n_value, ct.sell_n_value
        FROM daily_prices dp
        LEFT JOIN client_types_daily ct ON ct.symbol_id = dp.symbol_id AND ct.date = dp.date
        WHERE dp.date = ? AND dp.source = 'tsetmc' AND dp.symbol_id IN ({placeholders});
        """, (last, *ids))
    return dict(date=last, **summarize_daily_market(df, expected_count=len(ids)))


@st.cache_data(ttl=60)
def symbol_daily(symbol: str) -> pd.DataFrame:
    from core.client_quality import validated_client_data

    with ro_db() as db:
        df = db.get_daily_prices_df(symbol)
        ct = db.get_client_types_df(symbol) if not df.empty else pd.DataFrame()
    if df.empty:
        return df
    if not ct.empty:
        clean = validated_client_data(df, ct)
        df = df.merge(ct, on="date", how="left", validate="one_to_one")
    df["change_pct"] = market_rules.pit_daily_returns(df) * 100
    df = pd.concat([df, market_rules.lock_flags(df)], axis=1)
    if "buy_i_value" in df.columns:
        df["real_flow"] = clean["buy_i_value"] - clean["sell_i_value"]
        buy = (clean["buy_i_value"] / clean["buy_i_count"]).where(clean["buy_i_count"] > 0)
        sell = (clean["sell_i_value"] / clean["sell_i_count"]).where((clean["sell_i_count"] > 0) & (clean["sell_i_value"] > 0))
        df["buyer_power"] = (buy / sell).replace([float("inf"), -float("inf")], float("nan"))
        df['client_buy_value_quality'] = clean['buy_value_quality']
        df['client_sell_value_quality'] = clean['sell_value_quality']
    df["jalali"] = df["date"].map(lambda d: to_jalali(d, fa_digits=False))
    return df


@st.cache_data(ttl=20)
def latest_snapshots() -> pd.DataFrame:
    """آخرین اسنپ‌شات هر نماد در آخرین روزی که پایش زنده انجام شده."""
    with ro_db() as db:
        ids = [s["id"] for s in db.get_universe_symbols()]
    if not ids:
        return pd.DataFrame()
    placeholders = ",".join("?" for _ in ids)
    last = query_df(f"SELECT MAX(captured_at) AS m FROM snapshots WHERE symbol_id IN ({placeholders});", tuple(ids))["m"].iloc[0]
    if not last:
        return pd.DataFrame()
    day = last[:10]
    # بازه روی captured_at تا ایندکس استفاده شود ("~" بعد از همه ارقام و حرف T قرار می‌گیرد)
    return query_df(
        f"""
        SELECT s.symbol_fa, s.name_fa, sn.*
        FROM snapshots sn JOIN symbols s ON s.id = sn.symbol_id
        WHERE sn.id IN (SELECT MAX(id) FROM snapshots WHERE captured_at >= ? AND captured_at < ? AND symbol_id IN ({placeholders}) GROUP BY symbol_id);
        """, (day, day + "~", *ids))


# ═══════════════════════════════════════════════
# اجزای مشترک
# ═══════════════════════════════════════════════

JOB_LABELS = {"update": "به‌روزرسانی داده‌ها", "live": "پایش زنده بازار", "codal": "اطلاعیه‌های کدال", "research": "پژوهش استراتژی‌های پایه",
              'market-review': 'بررسی مستند بازار امروز',
              "research-dip": "آزمون روش خرید در اصلاح", 'research-portfolio': 'پژوهش پرتفوی با نقد مشترک',
              'research-flow': 'پژوهش فیلترهای ثابت جریان حقیقی', 'research-preflight': 'بررسی قرارداد داده پژوهش'}


def job_panel(name: str, args: list, start_label: str, help_text: str, stoppable: bool = False) -> None:
    status = jobs.job_status(name)
    cols = st.columns([2, 1])
    with cols[0]:
        if status["running"]:
            st.info(f"⏳ «{JOB_LABELS.get(name, name)}» در حال اجراست...")
        elif status["started_at"]:
            st.caption(f"آخرین اجرا: {to_jalali(status['started_at'], with_time=True)}")
        st.caption(help_text)
    with cols[1]:
        if status["running"]:
            if stoppable and st.button("⏹ توقف", key=f"stop-{name}", width="stretch"):
                jobs.stop_job(name)
                st.rerun()
        elif st.button(start_label, key=f"start-{name}", type="primary", width="stretch"):
            jobs.start_job(name, args)
            st.rerun()
    if status["log_tail"]:
        with st.expander("📜 گزارش اجرا", expanded=status["running"]):
            st.code("\n".join(status["log_tail"]), language=None)


# ═══════════════════════════════════════════════
# صفحه‌ها
# ═══════════════════════════════════════════════

def page_home() -> None:
    st.title("📈 دستیار نوسان‌گیری BRS")
    now = now_tehran()
    with ro_db() as db:
        open_now = is_market_open(now) and db.is_trading_day(now.date().isoformat())
    st.markdown(
        f"<div class='brs-card'>🕐 امروز {to_jalali(now, with_time=True)} — بازار "
        f"<b>{'باز است 🟢' if open_now else 'بسته است 🔴'}</b> (شنبه تا چهارشنبه، ۹:۰۰ تا ۱۲:۳۰)</div>",
        unsafe_allow_html=True,
    )

    operator_panel()
    pulse = market_pulse()
    if pulse:
        st.subheader(f"نبض بازار — {to_jalali(pulse['date'])}")
        st.caption(f"پوشش {fa_num(pulse['count'])} از {fa_num(pulse['expected_count'])} سهم جامعه جاری — مبالغ به میلیارد تومان")
        if pulse['date'] != now.date().isoformat():
            st.warning('این نبض مربوط به آخرین جلسه ثبت‌شده است و گزارش بازار امروز نیست. '
                       'برای دریافت مستقل جلسه جاری، صفحه «بررسی بازار امروز» را باز کنید.')
        if pulse['missing_count'] or pulse['return_unknown_count']:
            st.warning(f"{fa_num(pulse['missing_count'])} سهم فاقد ردیف این جلسه و "
                       f"{fa_num(pulse['return_unknown_count'])} ردیف فاقد بازده معتبر است؛ در شمار مثبت و منفی وارد نشده‌اند.")
        row1 = st.columns(2)
        row1[0].metric("📈 مثبت / 📉 منفی", f"\u2066{fa_num(pulse['up'])} / {fa_num(pulse['down'])}\u2069")
        row1[1].metric("معاملات تک‌قیمت صعودی / نزولی", f"\u2066{fa_num(pulse['locked_up'])} / {fa_num(pulse['locked_down'])}\u2069",
                       help="قرینه کندلی است؛ بدون مظنه و دامنه معتبر، وجود صف یا امکان اجرای سفارش را اثبات نمی‌کند")
        row2 = st.columns(2)
        row2[0].metric("💵 ارزش معاملات", fa_num(billion_toman(pulse["value"])))
        if pulse['value_coverage'] < pulse['count']:
            st.caption(f"ارزش معتبر برای {fa_num(pulse['value_coverage'])} از {fa_num(pulse['count'])} ردیف موجود است؛ جمع ناقص است.")
        if pulse["real_flow"] is not None:
            partial = pulse["flow_coverage"] < pulse["count"]
            row2[1].metric("👥 ورود پول حقیقی" + (" (ناقص)" if partial else ""), fa_num(billion_toman(pulse["real_flow"])),
                           help="خرید حقیقی منهای فروش حقیقی؛ منفی یعنی خروج پول حقیقی")
            if partial:
                st.caption(f"⏳ آمار معتبر حقیقی/حقوقی جلسه {to_jalali(pulse['date'])} برای "
                           f"{fa_num(pulse['flow_coverage'])} از {fa_num(pulse['count'])} سهم موجود است؛ "
                           "نماد فاقد داده یا دارای آمار ناسازگار در جمع وارد نشده است. صفحه سلامت را بررسی کنید.")
    else:
        st.warning("هنوز داده‌ای ثبت نشده است. دکمه «به‌روزرسانی داده‌ها» را بزنید.")

    st.divider()
    st.subheader("🔄 به‌روزرسانی داده‌ها")
    job_panel("update", ["update"], "شروع به‌روزرسانی",
              "فهرست سهام، قیمت‌های روزانه، حقیقی/حقوقی و اطلاعیه‌های کدال را به‌روز می‌کند. "
              "مدت دریافت به حجم داده و دسترسی منبع بستگی دارد. پس از بسته شدن بازار به‌روزرسانی کنید؛ "
              "زمان انتشار یا اصلاح آمار نمادها یکسان نیست. تاریخ و پوشش معتبر را در صفحه سلامت بررسی کنید؛ "
              "اجرای دوباره، اصلاحات اخیر منبع را نیز بررسی می‌کند.")

    st.subheader("📡 پایش زنده بازار")
    job_panel("live", ["live"], "شروع پایش زنده",
              "در ساعات بازار هر ۳۰ ثانیه وضعیت همه سهام (قیمت، صف‌ها، ۵ مظنه، حقیقی/حقوقی) را ثبت می‌کند. "
              "خارج از ساعات بازار منتظر می‌ماند.", stoppable=True)

    st.divider()
    be = market_rules.breakeven_move() * 100
    st.markdown(
        f"<div class='brs-card'>💰 کارمزد خرید {fa_num(settings.FEES['buy'] * 100, 4)}٪ و فروش "
        f"{fa_num(settings.FEES['sell'] * 100, 2)}٪ — هر معامله برای سر به سر شدن حداقل "
        f"<b>{fa_num(be, 2)}٪</b> رشد قیمت لازم دارد.</div>",
        unsafe_allow_html=True,
    )


def page_symbol() -> None:
    st.title("📊 بررسی نماد")
    symbols = symbol_list()
    if not symbols:
        st.warning("هنوز نمادی ثبت نشده است.")
        return
    default = symbols.index("فولاد") if "فولاد" in symbols else 0
    c = st.columns([2, 2])
    symbol = c[0].selectbox("نماد را انتخاب یا جستجو کنید:", symbols, index=default)
    period = c[1].radio("بازه نمودار", ["۳ ماه", "۶ ماه", "۱ سال", "۳ سال"], index=1, horizontal=True)
    df = symbol_daily(symbol)
    if df.empty:
        st.info("برای این نماد هنوز قیمتی ثبت نشده است. به‌روزرسانی داده‌ها را اجرا کنید.")
        return
    days = {"۳ ماه": 63, "۶ ماه": 126, "۱ سال": 240, "۳ سال": 720}[period]
    view = df.tail(days)
    last = view.iloc[-1]

    st.caption(f"آخرین روز معاملاتی: {to_jalali(last['date'])} — مبالغ به میلیارد تومان")
    m = st.columns(2)
    m[0].metric("قیمت پایانی (ریال)", fa_num(last["close_final_raw"]), f"{last['change_pct']:+.2f}%")
    m[1].metric("ارزش معاملات", fa_num(billion_toman(last["value"]), 1))
    m = st.columns(2)
    if "real_flow" in view.columns and pd.notna(last.get("real_flow")):
        m[0].metric("ورود پول حقیقی", fa_num(billion_toman(last["real_flow"]), 1))
    if "buyer_power" in view.columns and pd.notna(last.get("buyer_power")):
        m[1].metric("قدرت خریدار حقیقی", fa_num(last["buyer_power"], 2),
                    help="سرانه خرید حقیقی ÷ سرانه فروش حقیقی؛ بزرگ‌تر از ۱ یعنی خریداران درشت‌ترند")
    elif 'client_buy_value_quality' in view.columns:
        st.caption('قدرت خریدار نامعلوم است: داده موجود نیست، تقسیم تعریف نشده یا آمار با حجم، ارزش '
                   'و بازه قیمت روز سازگار نیست. داده خام حفظ شده است.')
    if last["locked_up"]:
        st.info("آخرین روز: الگوی تک‌قیمتی صعودی — تقریب روزانه صف خرید")
    elif last["locked_down"]:
        st.info("آخرین روز: الگوی تک‌قیمتی نزولی — تقریب روزانه صف فروش")
    st.caption('پرچم روزانه از تک‌قیمتی بودن معاملات نسبت به مرجع به دست می‌آید؛ سقف/کف مجاز تاریخی '
               'و وضعیت صف پایان جلسه را اثبات نمی‌کند. نبود پرچم نیز اثبات امکان خروج نیست.')

    chart_mode = st.radio('نوع قیمت نمودار', ['تعدیل نمایشی بر مبنای مرجع', 'قیمت خام روز'], horizontal=True)
    # تعدیل روی کل تاریخچه، پیش از برش بازه نمودار؛ مصرف آن در فیچر ممنوع است.
    view = view.copy()
    view['chart_close'] = (market_rules.backward_adjusted_close(df).loc[view.index]
                          if chart_mode == 'تعدیل نمایشی بر مبنای مرجع' else view['close_final_raw'])
    if chart_mode == 'تعدیل نمایشی بر مبنای مرجع':
        st.caption('تعدیل رو به عقب فقط برای نمایش پیوستگی قیمت است؛ با مرجع‌های جدید تغییر می‌کند '
                   'و بازده واقعی حساب، وصول سود یا قابل فروش بودن سهام جایزه را ثابت نمی‌کند.')
    base = alt.Chart(view).encode(x=alt.X("jalali:O", title=None, sort=None,
                                          axis=alt.Axis(labelOverlap=True, labelAngle=0)))
    price = base.mark_line(strokeWidth=2).encode(
        y=alt.Y("chart_close:Q", title="قیمت نمودار (ریال)", scale=alt.Scale(zero=False)),
        tooltip=[alt.Tooltip("jalali:O", title="تاریخ"), alt.Tooltip("chart_close:Q", title="قیمت نمودار", format=",.0f"),
                 alt.Tooltip("change_pct:Q", title="تغییر ٪", format="+.2f")],
    )
    st.altair_chart(price.properties(height=320), width="stretch")
    if "real_flow" in view.columns:
        flow = base.mark_bar().encode(
            y=alt.Y("real_flow_bt:Q", title="ورود پول حقیقی (میلیارد تومان)"),
            color=alt.condition("datum.real_flow_bt > 0", alt.value("#2e7d32"), alt.value("#c62828")),
            tooltip=[alt.Tooltip("jalali:O", title="تاریخ"), alt.Tooltip("real_flow_bt:Q", title="میلیارد تومان", format=",.1f")],
        ).transform_calculate(real_flow_bt="datum.real_flow / 10000000000")
        st.altair_chart(flow.properties(height=160), width="stretch")

    table = view.tail(20).iloc[::-1]
    out = pd.DataFrame({
        "تاریخ": table["jalali"],
        "پایانی": table["close_final_raw"],
        "تغییر ٪": table["change_pct"].round(2),
        "کمترین": table["low_raw"],
        "بیشترین": table["high_raw"],
        "حجم": table["volume"],
        "الگوی روزانه": table.apply(lambda r: "تک‌قیمتی صعودی؛ تقریب صف" if r["locked_up"] else (
            "تک‌قیمتی نزولی؛ تقریب صف" if r["locked_down"] else ""), axis=1),
    })
    if "real_flow" in table.columns:
        out["پول حقیقی (م.ت)"] = (table["real_flow"] / 1e10).round(1)
    st.dataframe(out, hide_index=True, width="stretch")
    from dashboard.symbol_review import symbol_review_panel
    symbol_review_panel(settings.DB_PATH, symbol)
    feature_panel(symbol)


@st.cache_data(ttl=300)
def cached_symbol_features(symbol: str, dataset_sha: str = "") -> pd.DataFrame:
    return load_symbol_features(symbol)


def feature_panel(symbol: str) -> None:
    """آخرین مقدار فیچرهای نماد از انبار فیچر (همان ورودی‌هایی که مدل‌ها می‌بینند)."""
    manifest = load_manifest()
    with st.expander("📐 فیچرهای آخرین روز (ورودی مدل‌های هوش مصنوعی)"):
        if not manifest:
            st.info("انبار فیچر هنوز ساخته نشده است. «به‌روزرسانی داده‌ها» آن را در پایان می‌سازد.")
            return
        feats = cached_symbol_features(symbol, (manifest.get("provenance") or {}).get("parquet_sha256", ""))
        if feats.empty:
            st.info("برای این نماد فیچری ثبت نشده است.")
            return
        last = feats.iloc[-1]
        st.caption(f"نسخه {manifest['version']} — تاریخ {to_jalali(last['date'])} — ساخته‌شده در "
                   f"{to_jalali(manifest['created_at'], with_time=True)}")
        st.dataframe(pd.DataFrame([
            {"فیچر": label, "مقدار": format_feature(name, last.get(name)), "توضیح": help_text}
            for name, (label, help_text, _) in FEATURE_LABELS_FA.items()
        ]), hide_index=True, width="stretch", height=420)


def page_live() -> None:
    st.title("📡 دیده‌بان زنده")
    status = jobs.job_status("live")
    if not status["running"]:
        st.info("پایش زنده خاموش است. از صفحه «خانه» آن را روشن کنید. (جدول زیر آخرین وضعیت ثبت‌شده را نشان می‌دهد)")

    @st.fragment(run_every=30 if status["running"] else None)
    def live_table() -> None:
        snaps = latest_snapshots()
        if snaps.empty:
            st.warning("هنوز اسنپ‌شاتی ثبت نشده است.")
            return
        snaps = snaps.copy()
        snaps["change_pct"] = (snaps["last_price"] / snaps["ref_price"].where(snaps["ref_price"] > 0) - 1) * 100
        monitor = DataFreshnessMonitor()
        health = [monitor.inspect_snapshot(row["symbol_fa"], row) for row in snaps.to_dict("records")]
        snaps["buyer_power"] = pd.to_numeric(pd.Series([h['buyer_power'] for h in health], index=snaps.index), errors='coerce')
        snaps["real_flow_bt"] = pd.to_numeric(pd.Series([h['real_flow_estimate_rial'] for h in health], index=snaps.index), errors='coerce') / 1e10
        snaps["client_volume_quality"] = [h['buy_volume_quality_label'] + ' / ' + h['sell_volume_quality_label'] for h in health]
        snaps["freshness"] = [h["status"] for h in health]
        snaps["source_delay"] = [h["delay_seconds"] for h in health]
        view_filter = st.radio("نمایش", ["همه", "فقط صف خرید", "فقط صف فروش", "قدرت خریدار > ۲"], horizontal=True)
        if view_filter == "فقط صف خرید":
            snaps = snaps[snaps["buy_queue_volume"] > 0]
        elif view_filter == "فقط صف فروش":
            snaps = snaps[snaps["sell_queue_volume"] > 0]
        elif view_filter == "قدرت خریدار > ۲":
            snaps = snaps[snaps["buyer_power"] > 2]
        if snaps.empty:
            st.info("هیچ نمادی با این فیلتر وجود ندارد.")
            return
        snaps = snaps.sort_values("value", ascending=False)
        st.caption(f"آخرین ثبت: {to_jalali(snaps['captured_at'].max(), with_time=True)} — {to_fa_digits(len(snaps))} نماد")
        st.dataframe(pd.DataFrame({
            "نماد": snaps["symbol_fa"],
            "تازگی منبع": snaps["freshness"],
            "تأخیر منبع (ثانیه)": snaps["source_delay"],
            "آخرین": snaps["last_price"],
            "پایانی": snaps["close_price"],
            "تغییر آخرین ٪": snaps["change_pct"].round(2),
            "صف خرید (حجم)": snaps["buy_queue_volume"],
            "صف فروش (حجم)": snaps["sell_queue_volume"],
            "قدرت خریدار": snaps["buyer_power"].round(2),
            "کیفیت حجم خرید / فروش": snaps["client_volume_quality"],
            "برآورد پول حقیقی (میلیارد تومان)": snaps["real_flow_bt"].round(1),
            "ارزش (میلیارد تومان)": (snaps["value"] / 1e10).round(1),
        }), hide_index=True, width="stretch", height=600)
        st.caption('قدرت خریدار این جدول حجمی است. برآورد پول حقیقی از تفاضل حجم حقیقی × آخرین قیمت است؛ '
                   'ارزش واقعی معاملات حقیقی را فقط با آمار ارزشی روزانه می‌توان تطبیق داد. '
                   'هم‌زمانی مستقل آمار حقیقی/حقوقی با عمق سفارش‌ها تأیید نشده است.')
        interval_panel(snaps['symbol_fa'].tolist())

    live_table()


def page_codal() -> None:
    st.title("📰 اطلاعیه‌های کدال")
    from dashboard.codal_documents import document_archive_panel

    document_archive_panel(job_panel)
    job_panel("codal", ["codal", "--pages", "10", "--judge"], "دریافت اطلاعیه‌های جدید",
              "جدیدترین اطلاعیه‌های سهام بورس و فرابورس را از کدال می‌گیرد و با هوش مصنوعی Jev "
              "نوع و اثر احتمالی آن‌ها را ارزیابی می‌کند (ارزیابی هوش مصنوعی توصیه خرید/فروش نیست).")
    df = query_df(
        """
        SELECT a.published_at, a.symbol_fa, a.title, a.url, j.category, j.impact_score, j.is_material
        FROM announcements a
        LEFT JOIN announcement_judgments j ON j.id = (
            SELECT MAX(id) FROM announcement_judgments WHERE announcement_id = a.id)
        ORDER BY a.published_at DESC LIMIT 200;
        """)
    if df.empty:
        st.info("هنوز اطلاعیه‌ای ثبت نشده است.")
        return
    labels = {"monthly_activity": "گزارش ماهانه", "financial_statements": "صورت مالی", "capital_increase": "افزایش سرمایه",
              "agm_dividend": "مجمع/سود", "trading_halt": "توقف/بازگشایی", "material_news": "اطلاعات بااهمیت",
              "clarification": "توضیح/شایعه", "other": "سایر"}
    impact_labels = ["به‌شدت منفی", "منفی", "خنثی", "مثبت", "به‌شدت مثبت"]
    st.dataframe(pd.DataFrame({
        "زمان انتشار": df["published_at"].map(lambda v: to_jalali(v, with_time=True, fa_digits=False)),
        "نماد": df["symbol_fa"],
        "عنوان": df["title"],
        "نوع (AI)": df["category"].map(labels),
        "اثر احتمالی (AI)": df["impact_score"].map(
            lambda s: impact_labels[min(4, max(0, int(round(s))))] if pd.notna(s) else ""),
        "لینک": df["url"],
    }), hide_index=True, width="stretch", height=600,
        column_config={"لینک": st.column_config.LinkColumn("لینک", display_text="مشاهده")})


def latest_research_report(pointer: str = "latest.json") -> str:
    """متن گزارش آخرین پژوهش (data/research)، یا رشته خالی."""
    import json
    import re

    folder = settings.DATA_DIR / "research"
    try:
        experiment_id = json.loads((folder / pointer).read_text(encoding="utf-8"))["experiment_id"]
        payload = json.loads((folder / f'{experiment_id}.json').read_text(encoding='utf-8'))
        report = (folder / f"{experiment_id}.md").read_text(encoding="utf-8")
        params = payload.get('params') or {}
        from research.dip import DIP_VERSION, TIME_EXIT_PRICE_BASIS
        from research.dip_run import DIP_SUMMARY_VERSION
        if pointer == 'latest-flow.json':
            from backtest.portfolio import PORTFOLIO_VERSION
            from core.flow_filters import FLOW_FILTER_VERSION
            from research.flow_run import FLOW_RESEARCH_VERSION
            protocol = payload.get('protocol') or {}
            current = (protocol.get('version') == FLOW_RESEARCH_VERSION
                       and protocol.get('filter_version') == FLOW_FILTER_VERSION
                       and protocol.get('portfolio_version') == PORTFOLIO_VERSION)
            if current:
                capital = protocol['config']['initial_capital_rial']
                report += (f'\n\nسرمایه آغاز هر برش: {to_fa_digits(f"{capital:,.0f}")} ریال '
                           f'({to_fa_digits(f"{capital/10:,.0f}")} تومان)، تماماً نقد فرضی. '
                           'این آزمون سبد فعلی کارگزاری را بازسازی نمی‌کند و سود حساب واقعی نیست.\n')
                names = {'retail_acceleration': 'تقویت سرانه', 'volume_breakout': 'شکست سقف', 'daily_pressure': 'فشار خرید'}
                def mode_label(mode):
                    if mode.startswith('past_baseline'):
                        return 'پایه منتخب گذشته' + (' / فشار اجرا' if mode.endswith(('_stress', '-stress')) else '')
                    for key, label in names.items():
                        if mode.startswith(key+'-'):
                            rest = mode[len(key)+1:].replace('d', ' جلسه', 1)
                            return to_fa_digits(label+' / '+rest.replace('-stress', ' / فشار اجرا').replace('-random', ' / کنترل تصادفی'))
                    return mode
                details = ['\n\n| حالت | گذر از رویداد مصنوعی | برش با پوزیشن باز | روز با علامت قدیمی |',
                           '| --- | ---: | ---: | ---: |']
                for mode in payload['summary']:
                    values = [row['modes'][mode]['metrics'] for row in payload['results']]
                    details.append(f"| {mode} | {sum(v['corporate_action_events'] for v in values)} | "
                                   f"{sum(v['open_positions'] > 0 for v in values)} | {sum(v['stale_mark_days'] for v in values)} |")
                report += '\n'.join(details)
                for mode in sorted(payload['summary'], key=len, reverse=True):
                    report = report.replace(f'| {mode} |', f'| {mode_label(mode)} |')
                # Keep the sign and percent with the number inside Persian table cells.
                report = re.sub(r'[+-]?\d+(?:\.\d+)?%', lambda m: '\u2066'+m.group(0)+'\u2069', report)
        elif pointer == 'latest-dip.json':
            current = (params.get('version') == DIP_VERSION and params.get('time_exit_price_basis') == TIME_EXIT_PRICE_BASIS
                       and params.get('summary_version') == DIP_SUMMARY_VERSION)
        elif pointer in {'latest-portfolio.json', 'latest-portfolio-buy_queue.json', 'latest-portfolio-buyer_power.json'}:
            from backtest.portfolio import PORTFOLIO_VERSION
            protocol = payload.get('protocol') or {}
            if pointer != 'latest-portfolio.json':
                selector = pointer[len('latest-portfolio-'):-len('.json')]
                if (protocol.get('dip') or {}).get('selector') != selector:
                    return '> گزارش با فرضیه انتخابی سازگار نیست؛ نتیجه آن نمایش داده نمی‌شود. پژوهش همین فرضیه را دوباره اجرا کنید.'
            current = protocol.get('portfolio_version') == PORTFOLIO_VERSION and (protocol.get('dip') or {}).get('time_exit_price_basis') == TIME_EXIT_PRICE_BASIS
        else:
            from features.daily import FEATURE_SET_VERSION
            from research.labels import LABEL_SET_VERSION
            current = (params.get('raw_feature_inputs_verified') is True
                       and (params.get('features') or {}).get('version') == FEATURE_SET_VERSION
                       and (params.get('labels') or {}).get('version') == LABEL_SET_VERSION)
        if not current:
            return '> ⚠️ گزارش قدیمی: با قرارداد فعلی داده/اجرا سازگار نیست؛ برای ارزیابی تازه پژوهش را دوباره اجرا کنید. اعداد زیر فقط سابقه آرشیوی‌اند.\n\n' + report
        return report
    except (OSError, ValueError, KeyError):
        return ""


def page_research() -> None:
    st.title("🔬 پژوهش: آیا این روش‌ها بعد از کارمزد سود می‌دهند؟")
    st.info("روش‌های پایه و خرید در اصلاح، بازده گروه تصمیم‌ها را می‌سنجند. زبانه سرمایه مشترک، نقدینگی و "
            "پوزیشن‌های هم‌زمان را در یک اجرای روزانه پژوهشی لحاظ می‌کند؛ پر شدن سفارش واقعی هنوز تأیید نشده است.")
    job_panel("research", ["research"], "اجرای دوباره پژوهش",
              "برچسب‌ها و ارزیابی همه روش‌ها را با آخرین داده دوباره می‌سازد (چند دقیقه). دوره بیرون از نمونه "
              "(از ۱۴۰۴) عمداً بسته می‌ماند تا فقط برای آزمون نهایی مدل استفاده شود.")
    dip_tab, base_tab, portfolio_tab, flow_tab = st.tabs(["روش شما: خرید در اصلاح", "روش‌های پایه (خرید فردا، فروش چند روز بعد)",
                                               "سرمایه مشترک و ارزیابی زمانی", 'فیلترهای جریان حقیقی'])
    with dip_tab:
        job_panel("research-dip", ["research-dip"], "اجرای دوباره آزمون خرید در اصلاح",
                  "نامزدهای پایان روز ← فردا خرید فقط اگر سهم ۱ تا ۳٪ زیر قیمت مرجع آمد ← فروش با هدف ۲ تا ۴٪ سود "
                  "تا فردا یا پس‌فردا (چند دقیقه).")
        dip_report = latest_research_report("latest-dip.json")
        st.markdown(dip_report) if dip_report else st.info("هنوز اجرا نشده است. دکمه بالا را بزنید.")
    with base_tab:
        report = latest_research_report()
        st.markdown(report) if report else st.info("هنوز پژوهشی اجرا نشده است. دکمه بالای صفحه را بزنید.")
    with portfolio_tab:
        selectors = {'صف خرید قفل در روز تصمیم': 'buy_queue', 'قدرت خریدار': 'buyer_power'}
        selector = st.selectbox('فرضیه انتخاب نامزدهای خرید در اصلاح', list(selectors))
        st.caption('فرضیه صف خریدِ گزارش قدیمی با قدرت خریدار متفاوت است؛ نتیجه هر اجرا نام روش را مشخص می‌کند.')
        job_panel("research-portfolio", ["research-portfolio", '--dip-selector', selectors[selector]], "اجرای پژوهش سرمایه مشترک",
                  "از ورودی محفوظ پژوهش پایه استفاده می‌کند؛ انتخاب روش فقط با گذشته و آزمون در برش بعد. "
                  "قاعده ثابت اصلاح ۲٪ با هدف ۳٪ هم با نقد مشترک و فشار اجرا مقایسه می‌شود. دوره نهایی بسته می‌ماند.")
        report = latest_research_report(f"latest-portfolio-{selectors[selector]}.json")
        st.markdown(report) if report else st.info("برای این فرضیه گزارشی ثبت نشده است؛ ابتدا پژوهش پایه و سپس پژوهش همین فرضیه را اجرا کنید.")
    with flow_tab:
        default_capital = to_fa_digits(f"{settings.BACKTEST_CONFIG['initial_capital']/10:,.0f}")
        job_panel('research-flow', ['research-flow'], 'آزمون فیلترهای ثابت روزانه',
                  'سه تعریف از پیش ثابت، افق ۲ و ۳ جلسه، نقد مشترک، کارمزد، صف و فشار اجرا. '
                  f'سرمایه فرضی پیش‌فرض از تنظیمات بکتست: {default_capital} تومان. '
                  'این آزمون الگوی درون‌روزی یا فیلتر ۹ شرطی ناقص را تأیید نمی‌کند؛ دوره نهایی بسته می‌ماند.')
        report = latest_research_report('latest-flow.json')
        st.markdown(report) if report else st.info('هنوز پژوهش فیلترهای جریان حقیقی اجرا نشده است.')


def page_health() -> None:
    st.title("🩺 سلامت سامانه")
    from dashboard.reliability import show_reliability

    show_reliability(job_panel)
    with st.expander('شواهد مشخصات تاریخی نمادها'):
        from dashboard.instrument_history import show_instrument_history
        show_instrument_history()
    with st.expander('کیفیت ثبت داده درون‌روزی'):
        st.caption('فاصله اسنپ‌شات‌ها، فاصله تغییرات ذخیره‌شده است؛ پاسخ بدون تغییر دوباره ذخیره نمی‌شود. '
                   'ثبت مستقل چرخه‌ها کیفیت دریافت را جدا نشان می‌دهد. زمان مستقل عمق بازار، وضعیت مجاز معاملات '
                   'و پرشدن سفارش خودمان هنوز تأیید نشده‌اند.')
        if st.button('بررسی کیفیت آخرین جلسه ثبت‌شده'):
            from ops.intraday_audit import audit_intraday
            with ro_db() as db:
                quality = audit_intraday(db)
            if quality['rows']:
                st.write(f"روز: {to_jalali(quality['session_date'])} | {quality['rows']:,} اسنپ‌شات | {quality['symbols']} نماد")
                st.write(f"زمان قیمت نامعلوم: {quality['timestamp_unknown_rows']:,} | قیمت قدیمی هنگام دریافت: {quality['provider_stale_rows']:,}")
                snapshots = pd.DataFrame(quality['per_symbol'])
                for field in ('observed_start', 'observed_end'):
                    snapshots[field] = snapshots[field].map(lambda value: to_jalali(value, with_time=True) if value else 'نامعلوم')
                for field in ('gap_p95_seconds', 'max_gap_seconds', 'provider_age_p95_seconds'):
                    snapshots[field] = snapshots[field].map(lambda value: 'نامعلوم' if pd.isna(value) else f'{value:,.1f}')
                st.dataframe(snapshots.rename(columns={'symbol': 'نماد', 'rows': 'تغییر ذخیره‌شده',
                    'observed_start': 'شروع مشاهده', 'observed_end': 'پایان مشاهده',
                    'gap_p95_seconds': 'صدک ۹۵ فاصله تغییر (ثانیه)', 'max_gap_seconds': 'بیشترین فاصله تغییر (ثانیه)',
                    'gaps_over_45_seconds': 'فاصله تغییر بیش از ۴۵ ثانیه',
                    'provider_age_p95_seconds': 'صدک ۹۵ سن زمان قیمت (ثانیه)',
                    'observable_ask_rows': 'مظنه واجد کنترل عددی'}), hide_index=True)
            else:
                st.info('هنوز اسنپ‌شات واقعی برای بررسی وجود ندارد.')
            capture = quality['capture']
            if capture['protocols']:
                st.write(f"ثبت مستقل دریافت: {capture['attempted_polls']:,} شروع | {capture['completed_polls']:,} نتیجه | "
                         f"{capture['pending_polls']:,} شروع بدون نتیجه | {capture['complete_scope_polls']:,} پاسخ با همه نمادهای هدف")
                st.write(f"پیوند محتوای مظنه: {capture['bound_snapshot_receipts']:,} رسید تطبیق‌یافته | "
                         f"{capture['unbound_snapshot_receipts']:,} پاسخ فاقد پیوند معتبر")
                st.caption('شروع بدون نتیجه می‌تواند دریافتِ در حال اجرا یا قطع برنامه باشد؛ نبود ثبت پایان، ادامه پایش را ثابت نمی‌کند. '
                           'پوشش کل جلسه و اعتبار تصمیم معاملاتی همچنان تأیید نشده‌اند.')
                stop_labels = {'cycles_completed': 'چرخه‌های تعیین‌شده تمام شدند',
                               'user_interrupted': 'توقف دستی', 'error': 'خطا', None: 'پایان ثبت نشده'}
                runs = [{'شروع پایش': to_jalali(run['started_at'], with_time=True),
                         'نمادهای هدف': run['expected_instruments'], 'شروع دریافت': run['attempted_polls'],
                         'نتیجه نامعلوم': run['pending_polls'], 'پاسخ معتبر همه نمادها': run['complete_scope_polls'],
                         'بیشترین فاصله شروع دریافت (ثانیه)': ('نامعلوم' if run['max_poll_start_gap_seconds'] is None
                                                               else f"{run['max_poll_start_gap_seconds']:,.1f}"),
                         'اجرای اجباری': 'بله' if run['forced'] else 'خیر', 'پایان': stop_labels[run['stop_reason']]}
                        for run in capture['runs']]
                st.dataframe(pd.DataFrame(runs), hide_index=True)
                for run in capture['runs']:
                    with st.expander(f"کیفیت دریافت نمادها — {run['id']}"):
                        fields = ('symbol', 'valid', 'unchanged', 'missing', 'duplicate', 'rejected',
                                  'bound_snapshot_receipts', 'snapshot_binding_missing', 'snapshot_binding_conflict',
                                  'snapshot_binding_clock_invalid',
                                  'outcome_unknown', 'price_time_fresh', 'price_time_stale', 'price_time_future',
                                  'price_time_unknown', 'max_valid_poll_gap_seconds')
                        instruments = pd.DataFrame(run['per_symbol'])[list(fields)]
                        instruments['max_valid_poll_gap_seconds'] = instruments['max_valid_poll_gap_seconds'].map(
                            lambda value: 'نامعلوم' if pd.isna(value) else f'{value:,.1f}')
                        st.dataframe(instruments.rename(columns={
                            'symbol': 'نماد', 'valid': 'پاسخ معتبر', 'unchanged': 'بدون تغییر', 'missing': 'غایب',
                            'duplicate': 'شناسه تکراری', 'rejected': 'رد شده', 'outcome_unknown': 'نتیجه نامعلوم',
                            'bound_snapshot_receipts': 'رسید متصل به مظنه', 'snapshot_binding_missing': 'پیوند موجود نیست',
                            'snapshot_binding_conflict': 'محتوای ناسازگار', 'snapshot_binding_clock_invalid': 'ساعت ناسازگار',
                            'price_time_fresh': 'زمان قیمت تازه', 'price_time_stale': 'زمان قیمت قدیمی',
                            'price_time_future': 'زمان قیمت در آینده',
                            'price_time_unknown': 'زمان قیمت نامعلوم',
                            'max_valid_poll_gap_seconds': 'بیشترین فاصله دریافت معتبر (ثانیه)'}), hide_index=True)
            else:
                st.info('ثبت مستقل چرخه برای این روز موجود نیست؛ فاصله اسنپ‌شات‌های قدیمی، قطع دریافت را ثابت نمی‌کند.')
    if st.button("اجرای عیب‌یابی کامل (اینترنت، پروکسی، دیتابیس، هوش مصنوعی)", type="primary"):
        from ops.doctor import run_doctor

        with st.spinner("در حال بررسی..."):
            report = run_doctor()
        for check in report.checks:
            box = {"ok": st.success, "warn": st.warning, "error": st.error}[check.status]
            box(f"{check.icon} **{check.title}**: {check.detail}" + (f"\n\n💡 {check.hint}" if check.hint and check.status != "ok" else ""))
    st.subheader("آمار پایگاه داده")
    with ro_db() as db:
        counts = db.table_counts()
    st.dataframe(pd.DataFrame({"جدول": list(counts), "تعداد رکورد": list(counts.values())}), hide_index=True)
    st.subheader("آخرین گردآوری‌ها")
    logs = query_df("SELECT started_at, collection_type, symbols_succeeded, symbols_failed, status FROM collection_log "
                    "ORDER BY id DESC LIMIT 15;")
    if not logs.empty:
        logs["started_at"] = logs["started_at"].map(lambda v: to_jalali(v, with_time=True, fa_digits=False))
        logs.columns = ["زمان", "نوع", "موفق", "ناموفق", "وضعیت"]
        st.dataframe(logs, hide_index=True, width="stretch")


def page_guide() -> None:
    st.title("ℹ️ راهنما")
    st.markdown(
        """
**این برنامه چه می‌کند؟**
داده‌های رسمی بازار (TSETMC و کدال) را برای همه سهام بورس و فرابورس جمع‌آوری می‌کند تا بتوانیم
استراتژی‌های نوسان‌گیری درون‌روزی و کوتاه‌مدت (۲ تا ۳ روزه) را با هوش مصنوعی بسازیم و **قبل از استفاده،
روی داده‌های گذشته با کسر کارمزد واقعی آزمایش (بکتست) کنیم**.

**کار روزانه پیشنهادی**
۱. صبح قبل از ساعت ۹: صفحه «خانه» ← «شروع پایش زنده»
۲. بعد از ساعت ۱۴: «شروع به‌روزرسانی» (قیمت‌های پایانی و حقیقی/حقوقی روز)
۳. هر زمان: «اطلاعیه‌های کدال» ← دریافت اطلاعیه‌های جدید

**اگر چیزی کار نکرد**
صفحه «سلامت سامانه» ← اجرای عیب‌یابی. اگر سایت‌های ایرانی (tsetmc.com و codal.ir) باز نشدند،
در Proxifier آن‌ها را روی Direct بگذارید. سرویس هوش مصنوعی (typesafe.ai) به VPN نیاز دارد.

⚠️ **مهم:** خروجی‌های این برنامه توصیه سرمایه‌گذاری نیستند. هیچ استراتژی بدون بکتست خارج از نمونه
و کسر کارمزد (حداقل ۱.۲۶٪ رشد برای سر به سر شدن) قابل اتکا نیست.
        """
    )


ensure_database()

PAGES = {
    "🏠 خانه": page_home,
    '🌐 بررسی بازار امروز': lambda: page_market_review(job_panel),
    "🔎 فهرست بررسی": page_shortlist,
    '🧪 آزمایشگاه فیلترها': page_flow_lab,
    "🛡️ برنامه و دفتر معاملات": page_workbench,
    '💼 پورتفوی مجازی': page_paper,
    '🗓️ دفتر مشاهده آینده': page_forward_observations,
    "🏦 حساب و تطبیق فارابی": page_broker,
    "📊 بررسی نماد": page_symbol,
    "📡 دیده‌بان زنده": page_live,
    "📰 اطلاعیه‌های کدال": page_codal,
    "🔬 پژوهش": page_research,
    '💸 هزینه و ظرفیت سرمایه': page_capital_capacity,
    '🎲 آزمایش احتمال و بازده': lambda: page_probability(job_panel),
    "📚 مطالعه رشد نمادها": page_stock_study,
    '🔎 پرسش‌گر بازار': page_query_screener,
    "🩺 سلامت سامانه": page_health,
    "ℹ️ راهنما": page_guide,
}

with st.sidebar:
    st.markdown(f"### 📈 BRS\n<span class='brs-muted'>نسخه {to_fa_digits(settings.VERSION)} — سهام بورس و فرابورس</span>",
                unsafe_allow_html=True)
    choice = st.radio("منو", list(PAGES), label_visibility="collapsed")
    st.divider()
    if st.button("🔄 تازه‌سازی صفحه", width="stretch"):
        st.cache_data.clear()
        st.rerun()

PAGES[choice]()
