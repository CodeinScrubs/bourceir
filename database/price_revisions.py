"""اصلاح اتمی شاهددار با همان دفتر فقط‌افزودنی 015 و نسخه قبلی در 007."""

import hashlib
import json
import re

from config import settings
from core.price_quality import json_price_row
from core.price_revision import STATISTICS, VERSION, valid_revised_row
from core.timeutils import is_canonical_iso_date, now_tehran_iso

FIELD_INDEX = dict(open_raw=(2, 13), high_raw=(3, 14), low_raw=(4, 15), close_raw=(5, 16),
                   volume=(7,), value=(8,), trade_count=(9,))


def apply_source_revisions(db, run_id, candidates, *, cutoff, evidence, before_commit):
    if (db.conn.in_transaction or not re.fullmatch(r'NATIVEREV-[a-f0-9]{32}', run_id)
            or evidence.get('version') != VERSION or not callable(before_commit)
            or not is_canonical_iso_date(cutoff) or cutoff > settings.RESEARCH_CONFIG['holdout_start']):
        raise ValueError('قرارداد یا مرز اعمال اصلاح منبع معتبر نیست')
    applied = already = 0
    db.conn.execute('BEGIN IMMEDIATE')
    try:
        for issue, candidate in candidates:
            before, after, changes = candidate['before'], candidate['after'], candidate['changed_fields']
            if (candidate['version'] != VERSION or candidate['status'] != 'verified_native_trade_revision'
                    or candidate.get('native_identity_verified') is not True or candidate.get('native_date_verified') is not True
                    or not all((candidate.get('reconciliation') or {}).get(k) is True for k in STATISTICS)
                    or type(issue['symbol_id']) is not int or before != issue['raw']
                    or not is_canonical_iso_date(issue['date']) or not issue['date'] < cutoff
                    or before['date'] != issue['date'] or not valid_revised_row(before, after, changes)):
                raise ValueError('نامزد اصلاح قیمت خارج از قرارداد محدود است')
            row = db.conn.execute('''SELECT p.*,s.ins_code,s.symbol_fa,s.instrument_type_code FROM daily_prices p
                JOIN symbols s ON s.id=p.symbol_id WHERE p.symbol_id=? AND p.date=? AND p.source='tsetmc' ''',
                (issue['symbol_id'], issue['date'])).fetchone()
            if (row is None or str(row['ins_code']) != issue['ins_code'] or row['symbol_fa'] != issue['symbol']
                    or row['instrument_type_code'] not in (300, 303) or row['price_unit'] != 'Rial'):
                raise ValueError('هویت یا واحد ردیف اصلاح تغییر کرده است')
            current = json_price_row(dict(row))
            receipt_id = hashlib.sha256(f'{run_id}:{row["id"]}'.encode()).hexdigest()
            receipt = db.conn.execute('SELECT * FROM price_recoveries WHERE id=?', (receipt_id,)).fetchone()
            if receipt is not None:
                if (current != after or json.loads(receipt['before_json']) != before
                        or json.loads(receipt['after_json']) != after or json.loads(receipt['fields_json']) != changes
                        or json.loads(receipt['evidence_json']) != evidence):
                    raise ValueError('رسید اصلاح یا نسخه فعلی تغییر کرده است')
                already += 1
                continue
            if current != before:
                raise ValueError('نسخه قیمت پس از دریافت شاهد تغییر کرده است')
            values = [(field, after[field]) for field in changes]
            values += [(field.removesuffix('_raw'), after[field]) for field in changes if field.endswith('_raw')]
            stamp = now_tehran_iso()
            db.conn.execute('UPDATE daily_prices SET ' + ','.join(f'{name}=?' for name, _ in values)
                            + ',captured_at=? WHERE id=?', [v for _, v in values] + [stamp, row['id']])
            db.conn.execute('INSERT INTO price_recoveries VALUES(?,?,?,?,?,?,?,?,?,?)',
                (receipt_id, run_id, issue['symbol_id'], row['id'], issue['date'], stamp,
                 json.dumps(before, sort_keys=True, allow_nan=False), json.dumps(after, sort_keys=True, allow_nan=False),
                 json.dumps(changes), json.dumps(evidence, sort_keys=True, allow_nan=False)))
            applied += 1
        before_commit()
        db.conn.commit()
    except Exception:
        db.conn.rollback()
        raise
    return dict(applied=applied, already_applied=already)


def preserve_native_revision(receipt, incoming):
    """فقط بازگشت دقیق نسخه قبلی منبع حفظ می‌شود؛ نسخه تازه/مخلوط پنهان نمی‌شود."""
    from core.price_revision import VERSION

    evidence = json.loads(receipt['evidence_json'])
    if evidence.get('version') != VERSION:
        return None
    before, after = json.loads(receipt['before_json']), json.loads(receipt['after_json'])
    changes = json.loads(receipt['fields_json'])
    if (json_price_row(dict(receipt)) == after and incoming == before
            and valid_revised_row(before, after, changes)):
        return {index: after[field] for field in changes for index in FIELD_INDEX[field]}
    return {}
