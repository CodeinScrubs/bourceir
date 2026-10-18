"""کیفیت چرخه دریافت از روی شواهد مستقل؛ فاصله اسنپ‌شات جای آن نیست."""

import hashlib
import json
from collections import Counter
from datetime import datetime, time

from core.capture_evidence import SUPPORTED_CAPTURE_VERSIONS, binding_problems, receipts_from_cycle, sha256
from core.timeutils import TEHRAN_TZ


def _clock(value):
    try:
        stamp = datetime.fromisoformat(value)
        return stamp.astimezone(TEHRAN_TZ) if stamp.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def _max_gap(stamps):
    # ترتیب ثبت حفظ می‌شود؛ برگشت ساعت به «فاصله مثبت» مرتب نمی‌شود.
    gaps = [(right - left).total_seconds() for left, right in zip(stamps, stamps[1:])
            if left is not None and right is not None]
    return max(gaps) if gaps else None, sum(gap < 0 for gap in gaps)


def read_capture_audit(db, session_date):
    result = dict(version='capture-quality-v2', runs=[], protocols=0, attempted_polls=0, completed_polls=0,
                  pending_polls=0, complete_scope_polls=0, skipped_closed=0, status_counts={},
                  bound_snapshot_receipts=0, unbound_snapshot_receipts=0,
                  full_session_coverage_known=False, prospective_decisions_verified=False)
    exists = db.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='capture_runs'").fetchone()
    result['status'] = 'not_recorded' if exists else 'schema_unavailable'
    if not exists or session_date is None:
        return result
    runs = db.conn.execute("SELECT * FROM capture_runs r WHERE substr(r.started_at,1,10)=? OR EXISTS "
                           "(SELECT 1 FROM capture_events e WHERE e.run_id=r.id AND substr(e.recorded_at,1,10)=?) "
                           "ORDER BY r.started_at,r.id", (session_date, session_date)).fetchall()
    totals = Counter()
    for source in runs:
        protocol = json.loads(source['protocol_json'])
        if (hashlib.sha256(source['protocol_json'].encode()).hexdigest() != source['protocol_sha256']
                or protocol['version'] not in SUPPORTED_CAPTURE_VERSIONS
                or sha256(protocol['code']['files']) != protocol['code']['sha256']):
            raise ValueError('هش یا قرارداد پروتکل پایش ناسازگار است؛ گزارش کیفیت معتبر نیست')
        pairs = db.conn.execute("SELECT s.cycle,s.recorded_at AS started_at,f.recorded_at AS finished_at,f.payload_json, "
            "s.id AS started_id,f.id AS finished_id "
            "FROM capture_events s LEFT JOIN capture_events f ON f.run_id=s.run_id AND f.cycle=s.cycle "
            "AND f.event='poll_finished' WHERE s.run_id=? AND s.event='poll_started' "
            "AND substr(s.recorded_at,1,10)=? ORDER BY s.cycle",
            (source['id'], session_date)).fetchall()
        stop = db.conn.execute("SELECT recorded_at,payload_json FROM capture_events WHERE run_id=? AND event='run_stopped'",
                               (source['id'],)).fetchone()
        skipped = db.conn.execute("SELECT COUNT(*) FROM capture_events WHERE run_id=? AND event='market_closed' "
                                  "AND substr(recorded_at,1,10)=?", (source['id'], session_date)).fetchone()[0]
        by_ins = {row['ins_code']: dict(symbol=row['symbol_fa'], ins_code=row['ins_code'],
                  valid=0, changed_or_first=0, unchanged=0, missing=0, rejected=0, duplicate=0, outcome_unknown=0,
                  bound_snapshot_receipts=0, snapshot_binding_missing=0, snapshot_binding_conflict=0,
                  snapshot_binding_clock_invalid=0,
                  price_time_fresh=0, price_time_stale=0, price_time_future=0, price_time_unknown=0)
                  for row in protocol['instruments']}
        counts = Counter()
        receives = {key: [] for key in by_ins}
        wall_clock_errors = 0
        for pair in pairs:
            start, finish = _clock(pair['started_at']), _clock(pair['finished_at'])
            if not pair['finished_at']:
                counts['pending'] += 1
                for item in by_ins.values():
                    item['outcome_unknown'] += 1
                continue
            payload = json.loads(pair['payload_json'])
            counts[payload['status']] += 1
            if start is None or finish is None or finish < start:
                wall_clock_errors += 1
            coverage = payload.get('coverage')
            if coverage is None:
                for item in by_ins.values():
                    item['outcome_unknown'] += 1
                continue
            observed = coverage['per_instrument']
            if (coverage['expected'] != len(by_ins) or len(observed) != len(by_ins)
                    or {item['ins_code'] for item in observed} != set(by_ins)
                    or coverage.get('unresolved_symbols')):
                raise ValueError('نتیجه دریافت با دامنه ثابت پروتکل سازگار نیست')
            statuses = Counter(item['status'] for item in observed)
            if (set(statuses) - {'changed', 'unchanged', 'missing', 'duplicate', 'rejected'}
                    or coverage['valid'] != statuses['changed'] + statuses['unchanged']
                    or any(coverage[key] != statuses[key] for key in ('changed', 'unchanged', 'missing', 'rejected', 'duplicate'))):
                raise ValueError('شمار نتیجه دریافت با مشاهدات مستقل سازگار نیست')
            if coverage['valid'] == len(by_ins):
                counts['complete_scope'] += 1
            evidence = dict(run=dict(source), started=dict(id=pair['started_id'], run_id=source['id'],
                cycle=pair['cycle'], event='poll_started', recorded_at=pair['started_at']),
                finished=dict(id=pair['finished_id'], run_id=source['id'], cycle=pair['cycle'], event='poll_finished',
                              recorded_at=pair['finished_at'], payload_json=pair['payload_json']))
            receipts = {r['ins_code']: r for r in receipts_from_cycle(evidence).values()}
            for row in observed:
                item = by_ins[row['ins_code']]
                status = row['status']
                item['changed_or_first' if status == 'changed' else status] += 1
                if status not in ('changed', 'unchanged'):
                    continue
                item['valid'] += 1
                binding = row.get('snapshot_binding')
                stored = db.conn.execute('SELECT * FROM snapshots WHERE id=?',
                                         (binding['snapshot_id'],)).fetchone() if binding else None
                try:
                    problems = binding_problems(dict(stored) if stored else None, receipts[row['ins_code']],
                                                pair['finished_at'])
                except ValueError:
                    problems = ['capture_receipt_time_unknown']
                label = ('snapshot_binding_missing' if 'snapshot_binding_missing' in problems else
                         'snapshot_binding_conflict' if 'snapshot_binding_conflict' in problems else
                         'snapshot_binding_clock_invalid' if problems else 'bound_snapshot_receipts')
                item[label] += 1
                receives[row['ins_code']].append(finish)
                provider = _clock(row['provider_timestamp'])
                # ساعت پایان مستقل است؛ captured_at ادعاییِ پاسخ، تازگی مصنوعی نمی‌سازد.
                age = (finish - provider).total_seconds() if finish and provider else None
                key = ('unknown' if age is None else 'future' if age < -5 else 'stale' if age > 45 else 'fresh')
                item['price_time_' + key] += 1
        for key, item in by_ins.items():
            gap, backwards = _max_gap(receives[key])
            item['max_valid_poll_gap_seconds'] = gap
            wall_clock_errors += backwards
        gap, backwards = _max_gap([_clock(pair['started_at']) for pair in pairs])
        wall_clock_errors += backwards
        day = datetime.fromisoformat(session_date).date()
        start_hhmm, end_hhmm = (protocol['trading_hours'][key] for key in ('start', 'end'))
        window_start = datetime.combine(day, time.fromisoformat(start_hhmm), TEHRAN_TZ)
        window_end = datetime.combine(day, time.fromisoformat(end_hhmm), TEHRAN_TZ)
        first = _clock(pairs[0]['started_at']) if pairs else None
        last = _clock(pairs[-1]['finished_at']) if pairs else None
        margin = protocol['interval_seconds']
        bracketed = bool(not protocol['forced'] and first and last
                         and abs((first - window_start).total_seconds()) <= margin
                         and abs((last - window_end).total_seconds()) <= margin)
        # دو انتها کافی نیست؛ این فقط شاهد پوشاندن حدود ساعت تنظیم‌شده است.
        run = dict(id=source['id'], started_at=source['started_at'], protocol_sha256=source['protocol_sha256'],
                   capture_version=protocol['version'],
                   project_version=protocol['project_version'], code_sha256=protocol['code']['sha256'],
                   scope_kind=protocol['scope_kind'], expected_instruments=len(by_ins),
                   interval_seconds=protocol['interval_seconds'], forced=protocol['forced'],
                   stopped_at=stop['recorded_at'] if stop else None,
                   stop_reason=json.loads(stop['payload_json'])['reason'] if stop else None,
                   attempted_polls=len(pairs), completed_polls=len(pairs) - counts['pending'],
                   pending_polls=counts['pending'], skipped_closed=skipped,
                   status_counts={key: counts[key] for key in ('success', 'partial', 'failed', 'error')},
                   complete_scope_polls=counts['complete_scope'], max_poll_start_gap_seconds=gap,
                   wall_clock_inconsistencies=wall_clock_errors,
                   configured_window_start=window_start.isoformat(), configured_window_end=window_end.isoformat(),
                   configured_window_bracketed=bracketed,
                   calendar_date_explicit=session_date in protocol['calendar'],
                   full_session_coverage_known=False, per_symbol=list(by_ins.values()))
        run['bound_snapshot_receipts'] = sum(r['bound_snapshot_receipts'] for r in by_ins.values())
        run['unbound_snapshot_receipts'] = sum(r['valid'] - r['bound_snapshot_receipts'] for r in by_ins.values())
        result['runs'].append(run)
        for name in ('attempted_polls', 'completed_polls', 'pending_polls', 'complete_scope_polls', 'skipped_closed',
                     'bound_snapshot_receipts', 'unbound_snapshot_receipts'):
            result[name] += run[name]
        totals.update(run['status_counts'])
    result.update(status='recorded' if runs else 'not_recorded', protocols=len(runs), status_counts=dict(totals))
    return result
