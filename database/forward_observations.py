"""ثبت اتمی و فقط‌افزودنی مشاهدات آینده روی دامنه ثابت؛ بدون دسترسی کارگزاری."""

import json
import uuid

from config import settings
from core.forward_observations import (
    CHOICES,
    PHASES,
    VERSION,
    canonical,
    clock,
    digest,
    event_hash,
    receipt_quote_evidence,
    report_trial,
    select_receipts,
    validate_archive,
    validate_schedule,
)
from core.text import normalize_symbol
from core.timeutils import now_tehran_iso
from database.capture import CAPTURE_FILES, CaptureRepository, capture_code_identity

CODE_FILES = tuple(dict.fromkeys(('core/forward_observations.py', 'database/forward_observations.py',
              'database/migrations/012_forward_observations.sql', 'dashboard/forward_observations.py',
              'database/db.py', 'core/timeutils.py', 'core/text.py', 'config/settings.py', 'requirements.lock.txt') + CAPTURE_FILES))


def code_identity():
    # فقط کد عمومی؛ تنظیمات شخصی، .env یا نشست مرورگر وارد آرشیو نمی‌شوند.
    sources = {name: (settings.BASE_DIR / name).read_text(encoding='utf-8').replace('\r\n', '\n') for name in CODE_FILES}
    return dict(sources=sources, sha256=digest(sources))


class ForwardRepository:
    def __init__(self, db):
        self.db = db

    def list_trials(self):
        exists = self.db.conn.execute("SELECT 1 FROM sqlite_master WHERE name='forward_trials' AND type='table'").fetchone()
        return [dict(r) for r in self.db.conn.execute('SELECT * FROM forward_trials ORDER BY registered_at,id')] if exists else []

    def _load(self, trial_id):
        row = self.db.conn.execute('SELECT * FROM forward_trials WHERE id=?', (trial_id,)).fetchone()
        if row is None:
            raise ValueError('پروتکل مشاهده پیدا نشد')
        events = [dict(e) for e in self.db.conn.execute('SELECT * FROM forward_events WHERE trial_id=? ORDER BY id', (trial_id,))]
        return dict(row), events

    def register(self, *, symbols, rationale, schedule):
        if not isinstance(rationale, str) or not 1 <= len(rationale.strip()) <= 10000:
            raise ValueError('روش انتخاب دامنه و معیار بررسی لازم است؛ حداکثر ۱۰هزار نویسه')
        if not isinstance(symbols, list) or not symbols or not all(isinstance(s, str) and s.strip() for s in symbols):
            raise ValueError('فهرست صریح نمادها لازم است')
        normalized = [normalize_symbol(s) for s in symbols]
        if len(set(normalized)) != len(normalized):
            raise ValueError('نماد تکراری پس از یکسان‌سازی نام پذیرفته نیست')
        identity = code_identity()
        with self.db.conn:
            self.db.conn.execute('BEGIN IMMEDIATE')
            scope = []
            for name in sorted(normalized):
                row = self.db.get_symbol(name)
                if not row or row['instrument_type_code'] not in (300, 303) or not row['ins_code']:
                    raise ValueError('دامنه باید سهام عادی ثبت‌شده با شناسه معتبر باشد')
                scope.append(dict(symbol_id=row['id'], symbol_fa=row['symbol_fa'], ins_code=row['ins_code'],
                                  instrument_type_code=row['instrument_type_code']))
            at, calendar = now_tehran_iso(), self.db.get_market_calendar()
            schedule = validate_schedule(schedule, at, calendar, settings.TRADING_HOURS, settings.TRADING_DAYS)
            key = 'FWD-' + uuid.uuid4().hex
            protocol = dict(id=key, version=VERSION, registered_at=at, project_version=settings.VERSION,
                            rationale=rationale.strip(), scope=scope, schedule=schedule, calendar=calendar,
                            trading_hours=dict(settings.TRADING_HOURS), trading_days=list(settings.TRADING_DAYS),
                            code=identity, capture_code=capture_code_identity(sources=identity['sources']),
                            kind='manual_observation_only', accepted_for_signals=False,
                            externally_timestamped=False, execution_model=None)
            self.db.conn.execute('INSERT INTO forward_trials VALUES(?,?,?,?)', (key, at, canonical(protocol), digest(protocol)))
        return key

    def record(self, trial_id, phase, *, assessments=None):
        if phase not in PHASES or (phase == 'followup' and assessments is not None):
            raise ValueError('مرحله مشاهده نامعتبر است')
        if phase == 'assessment' and not isinstance(assessments, dict):
            raise ValueError('ارزیابی‌ها باید نگاشت نام نماد به وضعیت و دلیل باشند')
        identity = code_identity()
        with self.db.conn:
            self.db.conn.execute('BEGIN IMMEDIATE')
            trial, events = self._load(trial_id)
            protocol, existing, previous = validate_archive(trial, events)
            if phase in existing or 'followup' in existing:
                raise ValueError('این مرحله قبلاً ثبت شده یا پیگیری پایان یافته است')
            if identity != protocol['code']:
                raise ValueError('کد از زمان ثبت پروتکل تغییر کرده؛ اجرای تازه لازم است')
            given = {}
            for name, item in (assessments or {}).items():
                if not isinstance(name, str):
                    raise ValueError('نام نماد باید متن باشد')
                symbol = normalize_symbol(name)
                if symbol in given or symbol not in {s['symbol_fa'] for s in protocol['scope']}:
                    raise ValueError('ارزیابی تکراری یا خارج از دامنه ثابت است')
                if (not isinstance(item, dict) or set(item) != {'choice', 'rationale'} or item['choice'] not in CHOICES
                        or not isinstance(item['rationale'], str) or len(item['rationale']) > 10000
                        or (item['choice'] != 'not_reviewed' and not item['rationale'].strip())):
                    raise ValueError('وضعیت و دلیل ارزیابی نامعتبر است')
                given[symbol] = dict(choice=item['choice'], rationale=item['rationale'].strip())
            # همان تصویر SQLite برای همه اعضا؛ نه فقط نمادهای منتخب یا دارای قیمت.
            snapshots = []
            for instrument in protocol['scope']:
                row = self.db.conn.execute("SELECT * FROM snapshots WHERE symbol_id=? AND source='tsetmc' ORDER BY id DESC LIMIT 1",
                                           (instrument['symbol_id'],)).fetchone()
                snapshots.append(dict(row) if row else None)
            at = now_tehran_iso()
            last_at = clock(events[-1]['recorded_at']) if events else clock(trial['registered_at'])
            if clock(at) < last_at or not clock(protocol['schedule'][phase + '_start']) <= clock(at) < clock(protocol['schedule'][phase + '_end']):
                raise ValueError('اکنون داخل پنجره از پیش ثبت‌شده نیست یا ساعت به عقب برگشته است')
            rows = []
            cycles = CaptureRepository(self.db).latest_cycles({s['symbol_id'] for s in protocol['scope']})
            receipts = select_receipts(cycles, protocol['scope'], protocol['capture_code'])
            for instrument, snapshot in zip(protocol['scope'], snapshots):
                row = dict(symbol_id=instrument['symbol_id'],
                           quote=receipt_quote_evidence(snapshot, at, receipts.get(instrument['symbol_id'])))
                if phase == 'assessment':
                    row.update(given.get(instrument['symbol_fa'], dict(choice='not_reviewed', rationale='')))
                rows.append(row)
            payload = dict(code_sha256=identity['sha256'], capture_cycles=cycles, rows=rows)
            self.db.conn.execute('INSERT INTO forward_events(trial_id,phase,recorded_at,payload_json,previous_sha256,event_sha256) '
                                 'VALUES(?,?,?,?,?,?)', (trial_id, phase, at, canonical(payload), previous,
                                                      event_hash(trial_id, phase, at, payload, previous)))

    def report(self, trial_id):
        # خواندن پروتکل و رویدادها در یک تصویر سازگار؛ این مسیر هیچ ثبت بازار/حساب ندارد.
        with self.db.conn:
            self.db.conn.execute('BEGIN')
            trial, events = self._load(trial_id)
            return report_trial(trial, events, now_tehran_iso())

    def export(self, trial_id):
        with self.db.conn:
            self.db.conn.execute('BEGIN')
            trial, events = self._load(trial_id)
            report = report_trial(trial, events, now_tehran_iso())
            return json.dumps(dict(trial=trial, events=events, report=report), ensure_ascii=False, indent=2, allow_nan=False)
