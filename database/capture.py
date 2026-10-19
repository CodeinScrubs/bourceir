"""ثبت مستقل و فقط‌افزودنیِ دامنه پایش و شروع/نتیجه دریافت‌ها."""

import hashlib
import json
import uuid

from config import settings
from core.capture_evidence import CAPTURE_VERSION, snapshot_binding, snapshot_sha256
from core.quote_context import MAX_INSTRUMENTS, QUOTE_SOURCES, SOURCE_CONTRACTS
from core.text import normalize_symbol
from core.timeutils import now_tehran_iso

CAPTURE_FILES = ('brs.py', 'data_collector/scheduler.py', 'data_collector/tsetmc.py',
                 'data_collector/validator.py', 'database/capture.py', 'database/db.py',
                 'database/migrations/011_capture_evidence.sql', 'core/timeutils.py',
                 'ops/capture_audit.py', 'ops/intraday_audit.py', 'config/settings.py', 'requirements.lock.txt',
                 'core/capture_evidence.py', 'core/legacy_quotes.py', 'data_collector/base.py', 'core/quote_context.py')


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def capture_code_identity(*, sources=None):
    """هش محتوا به checkout پاک یا نصب Git وابسته نیست و شامل رمز/نشست نیست."""
    files = {name: hashlib.sha256(sources[name].encode() if sources is not None else
                                 (settings.BASE_DIR / name).read_bytes().replace(b'\r\n', b'\n')).hexdigest()
             for name in CAPTURE_FILES}
    return dict(files=files, sha256=hashlib.sha256(canonical_json(files).encode()).hexdigest())


def bind_collected_snapshot(db, symbol_id, snapshot, stored_at):
    """پیوند فقط پس از تطبیق همه ستون‌های پاسخ با ردیف ذخیره‌شده صادر می‌شود."""
    from database.db import SNAPSHOT_COLUMNS

    expected = dict(zip(['symbol_id'] + list(SNAPSHOT_COLUMNS), db._snapshot_params(symbol_id, snapshot)))
    row = db.conn.execute('SELECT * FROM snapshots WHERE symbol_id=? AND captured_at=? AND source=?',
                          (symbol_id, stored_at, expected['source'])).fetchone()
    if row is None:
        return None
    stored = dict(row)
    content = {k: v for k, v in stored.items() if k not in ('id', 'captured_at')}
    expected.pop('captured_at')
    return snapshot_binding(stored) if snapshot_sha256(content) == snapshot_sha256(expected) else None


class CaptureRepository:
    def __init__(self, db):
        self.db = db

    def latest_cycles(self, symbol_ids):
        """ترتیب شناسه، مستقل از ساعت؛ ثبت با روز ناسازگار باعث بازگشت به دریافت قدیمی نمی‌شود."""
        selected, cycles = {}, {}
        rows = self.db.conn.execute("SELECT run_id,MAX(id) AS id FROM capture_events "
                                    "WHERE event='poll_finished' GROUP BY run_id")
        for row in rows.fetchall():
            run = dict(self.db.conn.execute('SELECT * FROM capture_runs WHERE id=?', (row['run_id'],)).fetchone())
            scope = {item['symbol_id'] for item in json.loads(run['protocol_json'])['instruments']}
            wanted = scope.intersection(symbol_ids)
            if not wanted:
                continue
            finished = dict(self.db.conn.execute('SELECT * FROM capture_events WHERE id=?', (row['id'],)).fetchone())
            started = self.db.conn.execute("SELECT * FROM capture_events WHERE run_id=? AND cycle=? AND event='poll_started'",
                                           (row['run_id'], finished['cycle'])).fetchone()
            if started is None:
                raise ValueError('پایان دریافت بدون شاهد شروع ثبت شده است')
            cycles[row['id']] = dict(run=run, started=dict(started), finished=finished)
            for sid in wanted:
                selected[sid] = max(selected.get(sid, 0), row['id'])
        return [cycles[key] for key in sorted(set(selected.values()))]

    def start(self, instruments, *, interval, cycles, forced, provider, explicit_scope, quote_source='market-watch'):
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
                or len({r['symbol_fa'] for r in scope}) != len(scope)
                or len({r['symbol_id'] for r in scope}) != len(scope)
                or any(isinstance(r['symbol_id'], bool) or not isinstance(r['symbol_id'], int) or r['symbol_id'] <= 0
                       for r in scope)):
            raise ValueError('دامنه پایش باید سهام عادی با نام و شناسه یکتا باشد')
        scope.sort(key=lambda row: row['symbol_fa'])
        if quote_source not in QUOTE_SOURCES or (quote_source != 'market-watch' and
                (not explicit_scope or len(scope) > MAX_INSTRUMENTS)):
            raise ValueError('منبع تاریخ‌دار به دامنه صریح حداکثر ده نماد نیاز دارد')
        protocol = dict(version=CAPTURE_VERSION, project_version=settings.VERSION, provider=provider,
                        interval_seconds=interval, planned_cycles=cycles, forced=bool(forced),
                        scope_kind='explicit' if explicit_scope else 'current_active_metadata',
                        instruments=scope, trading_hours=dict(settings.TRADING_HOURS),
                        trading_days=list(settings.TRADING_DAYS), calendar=self.db.get_market_calendar(),
                        code=capture_code_identity(), decisions_preregistered=False,
                        trading_state_feed_available=False, independent_book_clock_available=False)
        protocol.update(quote_source=quote_source,
                        quote_contract_version=SOURCE_CONTRACTS[quote_source])
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
            run = conn.execute('SELECT * FROM capture_runs WHERE id=?', (run_id,)).fetchone()
            if not run:
                raise ValueError('پروتکل پایش ثبت نشده است')
            protocol = json.loads(run['protocol_json'])
            if hashlib.sha256(run['protocol_json'].encode()).hexdigest() != run['protocol_sha256']:
                raise ValueError('هش پروتکل دریافت ناسازگار است')
            if (event in ('poll_started', 'poll_finished') and protocol['version'] == CAPTURE_VERSION
                    and protocol['code'] != capture_code_identity()):
                raise ValueError('کد دریافت از زمان ثبت پروتکل تغییر کرده؛ اجرای تازه لازم است')
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
