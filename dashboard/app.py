# -*- coding: utf-8 -*-
"""
📊 BRS Data Engine v0.1 — داشبورد استریم‌لیت
نمایش وضعیت بازار، قیمت سهام، نمودار قیمت و سلامت داده‌ها

اجرا:
    streamlit run dashboard/app.py
"""

import sys
from pathlib import Path
from datetime import datetime

import streamlit as st
import pandas as pd

# ──────────────────────────────────────────────
# افزودن مسیر ریشه پروژه به مسیر پایتون
# تا import از config و database به‌درستی کار کند
# ──────────────────────────────────────────────
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config.settings import (
    PROJECT_NAME,
    VERSION,
    DB_PATH,
    TEST_SYMBOLS,
    TIMEZONE,
    PRICE_UNIT,
)

# ──────────────────────────────────────────────
# تلاش برای بارگذاری ماژول پایگاه داده
# اگر هنوز database/db.py ساخته نشده، پیام مناسب نمایش می‌دهیم
# ──────────────────────────────────────────────
try:
    from database.db import DatabaseManager, BRSDatabase
    DB_MODULE_AVAILABLE = True
except ImportError:
    try:
        from database.db import DatabaseManager
        BRSDatabase = DatabaseManager
        DB_MODULE_AVAILABLE = True
    except ImportError:
        DB_MODULE_AVAILABLE = False


# ═══════════════════════════════════════════════
# تنظیمات اصلی صفحه استریم‌لیت
# ═══════════════════════════════════════════════
st.set_page_config(
    page_title="📊 BRS Data Engine v0.1",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ──────────────────────────────────────────────
# توابع کمکی برای خواندن داده از دیتابیس
# ──────────────────────────────────────────────

def _get_db():
    """ساخت اتصال به پایگاه داده و بازگرداندن آبجکت BRSDatabase"""
    if not DB_MODULE_AVAILABLE:
        return None
    if not DB_PATH.exists():
        return None
    try:
        return BRSDatabase(str(DB_PATH))
    except Exception as exc:
        st.error(f"❌ خطا در اتصال به پایگاه داده: {exc}")
        return None


def _safe_query(db, query: str, params: tuple = ()) -> pd.DataFrame:
    """اجرای یک کوئری و بازگرداندن نتیجه به صورت DataFrame — با مدیریت خطا"""
    try:
        import sqlite3
        conn = sqlite3.connect(str(DB_PATH))
        df = pd.read_sql_query(query, conn, params=params)
        conn.close()
        return df
    except Exception as exc:
        st.error(f"❌ خطا در خواندن داده: {exc}")
        return pd.DataFrame()


def _count_records(table: str) -> int:
    """شمارش تعداد رکوردهای یک جدول"""
    df = _safe_query(None, f"SELECT COUNT(*) as cnt FROM {table}")
    if df.empty:
        return 0
    return int(df.iloc[0]["cnt"])


def _db_exists() -> bool:
    """بررسی وجود فایل دیتابیس"""
    return DB_PATH.exists()


# ═══════════════════════════════════════════════
# ◀ نوار کناری (Sidebar)
# ═══════════════════════════════════════════════
with st.sidebar:
    st.image("https://img.icons8.com/color/96/stocks.png", width=64)
    st.title(f"{PROJECT_NAME}")
    st.caption(f"نسخه {VERSION}")
    st.divider()

    # نمایش زمان فعلی
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st.markdown(f"🕐 **آخرین به‌روزرسانی صفحه:** {now_str}")

    # تعداد نمادهای تحت پایش
    st.metric(label="📌 نمادهای تحت پایش", value=len(TEST_SYMBOLS))

    # نمایش نمادها
    st.markdown("**نمادهای آزمایشی:**")
    st.code(" ، ".join(TEST_SYMBOLS), language=None)

    st.divider()
    st.markdown(f"💾 واحد قیمت: **{PRICE_UNIT}**")
    st.markdown(f"🌍 منطقه زمانی: **{TIMEZONE}**")

    # دکمه رفرش
    if st.button("🔄 به‌روزرسانی داده‌ها", use_container_width=True):
        st.rerun()


# ═══════════════════════════════════════════════
# عنوان اصلی داشبورد
# ═══════════════════════════════════════════════
st.title("📊 BRS Data Engine v0.1")
st.markdown("**سامانه جمع‌آوری و نمایش داده‌های بازار سرمایه ایران**")
st.divider()


# ──────────────────────────────────────────────
# بررسی وضعیت کلی سامانه — آیا دیتابیس و ماژول‌ها آماده‌اند؟
# ──────────────────────────────────────────────
if not DB_MODULE_AVAILABLE:
    st.warning(
        "⚠️ ماژول `database.db` هنوز ایجاد نشده است. "
        "لطفاً ابتدا فایل `database/db.py` را بسازید و سپس داشبورد را اجرا کنید."
    )
    st.info("💡 برای ساخت پایگاه داده، اسکریپت مربوطه را اجرا کنید.")
    st.stop()

if not _db_exists():
    st.warning(
        "⚠️ فایل پایگاه داده یافت نشد.\n\n"
        f"مسیر مورد انتظار: `{DB_PATH}`\n\n"
        "لطفاً ابتدا اسکریپت جمع‌آوری داده را اجرا کنید تا پایگاه داده ساخته شود."
    )
    st.stop()


# ═══════════════════════════════════════════════
# 1️⃣ بخش: 🏠 خلاصه وضعیت (Status Summary)
# ═══════════════════════════════════════════════
st.header("🏠 خلاصه وضعیت")

try:
    # شمارش رکوردهای هر جدول
    total_symbols = _count_records("symbols")
    total_daily = _count_records("daily_prices")
    total_snapshots = _count_records("snapshots")
    total_logs = _count_records("collection_log")

    # آخرین زمان جمع‌آوری
    last_log_df = _safe_query(
        None,
        "SELECT finished_at, status FROM collection_log ORDER BY id DESC LIMIT 1"
    )
    if not last_log_df.empty:
        last_collection_time = last_log_df.iloc[0]["finished_at"] or "نامشخص"
        last_collection_status = last_log_df.iloc[0]["status"] or "نامشخص"
    else:
        last_collection_time = "هنوز جمع‌آوری انجام نشده"
        last_collection_status = "—"

    # نمایش متریک‌ها در چهار ستون
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric(label="📋 تعداد نمادها", value=total_symbols)
    with col2:
        st.metric(label="📊 رکوردهای روزانه", value=f"{total_daily:,}")
    with col3:
        st.metric(label="📸 اسنپ‌شات‌ها", value=f"{total_snapshots:,}")
    with col4:
        st.metric(label="📝 لاگ‌های جمع‌آوری", value=total_logs)

    st.markdown(f"🕐 **آخرین جمع‌آوری:** {last_collection_time} — وضعیت: `{last_collection_status}`")

except Exception as exc:
    st.error(f"❌ خطا در بارگذاری خلاصه وضعیت: {exc}")

st.divider()


# ═══════════════════════════════════════════════
# 2️⃣ بخش: 📈 قیمت سهام (Stock Prices)
# ═══════════════════════════════════════════════
st.header("📈 قیمت سهام")

try:
    # دریافت آخرین قیمت هر نماد — آخرین رکورد بر اساس تاریخ
    latest_prices_query = """
    SELECT
        s.symbol_fa   AS 'نماد',
        dp.close      AS 'آخرین قیمت',
        dp.adj_close  AS 'قیمت پایانی',
        dp.volume     AS 'حجم',
        dp.value      AS 'ارزش',
        ROUND(
            CASE
                WHEN dp.open IS NOT NULL AND dp.open > 0
                THEN ((dp.close - dp.open) / dp.open) * 100
                ELSE 0
            END, 2
        ) AS 'تغییر (%)',
        dp.date       AS 'تاریخ'
    FROM daily_prices dp
    JOIN symbols s ON dp.symbol_id = s.id
    WHERE dp.date = (
        SELECT MAX(dp2.date) FROM daily_prices dp2 WHERE dp2.symbol_id = dp.symbol_id
    )
    ORDER BY s.symbol_fa;
    """
    prices_df = _safe_query(None, latest_prices_query)

    if prices_df.empty:
        st.info("📭 هنوز داده قیمتی ثبت نشده است. لطفاً ابتدا اسکریپت جمع‌آوری داده را اجرا کنید.")
    else:
        # فرمت اعداد بزرگ برای خوانایی بهتر
        st.dataframe(
            prices_df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "آخرین قیمت": st.column_config.NumberColumn(format="%,.0f ریال"),
                "قیمت پایانی": st.column_config.NumberColumn(format="%,.0f ریال"),
                "حجم": st.column_config.NumberColumn(format="%,.0f"),
                "ارزش": st.column_config.NumberColumn(format="%,.0f ریال"),
                "تغییر (%)": st.column_config.NumberColumn(format="%.2f%%"),
            },
        )
        st.caption(f"📌 واحد قیمت‌ها: {PRICE_UNIT}")

except Exception as exc:
    st.error(f"❌ خطا در بارگذاری قیمت سهام: {exc}")

st.divider()


# ═══════════════════════════════════════════════
# 3️⃣ بخش: 📊 نمودار قیمت (Price Chart)
# ═══════════════════════════════════════════════
st.header("📊 نمودار قیمت")

try:
    # دریافت لیست نمادهای موجود در دیتابیس
    symbols_df = _safe_query(None, "SELECT id, symbol_fa FROM symbols ORDER BY symbol_fa")

    if symbols_df.empty:
        st.info("📭 هنوز نمادی ثبت نشده است.")
    else:
        # منوی کشویی انتخاب نماد
        symbol_list = symbols_df["symbol_fa"].tolist()
        selected_symbol = st.selectbox(
            "🔍 نماد مورد نظر را انتخاب کنید:",
            options=symbol_list,
            index=0,
        )

        # شناسه نماد انتخاب‌شده
        selected_id = int(
            symbols_df[symbols_df["symbol_fa"] == selected_symbol]["id"].iloc[0]
        )

        # دریافت تاریخچه قیمت پایانی نماد
        history_query = """
        SELECT date AS 'تاریخ', adj_close AS 'قیمت پایانی', close AS 'آخرین قیمت', volume AS 'حجم'
        FROM daily_prices
        WHERE symbol_id = ?
        ORDER BY date ASC;
        """
        history_df = _safe_query(None, history_query, (selected_id,))

        if history_df.empty:
            st.info(f"📭 داده تاریخی برای نماد «{selected_symbol}» موجود نیست.")
        else:
            # تنظیم ستون تاریخ به عنوان ایندکس
            chart_df = history_df.set_index("تاریخ")

            # نمودار مساحتی قیمت پایانی
            st.subheader(f"📈 روند قیمت پایانی — {selected_symbol}")
            st.area_chart(chart_df[["قیمت پایانی"]], use_container_width=True)

            # نمودار حجم معاملات
            st.subheader(f"📊 حجم معاملات — {selected_symbol}")
            st.bar_chart(chart_df[["حجم"]], use_container_width=True)

            # جدول تاریخچه کامل
            with st.expander(f"📋 جدول تاریخچه قیمت {selected_symbol}"):
                st.dataframe(history_df, use_container_width=True, hide_index=True)

except Exception as exc:
    st.error(f"❌ خطا در بارگذاری نمودار قیمت: {exc}")

st.divider()


# ═══════════════════════════════════════════════
# 4️⃣ بخش: 🔍 سلامت دیتا (Data Health)
# ═══════════════════════════════════════════════
st.header("🔍 سلامت دیتا")

try:
    # ─── لاگ‌های اخیر جمع‌آوری ───
    st.subheader("📝 لاگ‌های اخیر جمع‌آوری داده")
    logs_query = """
    SELECT
        started_at          AS 'زمان شروع',
        finished_at         AS 'زمان پایان',
        collection_type     AS 'نوع',
        symbols_requested   AS 'نمادهای درخواستی',
        symbols_succeeded   AS 'موفق',
        symbols_failed      AS 'ناموفق',
        status              AS 'وضعیت',
        errors              AS 'خطاها'
    FROM collection_log
    ORDER BY id DESC
    LIMIT 20;
    """
    logs_df = _safe_query(None, logs_query)

    if logs_df.empty:
        st.info("📭 هنوز لاگ جمع‌آوری ثبت نشده است.")
    else:
        st.dataframe(logs_df, use_container_width=True, hide_index=True)

        # ─── محاسبه نرخ موفقیت ───
        success_rate_query = """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success_count,
            SUM(CASE WHEN status = 'partial' THEN 1 ELSE 0 END) AS partial_count,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count
        FROM collection_log;
        """
        rate_df = _safe_query(None, success_rate_query)

        if not rate_df.empty:
            total = int(rate_df.iloc[0]["total"])
            success = int(rate_df.iloc[0]["success_count"])
            partial = int(rate_df.iloc[0]["partial_count"])
            failed = int(rate_df.iloc[0]["failed_count"])

            if total > 0:
                success_pct = round((success / total) * 100, 1)
            else:
                success_pct = 0.0

            col1, col2, col3, col4 = st.columns(4)
            with col1:
                st.metric("✅ موفق", success)
            with col2:
                st.metric("⚠️ ناقص", partial)
            with col3:
                st.metric("❌ ناموفق", failed)
            with col4:
                st.metric("📊 نرخ موفقیت", f"{success_pct}%")

    # ─── هشدار داده‌های ناقص ───
    st.subheader("⚠️ هشدارهای داده ناقص")

    # نمادهایی که داده قیمتی ندارند
    missing_query = """
    SELECT s.symbol_fa AS 'نماد'
    FROM symbols s
    LEFT JOIN daily_prices dp ON s.id = dp.symbol_id
    WHERE dp.id IS NULL;
    """
    missing_df = _safe_query(None, missing_query)

    if missing_df.empty:
        st.success("✅ همه نمادهای ثبت‌شده دارای داده قیمتی هستند.")
    else:
        st.warning(
            f"⚠️ {len(missing_df)} نماد بدون داده قیمتی یافت شد:"
        )
        st.dataframe(missing_df, use_container_width=True, hide_index=True)

    # بررسی داده‌های قدیمی (بیش از 7 روز بدون به‌روزرسانی)
    stale_query = """
    SELECT
        s.symbol_fa AS 'نماد',
        MAX(dp.date) AS 'آخرین تاریخ',
        CAST(julianday('now') - julianday(MAX(dp.date)) AS INTEGER) AS 'روز بدون به‌روزرسانی'
    FROM symbols s
    JOIN daily_prices dp ON s.id = dp.symbol_id
    GROUP BY s.id
    HAVING CAST(julianday('now') - julianday(MAX(dp.date)) AS INTEGER) > 7;
    """
    stale_df = _safe_query(None, stale_query)

    if stale_df.empty:
        st.success("✅ همه نمادها به‌روز هستند (داده‌ای قدیمی‌تر از ۷ روز وجود ندارد).")
    else:
        st.warning(f"⚠️ {len(stale_df)} نماد بیش از ۷ روز بدون به‌روزرسانی:")
        st.dataframe(stale_df, use_container_width=True, hide_index=True)

except Exception as exc:
    st.error(f"❌ خطا در بارگذاری سلامت دیتا: {exc}")


# ═══════════════════════════════════════════════
# فوتر
# ═══════════════════════════════════════════════
st.divider()
st.caption(
    f"📊 {PROJECT_NAME} v{VERSION} — "
    "سامانه جمع‌آوری داده‌های بورس اوراق بهادار تهران و فرابورس ایران"
)
