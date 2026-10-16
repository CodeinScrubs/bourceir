"""ثبت اتمی رسید بازیابی و حفظ آن در دریافت تکراریِ بدون تغییر سایر آمار روز."""

import hashlib
import json

import pandas as pd

from config import settings
from core.price_quality import json_price_row, price_quality
from core.timeutils import is_canonical_iso_date, now_tehran_iso

FIELDS = {'open_raw': (2, 13), 'close_raw': (5, 16)}
RAW_INDEX = dict(date=1, open_raw=13, high_raw=14, low_raw=15, close_raw=16,
                 close_final_raw=17, ref_price_raw=18, volume=7, value=8, trade_count=9)


def recovery_partitions(conn, symbol_ids, end_exclusive=None):
    """رسیدهای بازار به تفکیک روز، بدون صادرکردن توضیح آزاد یا محتوای حساب."""
    if not symbol_ids or not conn.execute("SELECT 1 FROM sqlite_master WHERE name='price_recoveries' AND type='table'").fetchone():
        return {}
    placeholders = ','.join('?' for _ in symbol_ids)
    rows = conn.execute(f'''SELECT r.* FROM price_recoveries r WHERE r.symbol_id IN ({placeholders})
        AND (? IS NULL OR r.date < ?) ORDER BY r.date,r.id''', [*symbol_ids, end_exclusive, end_exclusive])
    digests, counts = {}, {}
    for row in rows:
        day = row['date']
        body = json.dumps(dict(row), ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')
        digests.setdefault(day, hashlib.sha256()).update(body + b'\n')
        counts[day] = counts.get(day, 0) + 1
    return {day: dict(sha256=digest.hexdigest(), rows=counts[day]) for day, digest in digests.items()}


def preserve_recoveries(conn, symbol_id, params):
    """فقط اگر ردیف فعلی هنوز نسخه تأییدشده و سایر مقادیر دریافتی دقیقاً یکسان باشند."""
    if not conn.in_transaction:
        raise ValueError('حفظ بازیابی باید در تراکنش نویسنده انجام شود')
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='price_recoveries'").fetchone():
        return params
    rows = conn.execute('''SELECT r.after_json, r.fields_json, p.* FROM price_recoveries r
        JOIN daily_prices p ON p.id=r.daily_price_id WHERE r.symbol_id=?
        AND r.rowid=(SELECT max(last.rowid) FROM price_recoveries last WHERE last.daily_price_id=r.daily_price_id)''',
                        (symbol_id,)).fetchall()
    guards = {row['date']: row for row in rows}
    protected = []
    for values in params:
        values = list(values)
        receipt = guards.get(values[1])
        if receipt is not None and values[11] == receipt['source'] == 'tsetmc' and values[10] == receipt['price_unit'] == 'Rial':
            after, fields = json.loads(receipt['after_json']), json.loads(receipt['fields_json'])
            incoming = json_price_row({key: values[index] for key, index in RAW_INDEX.items()})
            if (json_price_row(dict(receipt)) == after
                    and all(incoming[key] == value for key, value in after.items() if key not in fields)
                    and all(incoming[key] in (None, after[key]) for key in fields)):
                for field in fields:
                    if incoming[field] is None:
                        for index in FIELDS[field]:
                            values[index] = after[field]
        protected.append(tuple(values))
    return protected


def apply_recoveries(db, run_id, candidates, *, cutoff, evidence, before_commit=None):
    """فراخواننده شواهد/کد را پیش‌تر بررسی و پشتیبان می‌گیرد؛ این تابع شبکه ندارد."""
    if db.conn.in_transaction:
        raise ValueError('اعمال بازیابی به تراکنش مستقل نیاز دارد')
    if not is_canonical_iso_date(cutoff) or cutoff > settings.RESEARCH_CONFIG['holdout_start']:
        raise ValueError('بازیابی اجازه تغییر دوره مهر و موم را ندارد')
    if not candidates:
        return dict(applied=0, already_applied=0)
    db.conn.execute('BEGIN IMMEDIATE')
    applied = already = 0
    try:
        for issue, candidate in candidates:
            _, flags = price_quality(pd.DataFrame([candidate['after']]))
            if (candidate['status'] != 'verified_same_source_candidate' or not is_canonical_iso_date(issue['date'])
                    or not issue['date'] < cutoff or candidate['after'] != json_price_row(candidate['after']) or flags.any(axis=None)):
                raise ValueError('فقط نامزد تطبیق‌شده دوره توسعه قابل اعمال است')
            if candidate['before'] != issue['raw']:
                raise ValueError('داده پیشنهادی ردیف تغییر کرده است')
            row = db.conn.execute('''SELECT p.*,s.ins_code,s.symbol_fa,s.instrument_type_code FROM daily_prices p
                JOIN symbols s ON s.id=p.symbol_id WHERE p.symbol_id=? AND p.date=? AND p.source='tsetmc' ''',
                                  (issue['symbol_id'], issue['date'])).fetchone()
            if (row is None or str(row['ins_code']) != issue['ins_code'] or row['symbol_fa'] != issue['symbol']
                    or row['instrument_type_code'] not in (300, 303) or row['price_unit'] != 'Rial'):
                raise ValueError('هویت یا واحد ردیف بازیابی تغییر کرده است')
            current = json_price_row(dict(row))
            receipt_id = hashlib.sha256(f'{run_id}:{row["id"]}'.encode()).hexdigest()
            receipt = db.conn.execute('SELECT * FROM price_recoveries WHERE id=?', (receipt_id,)).fetchone()
            if receipt is not None:
                if (current != candidate['after'] or json.loads(receipt['evidence_json']) != evidence
                        or json.loads(receipt['before_json']) != candidate['before']
                        or json.loads(receipt['after_json']) != candidate['after']):
                    raise ValueError('رسید یا داده ردیف تغییر کرده است')
                already += 1
                continue
            if current != candidate['before'] or candidate['before'] != issue['raw']:
                raise ValueError('داده ردیف پس از تهیه پیشنهاد تغییر کرده است')
            changed = candidate['changed_fields']
            if not changed or any(field not in FIELDS or current[field] is not None for field in changed):
                raise ValueError('بازیابی فقط فیلد گمشده اولین/آخرین معامله را می‌پذیرد')
            if {key for key in current if current[key] != candidate['after'].get(key)} != set(changed):
                raise ValueError('پیشنهاد شامل تغییر خارج از دامنه است')
            stamp = now_tehran_iso()
            columns = [(field, candidate['after'][field]) for field in changed]
            columns += [(field.removesuffix('_raw'), candidate['after'][field]) for field in changed]
            assignment = ','.join(f'{field}=?' for field, _ in columns)
            db.conn.execute(f'UPDATE daily_prices SET {assignment},captured_at=? WHERE id=?',
                            [value for _, value in columns] + [stamp, row['id']])
            db.conn.execute('''INSERT INTO price_recoveries VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (receipt_id, run_id, issue['symbol_id'], row['id'], issue['date'], stamp,
                 json.dumps(current, sort_keys=True, allow_nan=False), json.dumps(candidate['after'], sort_keys=True, allow_nan=False),
                 json.dumps(sorted(FIELDS)), json.dumps(evidence, sort_keys=True, allow_nan=False)))
            applied += 1
        if before_commit is not None:
            before_commit()
        db.conn.commit()
    except Exception:
        db.conn.rollback()
        raise
    return dict(applied=applied, already_applied=already)
