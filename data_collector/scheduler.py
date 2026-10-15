# -*- coding: utf-8 -*-
"""
هماهنگ‌کننده گردآوری داده‌ها (Data Collector)

وظایف:
- همگام‌سازی جامعه سهام بورس و فرابورس
- دریافت تاریخچه روزانه (کامل یا افزایشی) + علامت‌گذاری ناهنجاری‌های کمی
- دریافت تاریخچه حقیقی/حقوقی
- اسنپ‌شات لحظه‌ای کل بازار در ساعات معاملات (یک درخواست برای همه نمادها)
- دریافت اطلاعیه‌های کدال
هر اجرا در جدول collection_log ثبت می‌شود.
"""

import logging
from datetime import date
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from config import settings
from core.timeutils import is_market_open, now_tehran, now_tehran_iso
from data_collector.base import DAILY_COLUMNS, BaseDataProvider
from data_collector.codal import CodalProvider
from data_collector.tsetmc import TsetmcProvider
from data_collector.universe import UniverseManager
from data_collector.validator import DataValidator
from database.db import DatabaseManager

logger = logging.getLogger(__name__)

# اگر فاصله آخرین داده موجود بیشتر از این تعداد روز باشد، تاریخچه کامل دوباره دریافت می‌شود
INCREMENTAL_LOOKBACK_ROWS = 30
# داده دیده‌بان بعد از این ساعت (به وقت تهران) برای کندل روز نهایی فرض می‌شود
SESSION_FINAL_AFTER = "13:00"
INCREMENTAL_MAX_GAP_DAYS = 25
CLIENT_TYPE_REFRESH_SESSIONS = 5

ProgressFn = Callable[[int, int, str], None]


def is_market_trading_time() -> bool:
    """سازگاری با نسخه قبلی: آیا بازار هم‌اکنون (به وقت تهران) باز است؟"""
    return is_market_open()


class DataCollector:
    """کلاس هماهنگ‌کننده گردآوری داده‌ها و ذخیره‌سازی در پایگاه داده."""

    def __init__(self, symbols: Optional[List[str]] = None, db: Optional[DatabaseManager] = None,
                 provider: Optional[BaseDataProvider] = None, codal: Optional[CodalProvider] = None):
        self.db = db if db is not None else DatabaseManager()
        self.provider = provider or TsetmcProvider()
        self.codal = codal or CodalProvider()
        self.universe = UniverseManager(db=self.db, provider=self.provider)
        self.symbols = list(symbols) if symbols else None
        # اثر انگشت آخرین اسنپ‌شات هر نماد؛ اسنپ‌شات بدون تغییر دوباره ذخیره نمی‌شود
        self._last_fingerprints: Dict[str, tuple] = {}

    @staticmethod
    def _fingerprint(snap: Dict[str, Any]) -> tuple:
        """همه فیلدها به جز زمان ثبت؛ تغییر در سطوح ۲ تا ۵ عمق بازار هم «تغییر» حساب می‌شود."""
        return tuple(sorted((k, v) for k, v in snap.items() if k != "captured_at"))

    # ═══════════════════════════════════════════════
    # کمکی‌ها
    # ═══════════════════════════════════════════════

    def target_symbols(self, symbols: Optional[List[str]] = None) -> List[str]:
        """نمادهای هدف: ورودی صریح → نمادهای سازنده → کل جامعه سهام → نمادهای پیش‌فرض تنظیمات."""
        if symbols:
            return list(symbols)
        if self.symbols:
            return list(self.symbols)
        universe = self.universe.get_tracked_symbols()
        return universe or list(settings.TEST_SYMBOLS)

    def _resolve(self, symbol_fa: str) -> Optional[Dict[str, Any]]:
        """یافتن نماد در دیتابیس با ins_code؛ در صورت نبود، جستجو در منبع داده و ثبت."""
        sym = self.db.get_symbol(symbol_fa)
        if sym and sym.get("ins_code"):
            # نماد متوقف (مثلاً در مجمع) موقتاً غیرفعال است ولی هنوز داخل دامنه؛ با درخواست صریح به‌روز می‌شود
            if not TsetmcProvider.is_in_scope(sym["symbol_fa"], sym.get("instrument_type_code")):
                logger.warning("نماد ذخیره‌شده «%s» خارج از دامنه پروژه است.", symbol_fa)
                return None
            return sym
        meta = self.provider.search_symbol(symbol_fa)
        if not meta:
            return None
        if not TsetmcProvider.is_in_scope(meta["symbol_fa"], meta.get("instrument_type_code")):
            logger.warning("نماد «%s» سهم عادی بورس/فرابورس نیست و خارج از دامنه پروژه است.", symbol_fa)
            return None
        self.db.upsert_symbol(meta)
        return self.db.get_symbol(meta["symbol_fa"])

    def _finish(self, collection_type: str, started_at: str, requested: int, succeeded: int,
                failed: int, errors: List[str], extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        status = "success" if failed == 0 else ("partial" if succeeded > 0 or (extra or {}).get("partial_symbols") else "failed")
        finished_at = now_tehran_iso()
        self.db.log_collection(
            started_at=started_at, finished_at=finished_at, collection_type=collection_type,
            symbols_requested=requested, symbols_succeeded=succeeded, symbols_failed=failed,
            errors="; ".join(errors[:50]) if errors else None, status=status,
        )
        report = {
            "collection_type": collection_type, "started_at": started_at, "finished_at": finished_at,
            "symbols_requested": requested, "symbols_succeeded": succeeded, "symbols_failed": failed,
            "errors": errors, "status": status,
        }
        report.update(extra or {})
        logger.info("گردآوری «%s» پایان یافت: %s (موفق %d، ناموفق %d)", collection_type, status, succeeded, failed)
        return report

    # ═══════════════════════════════════════════════
    # جامعه سهام
    # ═══════════════════════════════════════════════

    def sync_universe(self) -> Dict[str, Any]:
        started_at = now_tehran_iso()
        result = self.universe.sync_universe()
        errors = [] if result["received"] else ["فهرست نمادها از TSETMC دریافت نشد"]
        return self._finish("universe", started_at, result["received"], result["saved"],
                            result["failed"] + (0 if result["received"] else 1), errors, result)

    # ═══════════════════════════════════════════════
    # تاریخچه روزانه
    # ═══════════════════════════════════════════════

    def _history_window(self, symbol_id: int, full: bool) -> int:
        """تعداد ردیف مورد نیاز: ۰ یعنی کل تاریخچه."""
        if full:
            return 0
        last_date = self.db.get_last_daily_date(symbol_id)
        if not last_date:
            return 0
        gap_days = (now_tehran().date() - date.fromisoformat(last_date)).days
        return 0 if gap_days > INCREMENTAL_MAX_GAP_DAYS else INCREMENTAL_LOOKBACK_ROWS

    def _missing_client_type_dates(self, symbol_id: int) -> Optional[List[str]]:
        """
        روزهای جدید و پنج جلسه اخیر، شامل رکورد موجود و شکاف پشت آخرین تاریخ.
        None یعنی کل تاریخچه دریافت شود (اولین بار یا فاصله زیاد).
        """
        row = self.db.conn.execute(
            "SELECT MAX(date) AS d FROM client_types_daily WHERE symbol_id = ?;", (symbol_id,)
        ).fetchone()
        if not row or not row["d"]:
            return None
        last = row["d"]
        if (now_tehran().date() - date.fromisoformat(last)).days > INCREMENTAL_MAX_GAP_DAYS:
            return None
        # بازخوانی محدود، اصلاح آمار اخیر را می‌گیرد؛ حفره‌های بسیار قدیمی هر روز درخواست نمی‌شوند.
        recent = self.db.conn.execute(
            "SELECT date FROM daily_prices WHERE symbol_id=? AND source='tsetmc' AND date<=? "
            "ORDER BY date DESC LIMIT ?", (symbol_id, now_tehran().date().isoformat(), CLIENT_TYPE_REFRESH_SESSIONS),
        ).fetchall()
        threshold = min([r['date'] for r in recent] + [last])
        return [r['date'] for r in self.db.conn.execute(
            "SELECT date FROM daily_prices WHERE symbol_id=? AND source='tsetmc' AND date>=? AND date<=? ORDER BY date",
            (symbol_id, threshold, now_tehran().date().isoformat()))]

    def collect_history(self, symbols: Optional[List[str]] = None, full: bool = False,
                        progress: Optional[ProgressFn] = None) -> Dict[str, Any]:
        """دریافت و ذخیره تاریخچه روزانه (افزایشی، مگر full=True)."""
        targets = self.target_symbols(symbols)
        started_at = now_tehran_iso()
        succeeded = failed = rows_saved = anomalies = 0
        errors: List[str] = []

        for i, sym in enumerate(targets, start=1):
            if progress:
                progress(i, len(targets), sym)
            try:
                meta = self._resolve(sym)
                if not meta:
                    errors.append(f"نماد «{sym}» یافت نشد یا خارج از دامنه است")
                    failed += 1
                    continue
                df = self.provider.get_symbol_history(meta["ins_code"], last_n=self._history_window(meta["id"], full))
                if df is None:
                    errors.append(f"عدم دریافت تاریخچه «{sym}»")
                    failed += 1
                    continue
                rows_saved += self.db.upsert_daily_prices_df(meta["id"], df)

                flags = DataValidator.quant_anomaly_flags(df)
                flagged = df.index[flags["price_anomaly"] | flags["volume_anomaly"]]
                anomalies += self.db.insert_quality_metrics_bulk([{
                    "symbol_id": meta["id"], "trade_date": df.at[idx, "date"],
                    "price_anomaly_flag": int(flags.at[idx, "price_anomaly"]),
                    "volume_anomaly_flag": int(flags.at[idx, "volume_anomaly"]),
                    "notes": "ناهنجاری کمی در تاریخچه روزانه",
                } for idx in flagged])
                succeeded += 1
            except Exception as e:
                logger.exception("خطای پیش‌بینی‌نشده در گردآوری تاریخچه «%s»", sym)
                errors.append(f"خطا در «{sym}»: {e}")
                failed += 1

        return self._finish("history", started_at, len(targets), succeeded, failed, errors,
                            {"rows_inserted": rows_saved, "anomalies_flagged": anomalies})

    def collect_session_candles(self) -> Dict[str, Any]:
        """
        ثبت کندل امروز همه سهام با یک درخواست به دیده‌بان بازار (بعد از ساعت ۱۳).
        سرویس تاریخچه روزانه TSETMC کندل امروز را تا عصر به‌تدریج منتشر می‌کند؛ این مرحله داده امروز را
        بلافاصله کامل می‌کند و به‌روزرسانی بعدی تاریخچه، همان رکورد را با داده رسمی بازنویسی می‌کند.
        """
        started_at = now_tehran_iso()
        now = now_tehran()
        if not self.db.is_trading_day(now.date().isoformat()) or now.strftime('%H:%M') < SESSION_FINAL_AFTER:
            return {"collection_type": "session_candles", "status": "skipped", "symbols_succeeded": 0,
                    "symbols_failed": 0, "errors": [],
                    "message": "دیده‌بان کندل امروز در روز تعطیل ثبت‌شده یا پیش از ساعت ۱۳ لازم نیست؛ تاریخچه مستقل دریافت می‌شود."}
        result = self.provider.get_session_candles()
        if not result:
            return self._finish("session_candles", started_at, 0, 0, 1, ["دیده‌بان بازار یا تاریخ روز معاملاتی دریافت نشد"])
        if result["date"] == now.date().isoformat() and now.strftime("%H:%M") < SESSION_FINAL_AFTER:
            return {"collection_type": "session_candles", "status": "skipped", "symbols_succeeded": 0,
                    "symbols_failed": 0, "errors": [], "message": "معاملات امروز هنوز نهایی نشده است (بعد از ساعت ۱۳)."}

        known = {s["ins_code"]: s for s in self.db.get_universe_symbols() if s.get("ins_code")}
        saved = rejected = 0
        for candle in result["candles"]:
            sym = known.get(candle["ins_code"])
            if not sym:
                continue
            df, _ = DataValidator.validate_daily_dataframe(pd.DataFrame([candle])[DAILY_COLUMNS])
            if df.empty:
                rejected += 1
                continue
            self.db.upsert_daily_prices_df(sym["id"], df, recompute=False)
            prev_close = self.db.previous_close_final(sym["id"], candle["date"])
            ref = candle.get("ref_price_raw")
            if prev_close and ref and abs(ref / prev_close - 1.0) > 1e-6:
                self.db.recompute_adjustments(sym["id"])  # روز اقدام شرکتی
            saved += 1
        return self._finish("session_candles", started_at, saved + rejected, saved, rejected, [],
                            {"rows_inserted": saved, "market_date": result["date"]})

    def collect_client_types(self, symbols: Optional[List[str]] = None,
                             progress: Optional[ProgressFn] = None) -> Dict[str, Any]:
        """دریافت تاریخچه روزانه معاملات حقیقی/حقوقی."""
        targets = self.target_symbols(symbols)
        started_at = now_tehran_iso()
        succeeded = failed = rows_saved = 0
        partial_symbols = 0
        errors: List[str] = []
        for i, sym in enumerate(targets, start=1):
            if progress:
                progress(i, len(targets), sym)
            meta = self._resolve(sym)
            dates = self._missing_client_type_dates(meta['id']) if meta else None
            df = self.provider.get_client_types_history(meta["ins_code"], dates=dates) if meta else None
            if df is None or (df.empty and dates != []):
                errors.append(f"عدم دریافت حقیقی/حقوقی «{sym}»")
                failed += 1
                continue
            rows_saved += self.db.upsert_client_types_df(meta["id"], df)
            missing = sorted(set(dates or []) - set(df['date'].dropna().astype(str)))
            if missing:
                failed += 1
                partial_symbols += int(not df.empty)
                errors.append(f"حقیقی/حقوقی «{sym}»: {len(missing)} روز درخواست‌شده دریافت نشد: {', '.join(missing)}")
            else:
                succeeded += 1
        return self._finish("client_types", started_at, len(targets), succeeded, failed, errors,
                            {"rows_inserted": rows_saved, "partial_symbols": partial_symbols})

    # ═══════════════════════════════════════════════
    # اسنپ‌شات لحظه‌ای
    # ═══════════════════════════════════════════════

    def collect_market_snapshot(self, symbols: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        اسنپ‌شات لحظه‌ای کل سهام بازار با یک درخواست؛ فقط نمادهای ثبت‌شده در دیتابیس (یا لیست ورودی)
        ذخیره می‌شوند. اسنپ‌شات نامعتبر ذخیره نمی‌شود و در جدول کیفیت داده ثبت می‌شود.
        """
        started_at = now_tehran_iso()
        snapshots = self.provider.get_market_snapshots()
        if not snapshots:  # پاسخ خالی هم خطاست، نه «موفق با صفر نماد»
            return self._finish("snapshot", started_at, 0, 0, 1, ["دیده‌بان بازار از TSETMC دریافت نشد"])

        wanted = set(symbols) if symbols else None
        known = {s["ins_code"]: s for s in self.db.get_universe_symbols() if s.get("ins_code")}
        rows, errors = [], []
        rejected = unchanged = 0
        for snap in snapshots:
            sym = known.get(snap["ins_code"])
            if not sym or (wanted and sym["symbol_fa"] not in wanted):
                continue
            fingerprint = self._fingerprint(snap)
            if self._last_fingerprints.get(snap["ins_code"]) == fingerprint:
                unchanged += 1
                continue
            is_valid, snap_errors = DataValidator.validate_snapshot(snap)
            if not is_valid:
                rejected += 1
                errors.append(f"{sym['symbol_fa']}: {'; '.join(snap_errors)}")
                self.db.insert_quality_metric(sym["id"], started_at[:10], completeness_score=None,
                                              quality_score=None, notes="; ".join(snap_errors))
                continue
            rows.append((sym["id"], snap))
        saved = self.db.insert_snapshots_bulk(rows)
        # اثر انگشت فقط بعد از ذخیره موفق ثبت می‌شود تا خطای ذخیره، داده بعدی را «بدون تغییر» جا نزند
        for _, snap in rows:
            self._last_fingerprints[snap["ins_code"]] = self._fingerprint(snap)
        return self._finish("snapshot", started_at, saved + rejected, saved, rejected, errors,
                            {"market_rows": len(snapshots), "unchanged_skipped": unchanged,
                             "snapshots": [s for _, s in rows]})

    def collect_snapshots(self, symbols: Optional[List[str]] = None) -> Dict[str, Any]:
        """سازگاری با نسخه قبلی: اسنپ‌شات نمادهای مشخص (از طریق دیده‌بان کل بازار)."""
        targets = self.target_symbols(symbols)
        for sym in targets:
            self._resolve(sym)
        return self.collect_market_snapshot(symbols=targets)

    def run_collection_cycle(self) -> Dict[str, Any]:
        """یک چرخه پایش: فقط در ساعات رسمی بازار اسنپ‌شات گرفته می‌شود."""
        clock = now_tehran()
        if not is_market_open(clock) or not self.db.is_trading_day(clock.date().isoformat()):
            return {
                "status": "skipped", "is_market_open": False, "timestamp": now_tehran_iso(),
                "message": "بازار خارج از ساعات معاملاتی است (شنبه تا چهارشنبه ۹:۰۰ تا ۱۲:۳۰).",
            }
        report = self.collect_market_snapshot()
        report["is_market_open"] = True
        return report

    # ═══════════════════════════════════════════════
    # کدال
    # ═══════════════════════════════════════════════

    def collect_announcements(self, pages: int = 3, symbol_fa: Optional[str] = None) -> Dict[str, Any]:
        """دریافت جدیدترین اطلاعیه‌های کدال؛ فقط اطلاعیه‌های نمادهای داخل دامنه ذخیره می‌شوند."""
        started_at = now_tehran_iso()
        if symbol_fa:
            meta = self._resolve(symbol_fa)
            if not meta:
                return self._finish('codal', started_at, 1, 0, 1, ['نماد دقیق داخل دامنه شناسایی نشد'])
            symbol_fa = meta['symbol_fa']
        if hasattr(self.codal, 'get_latest_report'):
            page_report = dict(self.codal.get_latest_report(pages=pages, symbol_fa=symbol_fa))
            letters = page_report.pop('letters')
        else:
            # رابط قدیمی تأمین‌کننده وضعیت خطا/پایان فهرست را اثبات نمی‌کند.
            letters = self.codal.get_latest(pages=pages, symbol_fa=symbol_fa)
            page_report = dict(errors=[], failed_page=None, pagination_status_unknown=True)
        tracked = {s["symbol_fa"] for s in self.db.get_universe_symbols(include_inactive=bool(symbol_fa))}
        in_scope = [letter for letter in letters if letter["symbol_fa"] in tracked
                    and (not symbol_fa or letter['symbol_fa'] == symbol_fa)]
        inserted = self.db.upsert_announcements(in_scope)
        errors = page_report.pop('errors')
        return self._finish("codal", started_at, len(letters), len(in_scope), int(bool(errors)), errors,
                            dict(new_announcements=inserted, symbol_fa=symbol_fa,
                                 coverage_proven=False, pagination=page_report))

    # ═══════════════════════════════════════════════
    # به‌روزرسانی کامل روزانه
    # ═══════════════════════════════════════════════

    def update_all(self, symbols: Optional[List[str]] = None, full: bool = False,
                   progress: Optional[ProgressFn] = None) -> Dict[str, Dict[str, Any]]:
        """یک دکمه برای همه‌چیز: جامعه سهام ← تاریخچه قیمت ← حقیقی/حقوقی ← کدال."""
        reports: Dict[str, Dict[str, Any]] = {}
        if not symbols and not self.symbols:
            reports["universe"] = self.sync_universe()
            reports["session_candles"] = self.collect_session_candles()
        reports["history"] = self.collect_history(symbols, full=full, progress=progress)
        reports["client_types"] = self.collect_client_types(symbols, progress=progress)
        requested = symbols or self.symbols
        if requested:
            # درخواست «فقط قاسم» نباید به سه صفحه آخر کل بازار تبدیل شود.
            parts = [self.collect_announcements(pages=3, symbol_fa=s) for s in dict.fromkeys(requested)]
            reports['codal'] = self._finish('codal_targeted', now_tehran_iso(), len(parts),
                sum(r['status'] == 'success' for r in parts), sum(r['status'] != 'success' for r in parts),
                [e for r in parts for e in r['errors']], dict(per_symbol=parts, coverage_proven=False))
        else:
            reports["codal"] = self.collect_announcements(pages=3)
        return reports

    def get_collection_report(self) -> Dict[str, Any]:
        logs = self.db.get_collection_stats()
        return {
            "total_runs_logged": len(logs),
            "success_runs": sum(1 for log in logs if log.get("status") == "success"),
            "failed_runs": sum(1 for log in logs if log.get("status") == "failed"),
            "partial_runs": sum(1 for log in logs if log.get("status") == "partial"),
            "recent_logs": logs[:5],
            "monitored_symbols": self.target_symbols(),
        }
