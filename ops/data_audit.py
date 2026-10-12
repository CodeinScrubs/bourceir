"""بازبینی فقط‌خواندنی آرشیو واقعی؛ شمار خطاها، بدون امتیاز یا سود ساختگی."""

import json
from datetime import date
from pathlib import Path

from config import settings
from core.timeutils import now_tehran_iso
from database.db import DatabaseManager


def audit_database(db: DatabaseManager) -> dict:
    """یک تصویر ثابت از دیتابیس؛ فایل خروجی حاوی هیچ کلید API یا رمز نیست."""
    own_snapshot = not db.conn.in_transaction
    if own_snapshot:
        db.conn.execute('BEGIN')
    try:
        daily = dict(db.conn.execute('''
            SELECT COUNT(*) AS rows, MIN(date) AS date_min, MAX(date) AS date_max,
              SUM(CASE WHEN high_raw IS NULL OR low_raw IS NULL OR high_raw <= 0 OR low_raw <= 0
                   OR high_raw < low_raw OR ABS(high_raw) >= 1e308 OR ABS(low_raw) >= 1e308 THEN 1 ELSE 0 END) AS invalid_range,
              SUM(CASE WHEN close_final_raw IS NULL OR close_final_raw <= 0 OR ABS(close_final_raw) >= 1e308
                   THEN 1 ELSE 0 END) AS unknown_official_close,
              SUM(CASE WHEN volume IS NULL THEN 1 ELSE 0 END) AS unknown_volume,
              SUM(CASE WHEN volume < 0 OR volume != CAST(volume AS INTEGER) OR ABS(volume) >= 1e308 THEN 1 ELSE 0 END) AS invalid_volume,
              SUM(CASE WHEN value IS NULL THEN 1 ELSE 0 END) AS unknown_value,
              SUM(CASE WHEN value < 0 OR ABS(value) >= 1e308 THEN 1 ELSE 0 END) AS invalid_value,
              SUM(CASE WHEN volume > 0 AND (value IS NULL OR value <= 0 OR ABS(value) >= 1e308)
                   THEN 1 ELSE 0 END) AS unknown_turnover_on_traded_day,
              SUM(CASE WHEN ref_price_raw IS NULL OR ref_price_raw <= 0 OR ABS(ref_price_raw) >= 1e308
                   THEN 1 ELSE 0 END) AS unknown_reference,
              SUM(CASE WHEN volume > 0 AND value > 0 AND low_raw > 0 AND high_raw >= low_raw
                   AND (value / volume < low_raw - 1e-6 * high_raw OR value / volume > high_raw + 1e-6 * high_raw)
                   THEN 1 ELSE 0 END) AS vwap_outside_range
            FROM daily_prices WHERE source = 'tsetmc';
        ''').fetchone())
        daily['invalid_date_rows'] = 0
        daily['future_date_rows'] = 0
        today = now_tehran_iso()[:10]
        for row in db.conn.execute("SELECT date, COUNT(*) AS n FROM daily_prices WHERE source='tsetmc' GROUP BY date"):
            try:
                parsed = date.fromisoformat(row['date'])
                if parsed.isoformat() != row['date']:
                    raise ValueError('noncanonical date')
            except (TypeError, ValueError):
                daily['invalid_date_rows'] += row['n']
                continue
            if row['date'] > today:
                daily['future_date_rows'] += row['n']
        snapshots = dict(db.conn.execute('''
            SELECT COUNT(*) AS rows, MAX(captured_at) AS latest_capture,
              SUM(CASE WHEN provider_timestamp IS NULL THEN 1 ELSE 0 END) AS unknown_source_time,
              SUM(CASE WHEN provider_timestamp IS NOT NULL AND julianday(provider_timestamp) IS NULL
                   THEN 1 ELSE 0 END) AS unparseable_source_time,
              SUM(CASE WHEN julianday(provider_timestamp) > julianday(captured_at) + 5.0 / 86400
                   THEN 1 ELSE 0 END) AS source_time_after_capture
            FROM snapshots;
        ''').fetchone())
        integrity = db.integrity_check()
        foreign_keys = len(db.conn.execute('PRAGMA foreign_key_check').fetchall())
        symbols = db.get_all_symbols(include_inactive=True)
        universe = db.get_universe_symbols()
    finally:
        if own_snapshot:
            db.conn.rollback()
    issue_keys = ('invalid_range', 'unknown_official_close', 'unknown_volume', 'invalid_volume', 'unknown_value',
                  'invalid_value', 'unknown_turnover_on_traded_day', 'unknown_reference', 'vwap_outside_range',
                  'invalid_date_rows', 'future_date_rows')
    needs_review = not daily['rows'] or any(daily.get(k) for k in issue_keys)
    needs_review |= bool(snapshots['unparseable_source_time'] or snapshots['source_time_after_capture'])
    overall = 'error' if integrity != 'ok' or foreign_keys else ('warn' if needs_review else 'ok')
    return {'checked_at':now_tehran_iso(), 'version':settings.VERSION, 'overall':overall,
            'integrity':integrity, 'foreign_key_violations':foreign_keys,
            'symbols':{'all_registered':len(symbols), 'current_in_scope':len(universe)},
            'daily_prices':daily, 'snapshots':snapshots,
            'interpretation':'Counts require review; first archived reference may be unknown. '
                             'Current scope is not historical membership. This report does not validate profitability.'}


def run_audit(output: Path | str) -> dict:
    """بازکردن فقط‌خواندنی؛ دیتابیس گمشده ساخته نمی‌شود و مایگریشن اجرا نمی‌شود."""
    try:
        with DatabaseManager(read_only=True) as db:
            report = audit_database(db)
    except Exception as exc:
        report = {'checked_at':now_tehran_iso(), 'version':settings.VERSION,
                  'overall':'error', 'error_type':type(exc).__name__}
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    return report
