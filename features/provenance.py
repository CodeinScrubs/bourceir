"""Content identity of raw feature inputs, partitioned by date to preserve the holdout seal."""

import hashlib
import json

import numpy as np
import pandas as pd

from config import settings
from database.db import CLIENT_TYPE_COLUMNS

INPUT_VERSION = 'raw-inputs-v1'
PRICE_COLUMNS = ['open_raw', 'high_raw', 'low_raw', 'close_raw', 'close_final_raw',
                 'ref_price_raw', 'volume', 'value', 'trade_count']


def feature_logic_fingerprint():
    """Feature computation dependencies, excluding unrelated account/UI/research code."""
    digest = hashlib.sha256()
    files = ('features/daily.py', 'features/market.py', 'features/store.py', 'features/provenance.py',
             'core/market_rules.py', 'core/text.py', 'core/timeutils.py', 'database/db.py', 'config/settings.py')
    for name in files:
        digest.update(name.encode())
        digest.update((settings.BASE_DIR / name).read_bytes().replace(b'\r\n', b'\n'))
    # Only runtime options used by feature calculation; changing a research cutoff is allowed.
    digest.update(json.dumps({'lock_min_move_pct': settings.BACKTEST_CONFIG['lock_min_move_pct']},
                             sort_keys=True).encode())
    return digest.hexdigest()


def raw_input_snapshot(db, *, symbols=None, end_exclusive=None):
    """Caller holds a SQLite read transaction shared with the actual input reads.

    Hash raw values rather than ingestion times: an identical re-fetch remains compatible.
    A date partition is a SHA256 of sorted fixed-width records (SHA256 symbol + float64
    raw columns, canonical NaN). No backward adjusted or private account columns are read.
    """
    universe = db.get_universe_symbols(include_inactive=True)
    wanted = None if symbols is None else set(symbols)
    ids = [s['id'] for s in universe if wanted is None or s['symbol_fa'] in wanted]
    tables = {}
    for table, columns in (('daily_prices', PRICE_COLUMNS), ('client_types_daily', CLIENT_TYPE_COLUMNS)):
        digests, counts = {}, {}
        if ids:
            placeholders = ','.join('?' for _ in ids)
            query = (f"SELECT s.symbol_fa AS symbol, d.date, {', '.join('d.' + c for c in columns)} "
                     f"FROM {table} d JOIN symbols s ON s.id=d.symbol_id "
                     f"WHERE d.symbol_id IN ({placeholders}) AND d.source='tsetmc' "
                     "AND (? IS NULL OR d.date < ?) ORDER BY d.date, s.symbol_fa")
            symbol_hashes = {}
            dtype = np.dtype([('symbol', 'V32')] + [(c, '<f8') for c in columns])
            for chunk in pd.read_sql_query(query, db.conn, params=ids + [end_exclusive, end_exclusive], chunksize=100000):
                for day, rows in chunk.groupby('date', sort=False):
                    packed = np.empty(len(rows), dtype=dtype)
                    for i, symbol in enumerate(rows['symbol']):
                        if symbol not in symbol_hashes:
                            symbol_hashes[symbol] = hashlib.sha256(symbol.encode('utf-8')).digest()
                        packed['symbol'][i] = symbol_hashes[symbol]
                    for column in columns:
                        values = pd.to_numeric(rows[column], errors='raise').to_numpy(dtype='float64', na_value=np.nan, copy=True)
                        values[np.isnan(values)] = np.nan
                        values[values == 0] = 0.0
                        packed[column] = values
                    digests.setdefault(day, hashlib.sha256()).update(packed.tobytes())
                    counts[day] = counts.get(day, 0) + len(rows)
        tables[table] = {day: dict(sha256=digest.hexdigest(), rows=counts[day]) for day, digest in digests.items()}
    return dict(version=INPUT_VERSION, tables=tables)


def assert_compatible_inputs(db, manifest, *, end_exclusive=None):
    """Reject stale raw values, changed membership or changed feature logic before research."""
    provenance = manifest.get('provenance') or {}
    expected = provenance.get('raw_inputs')
    if not expected or expected.get('version') != INPUT_VERSION:
        raise ValueError('منشأ داده خام انبار فیچر موجود نیست؛ ابتدا ساخت فیچر را دوباره اجرا کنید')
    if provenance.get('feature_logic_sha256') != feature_logic_fingerprint():
        raise ValueError('منشأ منطق فیچر تغییر کرده است؛ ابتدا ساخت فیچر را دوباره اجرا کنید')
    expected_tables = {
        table: {day: value for day, value in partitions.items() if not end_exclusive or day < end_exclusive}
        for table, partitions in expected['tables'].items()
    }
    actual = raw_input_snapshot(db, end_exclusive=end_exclusive)
    if actual['tables'] != expected_tables:
        raise ValueError('داده خام با منشأ فیچر یکسان نیست؛ ابتدا ساخت فیچر را دوباره اجرا کنید')
    return actual
