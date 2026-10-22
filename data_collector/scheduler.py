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
from collections import Counter, defaultdict
from datetime import date
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from config import settings
from core.client_quality import validated_client_data
from core.daily_publications import publication_request
from core.text import normalize_symbol
from core.timeutils import is_canonical_iso_date, is_market_open, now_tehran, now_tehran_iso
from data_collector.base import DAILY_COLUMNS, BaseDataProvider
from data_collector.codal import CodalProvider
from data_collector.daily_publications import DailyPublicationArchive
from data_collector.tsetmc import TsetmcProvider
from data_collector.universe import UniverseManager
from data_collector.validator import DataValidator
from database.capture import bind_collected_snapshot
from database.db import DatabaseManager

logger = logging.getLogger(__name__)

# اگر فاصله آخرین داده موجود بیشتر از این تعداد روز باشد، تاریخچه کامل دوباره دریافت می‌شود
INCREMENTAL_LOOKBACK_ROWS = 30
# دریافت کندل روز جاری زودتر از این ساعت مجاز نیست؛ پایان انتشار مستقل اثبات نمی‌شود.
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
                 provider: Optional[BaseDataProvider] = None, codal: Optional[CodalProvider] = None,
                 publication_provider_factory=None):
        self.db = db if db is not None else DatabaseManager()
        self.provider = provider or TsetmcProvider()
        self.codal = codal or CodalProvider()
        self.universe = UniverseManager(db=self.db, provider=self.provider)
        self.symbols = list(symbols) if symbols else None
        self.publication_provider_factory = (publication_provider_factory if publication_provider_factory is not None
            else (lambda: TsetmcProvider(realtime=True)) if provider is None else None)
        # اثر انگشت آخرین اسنپ‌شات هر نماد؛ اسنپ‌شات بدون تغییر دوباره ذخیره نمی‌شود
        self._last_fingerprints: Dict[str, tuple] = {}
        self._last_snapshot_times: Dict[str, str] = {}

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
        status = "success" if failed == 0 else ("partial" if succeeded > 0 or (extra or {}).get("partial_symbols")
                                              or (extra or {}).get('valid_observations') else "failed")
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
        missing = result.get('missing_from_response', 0)
        if missing:
            errors.append(f'{missing} نماد فعال محلی در پاسخ دیده نشد؛ علت غیبت نامعلوم و وضعیت قبلی محفوظ است')
        return self._finish("universe", started_at, result["received"] + missing, result["saved"],
                            result["failed"] + missing + (0 if result["received"] else 1), errors, result)

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

    def collect_session_candles(self, symbols: Optional[List[str]] = None) -> Dict[str, Any]:
        """پس از ساعت ۱۳ روز معاملاتی، دریافت تاریخ‌دار برای دامنه انتخاب‌شده.

        منبع فاقد قابلیت بومی از قرارداد دیده‌بان قبلی استفاده می‌کند. هیچ‌یک،
        پایان انتشار یا کامل‌بودن مستقل جلسه را صرفاً از ساعت محلی نتیجه نمی‌گیرند.
        """
        started_at = now_tehran_iso()
        now = now_tehran()
        if not self.db.is_trading_day(now.date().isoformat()) or now.strftime('%H:%M') < SESSION_FINAL_AFTER:
            return {"collection_type": "session_candles", "status": "skipped", "symbols_succeeded": 0,
                    "symbols_failed": 0, "errors": [],
                    "message": "کندل امروز در روز تعطیل ثبت‌شده یا پیش از ساعت ۱۳ دریافت نمی‌شود؛ تاریخچه مستقل دریافت می‌شود."}
        if getattr(self.provider, 'supports_dated_publications', False):
            return self._collect_published_prices(now.date().isoformat(), started_at, symbols)
        result = self.provider.get_session_candles()
        if not result:
            return self._finish("session_candles", started_at, 0, 0, 1, ["دیده‌بان بازار یا تاریخ روز معاملاتی دریافت نشد"])
        if result["date"] == now.date().isoformat() and now.strftime("%H:%M") < SESSION_FINAL_AFTER:
            return {"collection_type": "session_candles", "status": "skipped", "symbols_succeeded": 0,
                    "symbols_failed": 0, "errors": [], "message": "معاملات امروز هنوز نهایی نشده است (بعد از ساعت ۱۳)."}

        selected = symbols or self.symbols
        scope = set(normalize_symbol(s) for s in selected) if selected else None
        known = {s["ins_code"]: s for s in self.db.get_universe_symbols(include_inactive=bool(scope))
                 if s.get("ins_code") and (scope is None or normalize_symbol(s['symbol_fa']) in scope)}
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
        if getattr(self.provider, 'supports_dated_publications', False):
            return self._collect_published_clients(symbols, progress)
        return self._collect_client_types_legacy(symbols, progress)

    def _publication_capture(self, tasks, progress=None):
        if self.db.conn.in_transaction:
            raise ValueError('دریافت شبکه روزانه داخل تراکنش باز مجاز نیست')
        root = self.db.db_path.resolve().parent / 'private' / 'daily-publications'
        archive = DailyPublicationArchive(root, tasks, workers=3 if self.publication_provider_factory else 1)
        records = archive.capture(self.provider, provider_factory=self.publication_provider_factory, progress=progress)
        return archive, records

    @staticmethod
    def _publication_task(meta, day, kind):
        if (type(meta.get('instrument_type_code')) is not int or meta['instrument_type_code'] not in (300, 303)
                or type(meta.get('id')) is not int or meta['id'] <= 0 or not meta.get('ins_code')
                or not is_canonical_iso_date(day) or day > now_tehran().date().isoformat()):
            raise ValueError('منبع بومی روزانه فقط سهام عادی بورس و فرابورس را می‌پذیرد')
        code, _ = publication_request(meta['ins_code'], day, kind)
        symbol = normalize_symbol(meta['symbol_fa'])
        if not symbol or symbol[-1:].isdigit():
            raise ValueError('نام سهم عادی لازم است')
        return dict(symbol_id=meta['id'], symbol=symbol, ins_code=code,
                    instrument_type_code=meta['instrument_type_code'], date=day, kind=kind)

    def _collect_published_prices(self, day, started_at, symbols=None):
        selected = symbols or self.symbols
        known = (list(dict.fromkeys(normalize_symbol(s) for s in selected))
                 if selected else self.db.get_universe_symbols())
        if not known:
            return self._finish('session_candles', started_at, 0, 0, 1, ['جامعه ثبت‌شده‌ای برای قیمت بومی وجود ندارد'],
                                dict(native_date_and_identity_required=True))
        tasks, preparation_errors = [], []
        for item in known:
            try:
                meta = self._resolve(item) if selected else item
                if meta is None:
                    raise ValueError('هویت سهم انتخاب‌شده شناسایی نشد')
                if selected and normalize_symbol(meta['symbol_fa']) != item:
                    raise ValueError('نام سهم شناسایی‌شده با درخواست برابر نیست')
                tasks.append(self._publication_task(meta, day, 'price'))
            except (ValueError, KeyError, TypeError) as exc:
                preparation_errors.append(f"{item if selected else item.get('symbol_fa', 'نماد انتخاب‌شده')}: {exc}")
        if not tasks:
            return self._finish('session_candles', started_at, len(known), 0, len(known), preparation_errors,
                                dict(native_date_and_identity_required=True))
        archive, records = self._publication_capture(tasks)
        saved = zero = 0
        failed = len(preparation_errors)
        errors, application = preparation_errors, []
        for record in records:
            task, parsed = record['task'], record['parsed']
            outcome = dict(symbol_id=task['symbol_id'], date=day, stored=False, status=parsed['status'])
            try:
                if parsed['status'] == 'valid_price':
                    self.db.upsert_daily_prices_df(task['symbol_id'], pd.DataFrame([parsed['row']])[DAILY_COLUMNS])
                    saved += 1
                    outcome['stored'] = True
                elif parsed['status'] == 'observed_zero_activity':
                    previous = self.db.conn.execute('''SELECT volume FROM daily_prices
                        WHERE symbol_id=? AND date=? AND source='tsetmc' ''', (task['symbol_id'], day)).fetchone()
                    if previous and previous['volume'] and previous['volume'] > 0:
                        raise ValueError('گزارش صفر با کندل دارای معامله موجود تعارض دارد؛ حذف خودکار مجاز نیست')
                    zero += 1
                else:
                    raise ValueError(parsed.get('error', parsed['status']))
            except Exception as exc:
                failed += 1
                outcome.update(status='not_applied', error_type=type(exc).__name__)
                errors.append(f"{task['symbol']}: {exc}")
            application.append(outcome)
        path = archive.finish(dict(rows_stored=saved, zero_activity_observations=zero, failed=failed, records=application))
        return self._finish('session_candles', started_at, len(known), saved+zero, failed, errors,
            dict(rows_inserted=saved, market_date=day, zero_activity_observations=zero,
                 zero_activity_full_session_verified=False, native_date_and_identity_required=True,
                 evidence_path=path, full_session_coverage_verified=False))

    def _collect_published_clients(self, symbols, progress):
        started_at = now_tehran_iso()
        targets = list(dict.fromkeys(normalize_symbol(s) for s in self.target_symbols(symbols)))
        resolved, failed, errors, tasks = {}, set(), [], []
        stored, bootstrap, applied = {}, [], []
        for symbol in targets:
            try:
                meta = self._resolve(symbol)
                if not meta:
                    raise ValueError('هویت سهم داخل دامنه شناسایی نشد')
                if normalize_symbol(meta['symbol_fa']) != symbol:
                    raise ValueError('نام سهم شناسایی‌شده با درخواست برابر نیست')
                self._publication_task(meta, now_tehran().date().isoformat(), 'client')
                resolved[meta['id']] = meta
                dates = self._missing_client_type_dates(meta['id'])
                if dates is None:
                    # Initial full histories retain the legacy provider contract. The recent
                    # sessions are independently refreshed through byte-bound native receipts.
                    df = self.provider.get_client_types_history(meta['ins_code'], dates=None)
                    if df is None or df.empty:
                        failed.add(symbol)
                        errors.append(f'{symbol}: تاریخچه اولیه حقیقی/حقوقی دریافت نشد')
                    else:
                        stored[symbol] = self.db.upsert_client_types_df(meta['id'], df)
                    bootstrap.append(symbol)
                    dates = [r['date'] for r in self.db.conn.execute('''SELECT date FROM daily_prices
                        WHERE symbol_id=? AND source='tsetmc' AND date<=? ORDER BY date DESC LIMIT ?''',
                        (meta['id'], now_tehran().date().isoformat(), CLIENT_TYPE_REFRESH_SESSIONS))]
                tasks.extend(self._publication_task(meta, day, 'client') for day in dates)
            except Exception as exc:
                logger.exception('آماده‌سازی آمار روزانه %s ناموفق بود', symbol)
                failed.add(symbol)
                errors.append(f'{symbol}: {exc}')
        archive, records = self._publication_capture(tasks, progress) if tasks else (None, [])
        grouped = defaultdict(list)
        for record in records:
            task, parsed = record['task'], record['parsed']
            if parsed['status'] == 'observed_client':
                grouped[task['symbol_id']].append(parsed['row'])
            else:
                failed.add(task['symbol'])
                errors.append(f"{task['symbol']} {task['date']}: {parsed.get('error', parsed['status'])}")
        coherent = unknown = 0
        for sid, rows in grouped.items():
            meta = resolved[sid]
            symbol = meta['symbol_fa']
            try:
                df = pd.DataFrame(rows)
                dates = set(df['date'])
                prices = self.db.get_daily_prices_df(symbol, start_date=min(dates), end_date=max(dates))
                clean = validated_client_data(prices, df)
                pairs = clean.buy_i_value.notna() & clean.sell_i_value.notna() & clean.date.isin(dates)
                known = int(pairs.sum())
                missing = len(rows)-known
                # Preserve observed raw values, including honest None; the shared feature
                # validator masks incoherent components. An incomplete run is never success.
                count = self.db.upsert_client_types_df(sid, df)
                stored[symbol] = stored.get(symbol, 0)+count
                coherent += known
                unknown += missing
                if missing:
                    failed.add(symbol)
                    errors.append(f'{symbol}: {missing} زوج ارزش حقیقی نامعلوم یا ناسازگار با قیمت روز')
                applied.append(dict(symbol_id=sid, dates=sorted(dates), rows_stored=count,
                                    coherent_retail_pairs=known, unknown_retail_pairs=missing))
            except Exception as exc:
                logger.exception('ورود آمار بومی روزانه %s ناموفق بود', symbol)
                failed.add(symbol)
                errors.append(f'{symbol}: {exc}')
        path = archive.finish(dict(records=applied, coherent_retail_pairs=coherent, unknown_retail_pairs=unknown,
            failed_symbols=sorted(failed), bootstrap_history_without_byte_receipts=bootstrap)) if archive else None
        return self._finish('client_types', started_at, len(targets), len(set(targets)-failed), len(failed), errors,
            dict(rows_inserted=sum(stored.values()), partial_symbols=sum(s in failed for s in stored),
                 coherent_retail_pairs=coherent, unknown_retail_pairs=unknown, evidence_path=path,
                 native_date_and_identity_required=True, bootstrap_history_without_byte_receipts=bootstrap))

    def _collect_client_types_legacy(self, symbols: Optional[List[str]] = None,
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

    def collect_market_snapshot(self, symbols: Optional[List[str]] = None, *, instruments=None,
                                quote_source='market-watch') -> Dict[str, Any]:
        """
        اسنپ‌شات لحظه‌ای کل سهام بازار با یک درخواست؛ فقط نمادهای ثبت‌شده در دیتابیس (یا لیست ورودی)
        ذخیره می‌شوند. اسنپ‌شات نامعتبر ذخیره نمی‌شود و در جدول کیفیت داده ثبت می‌شود.
        """
        started_at = now_tehran_iso()
        wanted = {normalize_symbol(s) for s in symbols} if symbols else None
        scope = instruments if instruments is not None else self.db.get_universe_symbols(include_inactive=bool(wanted))
        known = {str(s['ins_code']): s for s in scope
                 if s.get('ins_code') and (not wanted or s['symbol_fa'] in wanted)}
        unresolved = wanted - {s['symbol_fa'] for s in known.values()} if wanted else set()
        from core.quote_context import MAX_INSTRUMENTS, QUOTE_SOURCES, archive_source, parse_source

        if quote_source not in QUOTE_SOURCES:
            raise ValueError('منبع مظنه شناخته‌شده نیست')
        source_records, source_errors = {}, {}
        observations, errors, rows = [], [], []
        if quote_source != 'market-watch':
            if not (wanted or instruments is not None) or not 1 <= len(known) + len(unresolved) <= MAX_INSTRUMENTS:
                raise ValueError('منبع تاریخ‌دار به دامنه صریح حداکثر ده نماد نیاز دارد')
            if any(meta.get('instrument_type_code') not in (300, 303) for meta in known.values()):
                raise ValueError('دامنه منبع تاریخ‌دار فقط سهام عادی بورس و فرابورس است')
            snapshots = []
            for code, meta in known.items():
                try:
                    source = (self.provider.get_legacy_quote_observation(code) if quote_source == 'dated-quote'
                              else self.provider.get_quote_context_observation(code))
                    if source is not None:
                        source_records[code] = archive_source(source, quote_source)
                        if source.get('ins_code') != code:
                            raise ValueError('شناسه پاسخ با دامنه درخواست ناسازگار است')
                        snapshots.append(parse_source(source, meta['symbol_fa'], quote_source))
                except (ValueError, KeyError, TypeError, NotImplementedError) as exc:
                    source_errors[code] = type(exc).__name__
        else:
            snapshots = self.provider.get_market_snapshots()
        grouped = defaultdict(list)
        for snap in snapshots or []:
            grouped[str(snap.get('ins_code'))].append(snap)
        for ins_code, sym in known.items():
            matches = grouped[ins_code]
            item = dict(ins_code=ins_code, status='missing', provider_timestamp=None, captured_at=None)
            observations.append(item)
            if quote_source != 'market-watch':
                item['source_observation'] = source_records.get(ins_code)
                if ins_code in source_errors:
                    item.update(status='rejected', source_error=source_errors[ins_code])
                    errors.append(f"{sym['symbol_fa']}: ساختار یا تاریخ مظنه تک‌نماد تأیید نشد")
                    continue
            if not matches:
                errors.append(f"{sym['symbol_fa']}: در پاسخ منبع موجود نیست؛ علت توقف/حذف معلوم نیست")
                continue
            if len(matches) > 1:
                item['status'] = 'duplicate'
                errors.append(f"{sym['symbol_fa']}: شناسه تکراری در پاسخ دیده‌بان")
                continue
            snap = matches[0]
            is_valid, snap_errors = DataValidator.validate_snapshot(snap)
            if not is_valid:
                item['status'] = 'rejected'
                errors.append(f"{sym['symbol_fa']}: {'; '.join(snap_errors)}")
                self.db.insert_quality_metric(sym["id"], started_at[:10], completeness_score=None,
                                              quality_score=None, notes="; ".join(snap_errors))
                continue
            item.update(provider_timestamp=snap.get('provider_timestamp'), captured_at=snap.get('captured_at'))
            if self._last_fingerprints.get(ins_code) == self._fingerprint(snap):
                item['status'] = 'unchanged'
                continue
            item['status'] = 'changed'
            rows.append((sym["id"], snap))
        saved = self.db.insert_snapshots_bulk(rows)
        # دریافت بی‌تغییر به ردیف اصلی وصل می‌شود؛ ساعت ذخیره قبلی بازنویسی نمی‌شود.
        with self.db.conn:
            self.db.conn.execute('BEGIN')
            for item in observations:
                if item['status'] not in ('changed', 'unchanged'):
                    continue
                ins_code = item['ins_code']
                snap = grouped[ins_code][0]
                stored_at = snap['captured_at'] if item['status'] == 'changed' else self._last_snapshot_times.get(ins_code)
                item['snapshot_binding'] = bind_collected_snapshot(self.db, known[ins_code]['id'], snap, stored_at)
                if item['snapshot_binding'] is None:
                    self._last_fingerprints.pop(ins_code, None)
                    self._last_snapshot_times.pop(ins_code, None)
                    errors.append(f"{known[ins_code]['symbol_fa']}: تطبیق محتوای پاسخ با ردیف ذخیره‌شده نامعلوم است")
                else:
                    # فقط ذخیره موفق و تطبیق‌یافته در حافظه می‌ماند؛ خطای نوشتن باعث حذف دریافت بعدی نمی‌شود.
                    self._last_fingerprints[ins_code] = self._fingerprint(snap)
                    self._last_snapshot_times[ins_code] = stored_at
        counts = Counter(item['status'] for item in observations)
        valid = counts['changed'] + counts['unchanged']
        unbound = sum(item['status'] in ('changed', 'unchanged') and item.get('snapshot_binding') is None
                      for item in observations)
        failed = counts['missing'] + counts['rejected'] + counts['duplicate'] + len(unresolved) + unbound
        if not snapshots:
            errors.insert(0, 'دیده‌بان بازار از TSETMC دریافت نشد')
        errors.extend(f'{name}: نماد در دامنه شناخته‌شده نیست' for name in sorted(unresolved))
        if not known and not unresolved:
            errors.append('دامنه پایش سهام عادی خالی است')
        coverage = dict(expected=len(known) + len(unresolved), valid=valid, changed=counts['changed'],
                        unchanged=counts['unchanged'], missing=counts['missing'] + len(unresolved),
                        rejected=counts['rejected'], duplicate=counts['duplicate'],
                        unresolved_symbols=sorted(unresolved), per_instrument=observations)
        if quote_source != 'market-watch':
            coverage['quote_source'] = quote_source
        return self._finish("snapshot", started_at, coverage['expected'], saved, failed or (0 if known else 1), errors,
                            {"market_rows": len(snapshots or []), "unchanged_skipped": counts['unchanged'],
                             "valid_observations": valid, 'coverage': coverage, "snapshots": [s for _, s in rows]})

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
        """جامعه سهام ← تاریخچه قیمت ← قیمت بومی امروز ← حقیقی/حقوقی ← کدال."""
        reports: Dict[str, Dict[str, Any]] = {}
        if not symbols and not self.symbols:
            reports["universe"] = self.sync_universe()
        reports["history"] = self.collect_history(symbols, full=full, progress=progress)
        reports["session_candles"] = self.collect_session_candles(symbols)
        reports["client_types"] = self.collect_client_types(symbols, progress=progress)
        requested = symbols or self.symbols
        if requested:
            # درخواست «فقط قاسم» نباید به سه صفحه آخر کل بازار تبدیل شود.
            codal_started_at = now_tehran_iso()
            parts = [self.collect_announcements(pages=3, symbol_fa=s) for s in dict.fromkeys(requested)]
            reports['codal'] = self._finish('codal_targeted', codal_started_at, len(parts),
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
