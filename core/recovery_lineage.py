"""Conservative, read-only provenance inventory for legacy trade-only price repairs."""

import json
import math
import re

from config import settings
from core.price_quality import RAW_PRICES
from core.timeutils import is_canonical_iso_date

VERSION = 'trade-recovery-lineage-v2'
_KNOWN_OTHER = {
    'native-price-revision-v1': ('NATIVEREV', ('core/price_revision.py', 'ops/price_revision.py'),
                                 {'open_raw', 'high_raw', 'low_raw', 'close_raw', 'volume', 'value', 'trade_count'}),
    'explicit-price-reference-v1': ('REFPRICE', ('core/price_reference.py', 'ops/reference_recovery.py'),
                                    {'ref_price_raw'}),
}
_LEGACY_CODE = ('core/price_recovery.py', 'ops/price_recovery.py', 'data_collector/trade_archive.py')
_FIELDS = {'open_raw', 'close_raw'}
_RAW = {'date', *RAW_PRICES, 'trade_count'}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key in recovery receipt')
        result[key] = value
    return result


def _constant(_):
    raise ValueError('Nonfinite JSON value in recovery receipt')


def _json(value):
    if not isinstance(value, str) or len(value) > 2_000_000:
        raise ValueError('Recovery receipt JSON is missing or too large')
    return json.loads(value, object_pairs_hook=_unique, parse_constant=_constant)


def _legacy_code(evidence):
    code = evidence.get('code')
    return (isinstance(code, dict) and all(isinstance(code.get(key), str)
            and re.fullmatch(r'[0-9a-f]{64}', code[key]) for key in _LEGACY_CODE))


def _valid_hash(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _known_other_receipt(row, evidence):
    """Validate metadata shape only; this does not certify another evidence path."""
    prefix, code_paths, allowed_fields = _KNOWN_OTHER[evidence['version']]
    code, artifacts = evidence.get('code'), evidence.get('artifacts_sha256')
    if (not isinstance(row['receipt_run_id'], str)
            or not re.fullmatch(prefix + r'-[0-9a-f]{32}', row['receipt_run_id'])
            or not isinstance(code, dict) or not all(_valid_hash(code.get(key)) for key in code_paths)
            or any(not isinstance(key, str) or not key or not _valid_hash(value) for key, value in code.items())
            or not isinstance(artifacts, dict) or not artifacts
            or any(not isinstance(key, str) or not key or not _valid_hash(value) for key, value in artifacts.items())
            or not _valid_hash(evidence.get('plan_sha256')) or not _valid_hash(evidence.get('result_sha256'))
            or artifacts.get('plan.json') != evidence['plan_sha256']
            or evidence.get('source_independently_verified') is not False
            or evidence.get('historical_availability_verified') is not False
            or not isinstance(evidence.get('review_note'), str)
            or not 10 <= len(evidence['review_note'].strip()) <= 2000):
        raise ValueError('Known recovery metadata is incomplete or inconsistent')
    if (row['price_id'] is None or row['price_symbol_id'] != row['receipt_symbol_id']
            or row['price_date'] != row['receipt_date'] or row['source'] != 'tsetmc' or row['price_unit'] != 'Rial'):
        raise ValueError('Known recovery receipt is not linked to the expected public price row')
    before, after, fields = (_json(row[key]) for key in ('before_json', 'after_json', 'fields_json'))
    if (not isinstance(before, dict) or not isinstance(after, dict) or set(before) != _RAW or set(after) != _RAW
            or before.get('date') != row['receipt_date'] or after.get('date') != row['receipt_date']
            or not isinstance(fields, list) or not fields or len(set(fields)) != len(fields)
            or not set(fields) <= allowed_fields or {key for key in _RAW if before[key] != after[key]} != set(fields)):
        raise ValueError('Known recovery changes are incomplete or inconsistent')
    for key in _RAW - {'date'}:
        for value in (before[key], after[key]):
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
                raise ValueError('Known recovery raw value is not finite or unknown')
    if any(after[key] is None for key in fields):
        raise ValueError('Known recovery endpoint is unknown')


def _changed_fields(before, after, fields, day):
    if (not isinstance(before, dict) or not isinstance(after, dict) or set(before) != _RAW
            or set(after) != _RAW or before.get('date') != day or after.get('date') != day
            or not isinstance(fields, list) or not fields or len(set(fields)) != len(fields)
            or not set(fields) <= _FIELDS):
        raise ValueError('Malformed trade recovery receipt')
    changed = {key for key in _RAW if before[key] != after[key]}
    if not changed or not changed <= set(fields):
        raise ValueError('Recovery receipt has unexpected changed fields')
    for key in changed:
        value = after[key]
        if (before[key] is not None or type(value) not in (int, float)
                or not math.isfinite(value) or not 0 < value < 2**53):
            raise ValueError('Recovered endpoint is not a positive replacement for an unknown value')
    return changed


def inspect_recovery_lineage(db, *, cutoff, development_start):
    """Count retained pre-holdout trade-only origins; do not infer accuracy or conflicts.

    Only market recovery receipts and linked public price rows before the explicit
    cutoff are read. Unknown or malformed lineage remains a blocker. A source
    value later replacing every recovered endpoint is classified as superseded;
    a still-retained endpoint remains unresolved even if another record exists.
    """
    if (not is_canonical_iso_date(cutoff) or cutoff > settings.RESEARCH_CONFIG['holdout_start']
            or not is_canonical_iso_date(development_start) or development_start >= cutoff):
        raise ValueError('A pre-holdout cutoff and earlier development start are required')
    own_snapshot = not db.conn.in_transaction
    if own_snapshot:
        db.conn.execute('BEGIN')
    try:
        rows = db.conn.execute('''SELECT r.id AS receipt_id,r.run_id AS receipt_run_id,r.symbol_id AS receipt_symbol_id,
            r.date AS receipt_date,r.before_json,r.after_json,r.fields_json,r.evidence_json,
            p.id AS price_id,p.symbol_id AS price_symbol_id,p.date AS price_date,p.source,p.price_unit,
            p.open_raw,p.close_raw,s.symbol_fa,s.instrument_type_code
            FROM price_recoveries r LEFT JOIN daily_prices p ON p.id=r.daily_price_id
            LEFT JOIN symbols s ON s.id=r.symbol_id WHERE r.date < ? ORDER BY r.id''', (cutoff,)).fetchall()
        receipt_count = unrecognized = malformed = excluded = 0
        states = {}
        rank = {'superseded': 0, 'partial': 1, 'all': 2}
        for row in rows:
            kind, symbol, day = row['instrument_type_code'], row['symbol_fa'], row['receipt_date']
            if kind is not None and (kind not in (300, 303) or isinstance(symbol, str) and symbol[-1:].isdigit()):
                excluded += 1
                continue
            if (kind not in (300, 303) or not isinstance(symbol, str) or not symbol
                    or not is_canonical_iso_date(day)):
                malformed += 1
                continue
            try:
                evidence = _json(row['evidence_json'])
                if not isinstance(evidence, dict):
                    raise ValueError('Unknown recovery evidence')
                if evidence.get('version') in _KNOWN_OTHER:
                    _known_other_receipt(row, evidence)
                    continue  # Structurally recognized, not certified by this inventory.
                if 'version' in evidence or not _legacy_code(evidence):
                    unrecognized += 1
                    continue
                receipt_count += 1
                before, after, fields = (_json(row[key]) for key in ('before_json', 'after_json', 'fields_json'))
                changed = _changed_fields(before, after, fields, day)
                if (row['price_id'] is None or row['price_symbol_id'] != row['receipt_symbol_id']
                        or row['price_date'] != day or row['source'] != 'tsetmc' or row['price_unit'] != 'Rial'):
                    raise ValueError('Recovery receipt is no longer linked to the expected public price row')
                current = {key: row[key] for key in changed}
                if any(value is not None and (type(value) not in (int, float) or not math.isfinite(value)
                                              or not 0 < value < 2**53)
                       for value in current.values()):
                    raise ValueError('Current recovered endpoint is not numeric or unknown')
                retained = sum(current[key] == after[key] for key in changed)
                state = 'all' if retained == len(changed) else 'partial' if retained else 'superseded'
                key = (row['receipt_symbol_id'], day)
                previous = states.get(key)
                if previous is None or rank[state] > rank[previous]:
                    states[key] = state
            except (ValueError, TypeError, KeyError, OverflowError, json.JSONDecodeError):
                malformed += 1
        retained = {key for key, value in states.items() if value != 'superseded'}
        clear = not retained and not unrecognized and not malformed
        return dict(version=VERSION, cutoff_exclusive=cutoff, development_start=development_start,
            legacy_trade_receipts=receipt_count, retained_all_rows=sum(v == 'all' for v in states.values()),
            retained_partial_rows=sum(v == 'partial' for v in states.values()),
            superseded_rows=sum(v == 'superseded' for v in states.values()),
            retained_development_rows=sum(day >= development_start for _, day in retained),
            retained_prior_rows=sum(day < development_start for _, day in retained),
            unrecognized_receipts=unrecognized, malformed_receipts=malformed, excluded_out_of_scope=excluded,
            review_clear=clear, status='no_retained_trade_only_rows' if clear else 'unresolved',
            source_conflict_count_known=False, independent_source_correctness_verified=False,
            historical_availability_verified=False, accepted_for_signals=False)
    finally:
        if own_snapshot:
            db.conn.rollback()
