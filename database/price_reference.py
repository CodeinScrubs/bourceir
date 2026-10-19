"""اعمال اتمی مرجع صریح و تازه‌سازی صرفاً نمایشی نمودار؛ دفترهای مالی مستقل می‌مانند."""

import hashlib
import json
import re

import pandas as pd

from config import settings
from core.market_rules import backward_adjusted_close, corporate_action_factors
from core.price_quality import json_price_row
from core.price_reference import VERSION, valid_reference_change
from core.timeutils import is_canonical_iso_date, now_tehran_iso


def apply_reference_recoveries(db, run_id, candidates, *, cutoff, evidence, before_commit):
    if (db.conn.in_transaction or not re.fullmatch(r'REFPRICE-[a-f0-9]{32}', run_id)
            or evidence.get('version') != VERSION or not callable(before_commit)
            or not is_canonical_iso_date(cutoff) or cutoff > settings.RESEARCH_CONFIG['holdout_start']):
        raise ValueError('قرارداد یا مرز اعمال مرجع معتبر نیست')
    applied, already, charts, seen = 0, 0, set(), set()
    display_rows = 0
    db.conn.execute('BEGIN IMMEDIATE')
    try:
        for issue, candidate in candidates:
            before, after = candidate['before'], candidate['after']
            key = (issue['symbol_id'], issue['date'])
            if (key in seen or candidate['version'] != VERSION or candidate['status'] != 'verified_explicit_reference'
                    or any(candidate.get(k) is not True for k in ('daily_identity_and_date_verified',
                           'history_identity_and_date_verified', 'both_paths_all_fields_equal'))
                    or candidate['changed_fields'] != ['ref_price_raw'] or type(issue['symbol_id']) is not int
                    or before != issue['raw'] or before['date'] != issue['date']
                    or not is_canonical_iso_date(issue['date']) or issue['date'] >= cutoff
                    or not valid_reference_change(before, after)):
                raise ValueError('نامزد مرجع خارج از قرارداد محدود است')
            seen.add(key)
            row = db.conn.execute('''SELECT p.*,s.ins_code,s.symbol_fa,s.instrument_type_code FROM daily_prices p
                JOIN symbols s ON s.id=p.symbol_id WHERE p.symbol_id=? AND p.date=? AND p.source='tsetmc' ''', key).fetchone()
            if (row is None or str(row['ins_code']) != issue['ins_code'] or row['symbol_fa'] != issue['symbol']
                    or row['instrument_type_code'] not in (300, 303) or row['price_unit'] != 'Rial'):
                raise ValueError('هویت یا واحد ردیف مرجع تغییر کرده است')
            current = json_price_row(dict(row))
            receipt_id = hashlib.sha256(f'{run_id}:{row["id"]}'.encode()).hexdigest()
            receipt = db.conn.execute('SELECT * FROM price_recoveries WHERE id=?', (receipt_id,)).fetchone()
            if receipt is not None:
                if (current != after or json.loads(receipt['before_json']) != before
                        or json.loads(receipt['after_json']) != after or json.loads(receipt['fields_json']) != ['ref_price_raw']
                        or json.loads(receipt['evidence_json']) != evidence):
                    raise ValueError('رسید یا نسخه فعلی مرجع تغییر کرده است')
                already += 1
                continue
            if current != before:
                raise ValueError('نسخه قیمت پس از دریافت شاهد مرجع تغییر کرده است')
            stamp = now_tehran_iso()
            db.conn.execute('UPDATE daily_prices SET ref_price_raw=?,captured_at=? WHERE id=?',
                            (after['ref_price_raw'], stamp, row['id']))
            db.conn.execute('INSERT INTO price_recoveries VALUES(?,?,?,?,?,?,?,?,?,?)',
                (receipt_id, run_id, issue['symbol_id'], row['id'], issue['date'], stamp,
                 json.dumps(before, sort_keys=True, allow_nan=False), json.dumps(after, sort_keys=True, allow_nan=False),
                 json.dumps(['ref_price_raw']), json.dumps(evidence, sort_keys=True, allow_nan=False)))
            applied += 1
            charts.add(issue['symbol_id'])
        for sid in sorted(charts):
            # همه تاریخ‌ها فقط برای نمودار خوانده می‌شوند؛ خام دوره مهروموم عوض نمی‌شود و وارد پژوهش نمی‌شود.
            frame = pd.read_sql_query('''SELECT id,date,close_raw,close_final_raw,ref_price_raw
                FROM daily_prices WHERE symbol_id=? AND source='tsetmc' ORDER BY date''', db.conn, params=[sid])
            factors, adjusted = corporate_action_factors(frame), backward_adjusted_close(frame)
            updates = [(float(round(a, 4)) if pd.notna(a) else None, float(f) if pd.notna(f) else None, int(i))
                       for a, f, i in zip(adjusted, factors, frame.id)]
            db.conn.executemany('UPDATE daily_prices SET adj_close=?,adjustment_factor=? WHERE id=?', updates)
            display_rows += len(updates)
        before_commit()
        db.conn.commit()
    except Exception:
        db.conn.rollback()
        raise
    return dict(applied=applied, already_applied=already, display_rows_recomputed=display_rows,
                display_only_recomputation=True, sealed_raw_prices_changed=False)


def preserve_reference_recovery(receipt, incoming):
    evidence = json.loads(receipt['evidence_json'])
    if evidence.get('version') != VERSION:
        return None
    before, after = json.loads(receipt['before_json']), json.loads(receipt['after_json'])
    if (json.loads(receipt['fields_json']) == ['ref_price_raw'] and json_price_row(dict(receipt)) == after
            and incoming == before and valid_reference_change(before, after)):
        return {18: after['ref_price_raw']}
    return {}
