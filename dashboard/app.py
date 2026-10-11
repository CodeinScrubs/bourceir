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
    return to_fa_digits(f"{value:,.{digits}f}")


def billion_toman(rial_value) -> float:
    """تبدیل ریال به میلیارد تومان (فقط برای نمایش؛ ذخیره‌سازی همیشه ریالی است)."""
    return rial_value / 10 / 1e9


@st.cache_data(ttl=60)
def symbol_list() -> list:
    return query_df("SELECT symbol_fa FROM symbols WHERE is_active = 1 ORDER BY symbol_fa;")["symbol_fa"].tolist()


@st.cache_data(ttl=60)
def market_pulse() -> dict:
    """نبض بازار در آخرین روز معاملاتی ثبت‌شده (فقط سهام داخل دامنه پروژه)."""
    last = query_df("SELECT MAX(date) AS d FROM daily_prices WHERE source = 'tsetmc';")["d"].iloc[0]
    if not last:
        return {}
    df = query_df(
        """
        SELECT dp.*, ct.buy_i_value, ct.sell_i_value
        FROM daily_prices dp
        LEFT JOIN client_types_daily ct ON ct.symbol_id = dp.symbol_id AND ct.date = dp.date
        WHERE dp.date = ? AND dp.source = 'tsetmc';
        """, (last,))
    flags = market_rules.lock_flags(df)
    ret = market_rules.pit_daily_returns(df)
    return {
        "date": last, "count": len(df), "up": int((ret > 0).sum()), "down": int((ret < 0).sum()),
        "locked_up": int(flags["locked_up"].sum()), "locked_down": int(flags["locked_down"].sum()),
        "value": float(df["value"].sum()),
        "real_flow": float((df["buy_i_value"] - df["sell_i_value"]).sum()) if df["buy_i_value"].notna().any() else None,
        "flow_coverage": int(df["buy_i_value"].notna().sum()),
    }


@st.cache_data(ttl=60)
def symbol_daily(symbol: str) -> pd.DataFrame:
    with ro_db() as db:
        df = db.get_daily_prices_df(symbol)
        ct = db.get_client_types_df(symbol) if not df.empty else pd.DataFrame()
    if df.empty:
        return df
    if not ct.empty:
        df = df.merge(ct, on="date", how="left")
    df["change_pct"] = market_rules.pit_daily_returns(df) * 100
    df = pd.concat([df, market_rules.lock_flags(df)], axis=1)
    if "buy_i_value" in df.columns:
        df["real_flow"] = df["buy_i_value"] - df["sell_i_value"]
        df["buyer_power"] = (df["buy_i_volume"] / df["buy_i_count"]) / (df["sell_i_volume"] / df["sell_i_count"])
    df["jalali"] = df["date"].map(lambda d: to_jalali(d, fa_digits=False))
    return df


@st.cache_data(ttl=20)
def latest_snapshots() -> pd.DataFrame:
    """آخرین اسنپ‌شات هر نماد در آخرین روزی که پایش زنده انجام شده."""
    last = query_df("SELECT MAX(captured_at) AS m FROM snapshots;")["m"].iloc[0]
    if not last:
        return pd.DataFrame()
    day = last[:10]
    # بازه روی captured_at تا ایندکس استفاده شود ("~" بعد از همه ارقام و حرف T قرار می‌گیرد)
    return query_df(
        """
        SELECT s.symbol_fa, s.name_fa, sn.*
        FROM snapshots sn JOIN symbols s ON s.id = sn.symbol_id
        WHERE sn.id IN (SELECT MAX(id) FROM snapshots WHERE captured_at >= ? AND captured_at < ? GROUP BY symbol_id);
        """, (day, day + "~"))


# ═══════════════════════════════════════════════
# اجزای مشترک
# ═══════════════════════════════════════════════

JOB_LABELS = {"update": "به‌روزرسانی داده‌ها", "live": "پایش زنده بازار", "codal": "اطلاعیه‌های کدال"}


def job_panel(name: str, args: list, start_label: str, help_text: str, stoppable: bool = False) -> None:
    status = jobs.job_status(name)
    cols = st.columns([2, 1])
    with cols[0]:
        if status["running"]:
            st.info(f"⏳ «{JOB_LABELS[name]}» در حال اجراست...")
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
    open_now = is_market_open(now)
    st.markdown(
        f"<div class='brs-card'>🕐 امروز {to_jalali(now, with_time=True)} — بازار "
        f"<b>{'باز است 🟢' if open_now else 'بسته است 🔴'}</b> (شنبه تا چهارشنبه، ۹:۰۰ تا ۱۲:۳۰)</div>",
        unsafe_allow_html=True,
    )

    pulse = market_pulse()
    if pulse:
        st.subheader(f"نبض بازار — {to_jalali(pulse['date'])}")
        st.caption(f"{fa_num(pulse['count'])} سهم بورس و فرابورس — مبالغ به میلیارد تومان")
        row1 = st.columns(2)
        row1[0].metric("📈 مثبت / 📉 منفی", f"{fa_num(pulse['up'])} / {fa_num(pulse['down'])}")
        row1[1].metric("🟢 صف خرید / 🔴 صف فروش", f"{fa_num(pulse['locked_up'])} / {fa_num(pulse['locked_down'])}",
                       help="نمادهایی که کل روز در سقف یا کف دامنه نوسان قفل بودند")
        row2 = st.columns(2)
        row2[0].metric("💵 ارزش معاملات", fa_num(billion_toman(pulse["value"])))
        if pulse["real_flow"] is not None:
            partial = pulse["flow_coverage"] < pulse["count"]
            row2[1].metric("👥 ورود پول حقیقی" + (" (ناقص)" if partial else ""), fa_num(billion_toman(pulse["real_flow"])),
                           help="خرید حقیقی منهای فروش حقیقی؛ منفی یعنی خروج پول حقیقی")
            if partial:
                st.caption(f"⏳ آمار حقیقی/حقوقی امروز فعلاً برای {fa_num(pulse['flow_coverage'])} از "
                           f"{fa_num(pulse['count'])} سهم منتشر شده است (TSETMC تا عصر کامل می‌کند).")
    else:
        st.warning("هنوز داده‌ای ثبت نشده است. دکمه «به‌روزرسانی داده‌ها» را بزنید.")

    st.divider()
    st.subheader("🔄 به‌روزرسانی داده‌ها")
    job_panel("update", ["update"], "شروع به‌روزرسانی",
              "فهرست سهام، قیمت‌های روزانه، حقیقی/حقوقی و اطلاعیه‌های کدال را به‌روز می‌کند. "
              "بار اول حدود ۴۵ دقیقه و بعد از آن چند دقیقه طول می‌کشد. بعد از ساعت ۱۴ قیمت‌های امروز کامل است؛ "
              "آمار حقیقی/حقوقی امروز را TSETMC تا عصر منتشر می‌کند، پس اجرای دوباره در عصر مفید و بی‌خطر است.")

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
    if last["locked_up"]:
        st.success("🟢 آخرین روز: صف خرید قفل (کل روز در سقف دامنه)")
    elif last["locked_down"]:
        st.error("🔴 آخرین روز: صف فروش قفل (کل روز در کف دامنه)")

    base = alt.Chart(view).encode(x=alt.X("jalali:O", title=None, sort=None,
                                          axis=alt.Axis(labelOverlap=True, labelAngle=0)))
    price = base.mark_line(strokeWidth=2).encode(
        y=alt.Y("close_final_raw:Q", title="قیمت پایانی (ریال)", scale=alt.Scale(zero=False)),
        tooltip=[alt.Tooltip("jalali:O", title="تاریخ"), alt.Tooltip("close_final_raw:Q", title="پایانی", format=",.0f"),
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
        "وضعیت": table.apply(lambda r: "صف خرید قفل" if r["locked_up"] else ("صف فروش قفل" if r["locked_down"] else ""), axis=1),
    })
    if "real_flow" in table.columns:
        out["پول حقیقی (م.ت)"] = (table["real_flow"] / 1e10).round(1)
    st.dataframe(out, hide_index=True, width="stretch")
    feature_panel(symbol)


@st.cache_data(ttl=300)
def cached_symbol_features(symbol: str) -> pd.DataFrame:
    return load_symbol_features(symbol)


def feature_panel(symbol: str) -> None:
    """آخرین مقدار فیچرهای نماد از انبار فیچر (همان ورودی‌هایی که مدل‌ها می‌بینند)."""
    manifest = load_manifest()
    with st.expander("📐 فیچرهای آخرین روز (ورودی مدل‌های هوش مصنوعی)"):
        if not manifest:
            st.info("انبار فیچر هنوز ساخته نشده است. «به‌روزرسانی داده‌ها» آن را در پایان می‌سازد.")
            return
        feats = cached_symbol_features(symbol)
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
        snaps["change_pct"] = (snaps["close_price"] / snaps["ref_price"] - 1) * 100
        snaps["real_flow_bt"] = (snaps["real_buy_volume"] - snaps["real_sell_volume"]) * snaps["close_price"] / 1e10
        snaps["buyer_power"] = (snaps["real_buy_volume"] / snaps["real_buy_count"]) / (
            snaps["real_sell_volume"] / snaps["real_sell_count"])
        view_filter = st.radio("نمایش", ["همه", "فقط صف خرید", "فقط صف فروش", "قدرت خریدار > ۲"], horizontal=True)
        if view_filter == "فقط صف خرید":
            snaps = snaps[snaps["buy_queue_volume"] > 0]
        elif view_filter == "فقط صف فروش":
            snaps = snaps[snaps["sell_queue_volume"] > 0]
        elif view_filter == "قدرت خریدار > ۲":
            snaps = snaps[snaps["buyer_power"] > 2]
        snaps = snaps.sort_values("value", ascending=False)
        st.caption(f"آخرین ثبت: {to_jalali(snaps['captured_at'].max(), with_time=True)} — {to_fa_digits(len(snaps))} نماد")
        st.dataframe(pd.DataFrame({
            "نماد": snaps["symbol_fa"],
            "آخرین": snaps["last_price"],
            "پایانی": snaps["close_price"],
            "تغییر ٪": snaps["change_pct"].round(2),
            "صف خرید (حجم)": snaps["buy_queue_volume"],
            "صف فروش (حجم)": snaps["sell_queue_volume"],
            "قدرت خریدار": snaps["buyer_power"].round(2),
            "پول حقیقی (م.ت)": snaps["real_flow_bt"].round(1),
            "ارزش (م.ت)": (snaps["value"] / 1e10).round(1),
        }), hide_index=True, width="stretch", height=600)

    live_table()


def page_codal() -> None:
    st.title("📰 اطلاعیه‌های کدال")
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


def page_health() -> None:
    st.title("🩺 سلامت سامانه")
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
    "📊 بررسی نماد": page_symbol,
    "📡 دیده‌بان زنده": page_live,
    "📰 اطلاعیه‌های کدال": page_codal,
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
