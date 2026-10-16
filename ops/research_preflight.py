"""بررسی فقط‌خواندنی داده توسعه؛ دوره مهر و موم باز و هیچ روش معاملاتی پذیرفته نمی‌شود."""

import argparse
import hashlib
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from config import settings
from core.client_quality import validated_client_data
from core.timeutils import is_canonical_iso_date, now_tehran_iso
from features.provenance import feature_environment, feature_logic_fingerprint, raw_input_snapshot
from ops.report_files import private_report_path, write_private_report

VERSION = 'research-preflight-v2'
RAW_PRICES = ('open_raw', 'high_raw', 'low_raw', 'close_raw', 'close_final_raw', 'ref_price_raw', 'volume', 'value')


def research_preflight(db, *, before=None):
    diagnostic_hash = hashlib.sha256(Path(__file__).read_bytes().replace(b'\r\n', b'\n')).hexdigest()
    logic_hash = feature_logic_fingerprint()
    cutoff = settings.RESEARCH_CONFIG['holdout_start'] if before is None else before
    if (not is_canonical_iso_date(cutoff)
            or cutoff > settings.RESEARCH_CONFIG['holdout_start']):
        raise ValueError('بررسی فقط برای دوره توسعه است؛ مرز دوره مهر و موم را جلو نبرید')
    end = (date.fromisoformat(cutoff) - timedelta(days=1)).isoformat()
    own_snapshot = not db.conn.in_transaction
    if own_snapshot:
        db.conn.execute('BEGIN')
    try:
        universe = [s for s in db.get_universe_symbols(include_inactive=True) if s['instrument_type_code'] in (300, 303)]
        records = []
        for symbol in universe:
            p = db.get_daily_prices_df(symbol['symbol_fa'], end_date=end)
            c = db.get_client_types_df(symbol['symbol_fa'], end_date=end)
            record = dict(symbol=symbol['symbol_fa'], instrument_type_code=symbol['instrument_type_code'],
                          price_rows=len(p), date_min=None, date_max=None, duplicate_dates=0,
                          client_rows=len(c), invalid_price_dates=0, invalid_client_dates=0, duplicate_client_dates=0,
                          invalid_price_rows=0, unknown_client_pairs=len(p), coherent_client_pairs=0,
                          invalid_optional_legal_cells=0)
            if not c.empty:
                record['invalid_client_dates'] = int((~c.date.map(is_canonical_iso_date)).sum())
                record['duplicate_client_dates'] = int(c.date.duplicated().sum())
            if not p.empty:
                record.update(date_min=p.date.min(), date_max=p.date.max(), duplicate_dates=int(p.date.duplicated().sum()))
                record['invalid_price_dates'] = int((~p.date.map(is_canonical_iso_date)).sum())
                numeric = p[list(RAW_PRICES)].apply(pd.to_numeric, errors='coerce')
                valid = pd.Series(np.isfinite(numeric.to_numpy()).all(axis=1), index=p.index)
                valid &= numeric[list(RAW_PRICES)].gt(0).all(axis=1)
                valid &= numeric.volume.eq(np.floor(numeric.volume))
                valid &= numeric.low_raw.le(numeric.high_raw)
                valid &= numeric.open_raw.between(numeric.low_raw, numeric.high_raw)
                valid &= numeric.close_raw.between(numeric.low_raw, numeric.high_raw)
                implied = numeric.value / numeric.volume
                valid &= implied.between(numeric.low_raw - 1., numeric.high_raw + 1.)
                record['invalid_price_rows'] = int((~valid).sum())
                if not any(record[k] for k in ('duplicate_dates', 'duplicate_client_dates',
                                               'invalid_price_dates', 'invalid_client_dates')):
                    clean = validated_client_data(p, c)
                    paired = clean.buy_i_value.notna() & clean.sell_i_value.notna()
                    record['coherent_client_pairs'] = int(paired.sum())
                    record['unknown_client_pairs'] = int((~paired).sum())
                    quality = [f'{side}_legal_{kind}_quality' for side in ('buy', 'sell') for kind in ('volume', 'value')]
                    record['invalid_optional_legal_cells'] = int(clean[quality].eq('invalid').sum().sum())
            records.append(record)
        identity = raw_input_snapshot(db, symbols=[s['symbol_fa'] for s in universe], end_exclusive=cutoff)
        schema = db.schema_version()
    finally:
        if own_snapshot:
            db.conn.rollback()
    rows = sum(r['price_rows'] for r in records)
    blockers = ['historical_membership_unverified', 'economic_corporate_entitlements_unverified',
                'historical_fee_schedule_unverified', 'execution_feasibility_unverified']
    if not rows:
        blockers.append('no_development_price_rows')
    if any(r['invalid_price_rows'] or r['duplicate_dates'] for r in records):
        blockers.append('invalid_development_prices')
    alignment_errors = any(r[k] for r in records for k in ('invalid_price_dates', 'invalid_client_dates',
                                                         'duplicate_client_dates'))
    if alignment_errors:
        blockers.append('invalid_development_date_alignment')
    try:
        environment = feature_environment()
    except ValueError as exc:
        environment = dict(error=str(exc))
        blockers.append('environment_mismatch')
    prices_passed = bool(rows) and not any(r['invalid_price_rows'] or r['duplicate_dates']
                                          or r['invalid_price_dates'] for r in records)
    contract_passed = prices_passed and not alignment_errors and 'environment_mismatch' not in blockers
    if (logic_hash != feature_logic_fingerprint() or diagnostic_hash !=
            hashlib.sha256(Path(__file__).read_bytes().replace(b'\r\n', b'\n')).hexdigest()):
        raise ValueError('کد هنگام بررسی تغییر کرد؛ گزارش منتشر نشد')
    return dict(version=VERSION, generated_at=now_tehran_iso(), end_exclusive=cutoff, database_schema=schema,
                universe_count=len(universe), observed_symbols=sum(r['price_rows'] > 0 for r in records), price_rows=rows,
                coherent_client_pairs=sum(r['coherent_client_pairs'] for r in records),
                unknown_client_pairs=sum(r['unknown_client_pairs'] for r in records),
                records=records, raw_inputs=identity, feature_environment=environment,
                feature_logic_sha256=logic_hash, diagnostic_code_sha256=diagnostic_hash, blockers=blockers,
                no_raw_price_errors=prices_passed, input_contract_passed=contract_passed,
                status='observed_contract_passed' if contract_passed else 'blocked',
                client_value_pairs_complete=bool(rows) and sum(r['unknown_client_pairs'] for r in records) == 0,
                accepted_for_signals=False, profitability_proven=False,
                limitations=['Current known registry is not point-in-time membership.',
                             'Unknown client pairs remain in the denominator; they are not zero flow.',
                             'No expected trading calendar or cause of missing sessions is inferred.',
                             'Observed numeric consistency is not independent source verification.'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before', default=None)
    parser.add_argument('--json', default='data/private/research-preflight.json')
    args = parser.parse_args(argv)
    from database.db import DatabaseManager
    try:
        target = private_report_path(args.json, forbidden_paths=(settings.DB_PATH,))
        with DatabaseManager(read_only=True) as db:
            report = research_preflight(db, before=args.before)
        write_private_report(target, report, forbidden_paths=(settings.DB_PATH,))
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(2, str(exc) + '\n')
    print(json.dumps({k: report[k] for k in ('universe_count', 'price_rows', 'input_contract_passed', 'blockers')}))
    return 0 if report['input_contract_passed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
