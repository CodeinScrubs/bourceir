"""قرارداد مشاهده آینده؛ تغییر قیمت، اجرای معامله یا سود سرمایه نیست."""

import hashlib
import json
import math
from collections import Counter
from datetime import datetime, time

from core.capture_evidence import binding_problems, receipts_from_cycle
from core.timeutils import TEHRAN_TZ

VERSION = 'forward-observation-v2'
SUPPORTED_VERSIONS = ('forward-observation-v1', VERSION)
PHASES = ('assessment', 'followup')
CHOICES = ('watch', 'skip', 'not_reviewed')
MAX_AGE_SECONDS = 45


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def clock(value):
    try:
        at = datetime.fromisoformat(value)
        if at.tzinfo is not None:
            return at.astimezone(TEHRAN_TZ)
    except (ValueError, TypeError):
        pass
    raise ValueError('زمان باید میلادی ISO با منطقه زمانی باشد')


def validate_schedule(schedule, registered_at, calendar, trading_hours, trading_days):
    keys = ('assessment_start', 'assessment_end', 'followup_start', 'followup_end')
    if not isinstance(schedule, dict) or set(schedule) != set(keys):
        raise ValueError('چهار زمان شروع و پایان بررسی و پیگیری لازم است')
    stamps = [clock(schedule[key]) for key in keys]
    if not clock(registered_at) < stamps[0] < stamps[1] < stamps[2] < stamps[3]:
        raise ValueError('ثبت باید پیش از بررسی و بررسی باید پیش از پیگیری باشد')
    def configured_open(stamp):
        session = calendar.get(stamp.date().isoformat(), stamp.weekday() in trading_days)
        return session is True and time.fromisoformat(trading_hours['start']) <= stamp.time() < time.fromisoformat(trading_hours['end'])

    if len({stamp.date() for stamp in stamps}) != 1 or not all(configured_open(s) for s in stamps):
        raise ValueError('نسخه فعلی فقط دو پنجره در یک روز و ساعت بازار تنظیم‌شده را می‌پذیرد')
    # پنجره بلند، انتخاب پس از دیدن نتیجه را آسان می‌کند؛ در v1 حداکثر پنج دقیقه است.
    if any((stamps[i + 1] - stamps[i]).total_seconds() > 300 for i in (0, 2)):
        raise ValueError('هر پنجره ثبت حداکثر پنج دقیقه است')
    return dict(zip(keys, (s.isoformat() for s in stamps)))


def quote_evidence(snapshot, recorded_at):
    """قیمت فقط با ساعت/واحد/دامنه معتبر؛ محتوای نامعتبر نیز برای ممیزی حفظ می‌شود."""
    at, problems = clock(recorded_at), []
    if snapshot is None:
        return dict(snapshot=None, problems=['missing_snapshot'], last_price_rial=None,
                    independent_receipt_verified=False, executable=False)
    for field in ('captured_at', 'provider_timestamp'):
        try:
            stamp = clock(snapshot.get(field))
            age = (at - stamp).total_seconds()
            if age < 0 or stamp.date() != at.date():
                problems.append(field + '_future_or_other_day')
            elif age > MAX_AGE_SECONDS:
                problems.append(field + '_stale')
        except ValueError:
            problems.append(field + '_unknown')
    if snapshot.get('source') != 'tsetmc' or snapshot.get('price_unit') != 'Rial':
        problems.append('source_or_unit_invalid')
    numbers = {}
    for field in ('last_price', 'low_price', 'high_price', 'ref_price', 'volume', 'value'):
        value = snapshot.get(field)
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
            problems.append(field + '_invalid')
        else:
            numbers[field] = value
    if all(k in numbers for k in ('last_price', 'low_price', 'high_price')):
        if not numbers['low_price'] <= numbers['last_price'] <= numbers['high_price']:
            problems.append('price_outside_range')
    return dict(snapshot=snapshot, problems=problems,
                last_price_rial=numbers.get('last_price') if not problems else None,
                independent_receipt_verified=False, executable=False)


def select_receipts(cycles, scope, expected_code):
    """آخرین چرخه پایان‌یافته مقدم است؛ شکست تازه با موفقیت قدیمی جایگزین نمی‌شود."""
    selected, seen = {}, set()
    by_id = {instrument['symbol_id']: instrument for instrument in scope}
    for cycle in cycles:
        key = (cycle['run']['id'], cycle['finished']['cycle'])
        if key in seen:
            raise ValueError('چرخه دریافت تکراری در آرشیو مشاهده')
        seen.add(key)
        for sid, receipt in receipts_from_cycle(cycle).items():
            if sid not in by_id:
                continue
            instrument = by_id[sid]
            receipt['scope_matches'] = all(receipt[k] == instrument[k] for k in ('ins_code', 'instrument_type_code'))
            receipt['code_matches'] = receipt['code'] == expected_code
            if sid not in selected or receipt['finished_event_id'] > selected[sid]['finished_event_id']:
                selected[sid] = receipt
    return selected


def receipt_quote_evidence(snapshot, recorded_at, receipt):
    problems = binding_problems(snapshot, receipt, recorded_at, max_age=MAX_AGE_SECONDS)
    if receipt:
        if not receipt['scope_matches']:
            problems.append('capture_scope_conflict')
        if not receipt['code_matches']:
            problems.append('capture_code_conflict')
        if receipt['provider'] != 'tsetmc':
            problems.append('capture_provider_conflict')
    verified = not problems
    # دریافت بی‌تغییر تازه، فقط ساعت دریافت را توضیح می‌دهد؛ ساعت قیمت همچنان مستقل کنترل می‌شود.
    checked = dict(snapshot, captured_at=receipt['observation']['captured_at']) if verified else snapshot
    quote = quote_evidence(checked, recorded_at)
    quote.update(snapshot=snapshot, capture_receipt_verified=verified,
                 capture_receipt=None if receipt is None else {key: receipt[key] for key in
                    ('run_id', 'protocol_sha256', 'cycle', 'finished_event_id')})
    quote['problems'] += problems
    if quote['problems']:
        quote['last_price_rial'] = None
    return quote


def event_hash(trial_id, phase, recorded_at, payload, previous_sha256):
    return digest(dict(trial_id=trial_id, phase=phase, recorded_at=recorded_at,
                       payload=payload, previous_sha256=previous_sha256))


def validate_archive(trial, events):
    protocol = json.loads(trial['protocol_json'])
    if digest(protocol) != trial['protocol_sha256'] or protocol.get('version') not in SUPPORTED_VERSIONS:
        raise ValueError('هش یا نسخه پروتکل مشاهده ناسازگار است')
    if trial['id'] != protocol['id'] or trial['registered_at'] != protocol['registered_at']:
        raise ValueError('هویت یا زمان ثبت با پروتکل ناسازگار است')
    validate_schedule(protocol['schedule'], protocol['registered_at'], protocol['calendar'],
                      protocol['trading_hours'], protocol['trading_days'])
    code = protocol['code']
    if digest(code['sources']) != code['sha256']:
        raise ValueError('اثر انگشت کد محفوظ ناسازگار است')
    if protocol['version'] == VERSION:
        capture_code = protocol['capture_code']
        files = {name: hashlib.sha256(code['sources'][name].encode()).hexdigest() for name in capture_code['files']}
        if not files or files != capture_code['files'] or digest(files) != capture_code['sha256']:
            raise ValueError('اثر انگشت کد دریافت با کد محفوظ ناسازگار است')
    scope = [s['symbol_id'] for s in protocol['scope']]
    if not scope or len(scope) != len(set(scope)):
        raise ValueError('دامنه پروتکل نامعتبر است')
    previous, last_at, seen, decoded = trial['protocol_sha256'], clock(trial['registered_at']), set(), {}
    for event in events:
        phase, at = event['phase'], clock(event['recorded_at'])
        payload = json.loads(event['payload_json'])
        if (phase not in PHASES or phase in seen or ('followup' in seen)
                or event['trial_id'] != trial['id'] or event['previous_sha256'] != previous
                or event_hash(trial['id'], phase, event['recorded_at'], payload, previous) != event['event_sha256']):
            raise ValueError('زنجیره شواهد مشاهده ناسازگار است')
        if not last_at <= at or not clock(protocol['schedule'][phase + '_start']) <= at < clock(protocol['schedule'][phase + '_end']):
            raise ValueError('رویداد خارج از ترتیب یا پنجره از پیش ثبت‌شده است')
        if payload['code_sha256'] != code['sha256'] or [r['symbol_id'] for r in payload['rows']] != scope:
            raise ValueError('کد یا دامنه رویداد با پروتکل ناسازگار است')
        receipts = (select_receipts(payload['capture_cycles'], protocol['scope'], protocol['capture_code'])
                    if protocol['version'] == VERSION else {})
        for row in payload['rows']:
            quote = row['quote']
            expected = (receipt_quote_evidence(quote['snapshot'], event['recorded_at'], receipts.get(row['symbol_id']))
                        if protocol['version'] == VERSION else quote_evidence(quote['snapshot'], event['recorded_at']))
            if quote != expected:
                raise ValueError('کیفیت نقل قیمت با محتوای محفوظ ناسازگار است')
            if quote['snapshot'] and quote['snapshot']['symbol_id'] != row['symbol_id']:
                raise ValueError('شناسه نقل قیمت با نماد بررسی متفاوت است')
            if phase == 'assessment' and (row['choice'] not in CHOICES or
                    (row['choice'] != 'not_reviewed' and not row['rationale'].strip())):
                raise ValueError('ارزیابی دستی ناقص است')
        decoded[phase] = dict(recorded_at=event['recorded_at'], **payload)
        previous, last_at = event['event_sha256'], at
        seen.add(phase)
    return protocol, decoded, previous


def report_trial(trial, events, at):
    protocol, decoded, chain = validate_archive(trial, events)
    at = clock(at)
    latest = max([clock(trial['registered_at'])] + [clock(e['recorded_at']) for e in events])
    if at < latest:
        raise ValueError('ساعت سیستم از آخرین ثبت عقب‌تر است')
    phases = {p: 'recorded' if p in decoded else 'missed' if at >= clock(protocol['schedule'][p + '_end'])
              else 'open' if at >= clock(protocol['schedule'][p + '_start']) else 'pending' for p in PHASES}
    rows = []
    for i, instrument in enumerate(protocol['scope']):
        first = decoded.get('assessment', {}).get('rows', [])
        second = decoded.get('followup', {}).get('rows', [])
        before, after = first[i] if first else None, second[i] if second else None
        reasons = []
        if not before:
            reasons.append('assessment_' + phases['assessment'])
        if not after:
            reasons.append('followup_' + phases['followup'])
        if before and before['quote']['problems']:
            reasons.append('assessment_quote_invalid')
        if after and after['quote']['problems']:
            reasons.append('followup_quote_invalid')
        change = None
        if not reasons:
            b, a = before['quote']['snapshot'], after['quote']['snapshot']
            if (a['id'] == b['id'] or clock(a['captured_at']) <= clock(decoded['assessment']['recorded_at'])
                    or clock(a['provider_timestamp']) <= clock(decoded['assessment']['recorded_at'])):
                reasons.append('no_new_quote_after_assessment')
            elif a['ref_price'] != b['ref_price']:
                reasons.append('reference_price_changed')
            else:
                change = after['quote']['last_price_rial'] / before['quote']['last_price_rial'] - 1
                if not math.isfinite(change):
                    change = None
                    reasons.append('nonfinite_quote_change')
        rows.append(dict(**instrument, choice=before['choice'] if before else 'not_recorded',
                         rationale=before['rationale'] if before else None,
                         assessment=before['quote'] if before else None, followup=after['quote'] if after else None,
                         raw_quote_change=change, problems=reasons))
    return dict(version=protocol['version'], id=trial['id'], protocol=protocol, phases=phases, rows=rows,
                scope_count=len(rows), choice_counts=dict(Counter(r['choice'] for r in rows)),
                known_quote_pairs=sum(r['raw_quote_change'] is not None for r in rows),
                unknown_quote_pairs=sum(r['raw_quote_change'] is None for r in rows), chain_sha256=chain,
                receipt_bound_quote_pairs=sum(r['raw_quote_change'] is not None and
                    all(r[p].get('capture_receipt_verified', False) for p in ('assessment', 'followup')) for r in rows),
                locally_ordered=True, externally_timestamped=False, independent_trial_verified=False,
                order_fills_verified=False, portfolio_return=None, accepted_for_signals=False)
