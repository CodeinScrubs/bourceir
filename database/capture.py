"""ثبت مستقل و فقط‌افزودنیِ دامنه پایش و شروع/نتیجه دریافت‌ها."""

import hashlib
import json
import uuid

from config import settings
from core.text import normalize_symbol
from core.timeutils import now_tehran_iso

CAPTURE_VERSION = 'capture-protocol-v1'
CAPTURE_FILES = ('brs.py', 'data_collector/scheduler.py', 'data_collector/tsetmc.py',
                 'data_collector/validator.py', 'database/capture.py', 'database/db.py',
                 'database/migrations/011_capture_evidence.sql', 'core/timeutils.py',
                 'ops/capture_audit.py', 'ops/intraday_audit.py', 'config/settings.py', 'requirements.lock.txt')


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def capture_code_identity():
    """هش محتوا به checkout پاک یا نصب Git وابسته نیست و شامل رمز/نشست نیست."""
    files = {name: hashlib.sha256((settings.BASE_DIR / name).read_bytes().replace(b'\r\n', b'\n')).hexdigest()
             for name in CAPTURE_FILES}
    return dict(files=files, sha256=hashlib.sha256(canonical_json(files).encode()).hexdigest())


class CaptureRepository:
    def __init__(self, db):
        self.db = db

    def start(self, instruments, *, interval, cycles, forced, provider, explicit_scope):
        if isinstance(interval, bool) or not isinstance(interval, int) or interval <= 0:
            raise ValueError('فاصله دریافت باید عدد صحیح مثبت باشد')
        if isinstance(cycles, bool) or not isinstance(cycles, int) or cycles < 0:
            raise ValueError('تعداد چرخه باید عدد صحیح نامنفی باشد')
        scope = [dict(symbol_id=row['id'], symbol_fa=normalize_symbol(row['symbol_fa']),
                      ins_code=str(row['ins_code']) if row.get('ins_code') is not None else '',
                      instrument_type_code=row['instrument_type_code'])
                 for row in instruments]
        if (not scope or any(not r['symbol_fa'] or not r['ins_code'] or r['instrument_type_code'] not in (300, 303)
                             for r in scope)
                or len({r['ins_code'] for r in scope}) != len(scope)
                or len({r['symbol_fa'] for r in scope}) != len(scope)):
            raise ValueError('دامنه پایش باید سهام عادی با نام و شناسه یکتا باشد')
        scope.sort(key=lambda row: row['symbol_fa'])
        protocol = dict(version=CAPTURE_VERSION, project_version=settings.VERSION, provider=provider,
                        interval_seconds=interval, planned_cycles=cycles, forced=bool(forced),
                        scope_kind='explicit' if explicit_scope else 'current_active_metadata',
                        instruments=scope, trading_hours=dict(settings.TRADING_HOURS),
                        trading_days=list(settings.TRADING_DAYS), calendar=self.db.get_market_calendar(),
                        code=capture_code_identity(), decisions_preregistered=False,
                        trading_state_feed_available=False, independent_book_clock_available=False)
        text = canonical_json(protocol)
        run_id = 'CAP-' + uuid.uuid4().hex
        with self.db.conn:
            self.db.conn.execute('INSERT INTO capture_runs VALUES(?,?,?,?)',
                                 (run_id, now_tehran_iso(), hashlib.sha256(text.encode()).hexdigest(), text))
        return run_id

    def append(self, run_id, cycle, event, payload=None):
        """شروع پیش از درخواست commit می‌شود؛ پایان گمشده بعد از crash نامعلوم می‌ماند."""
        payload = {} if payload is None else payload
        if not isinstance(payload, dict):
            raise ValueError('محتوای رویداد پایش باید شیء باشد')
        if event not in ('poll_started', 'poll_finished', 'market_closed', 'run_stopped'):
            raise ValueError('نوع رویداد پایش نامعتبر است')
        if event == 'run_stopped' and payload.get('reason') not in ('cycles_completed', 'user_interrupted', 'error'):
            raise ValueError('علت پایان پایش نامعتبر است')
        if event == 'poll_finished' and payload.get('status') not in ('success', 'partial', 'failed', 'error'):
            raise ValueError('وضعیت دریافت نامعتبر است')
        text = canonical_json(payload)
        if isinstance(cycle, bool) or not isinstance(cycle, int):
            raise ValueError('شماره چرخه باید عدد صحیح باشد')
        conn = self.db.conn
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            if not conn.execute('SELECT 1 FROM capture_runs WHERE id=?', (run_id,)).fetchone():
                raise ValueError('پروتکل پایش ثبت نشده است')
            if conn.execute("SELECT 1 FROM capture_events WHERE run_id=? AND event='run_stopped'", (run_id,)).fetchone():
                raise ValueError('پایش پایان یافته است؛ اجرای تازه لازم است')
            events = {r[0] for r in conn.execute('SELECT event FROM capture_events WHERE run_id=? AND cycle=?',
                                                (run_id, cycle))}
            if event == 'poll_finished' and 'poll_started' not in events:
                raise ValueError('نتیجه بدون ثبت مستقل شروع دریافت پذیرفته نمی‌شود')
            if event in ('poll_started', 'market_closed') and events:
                raise ValueError('چرخه پایش قبلاً ثبت شده است')
            if event in ('poll_started', 'market_closed'):
                previous = conn.execute('SELECT COALESCE(MAX(cycle),0) FROM capture_events WHERE run_id=?',
                                        (run_id,)).fetchone()[0]
                if cycle != previous + 1:
                    raise ValueError('شماره چرخه باید پیوسته باشد؛ شروع مجدد، اجرای تازه لازم دارد')
            conn.execute('INSERT INTO capture_events(run_id,cycle,event,recorded_at,payload_json) VALUES(?,?,?,?,?)',
                         (run_id, cycle, event, now_tehran_iso(), text))
