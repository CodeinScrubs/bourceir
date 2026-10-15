"""Read ordinary-equity prices and client types from one SQLite statement/snapshot."""

import pandas as pd

from database.db import CLIENT_TYPE_COLUMNS


def load_flow_inputs(db, *, before=None, tail_per_symbol=None, include_inactive=True):
    if tail_per_symbol is not None and (isinstance(tail_per_symbol, bool)
            or not isinstance(tail_per_symbol, int) or tail_per_symbol < 1):
        raise ValueError('تعداد مشاهدات باید صحیح و مثبت باشد')
    symbols = [s for s in db.get_universe_symbols(include_inactive=include_inactive)
               if s['instrument_type_code'] in (300, 303)]
    ids = [s['id'] for s in symbols]
    price_cols = ['symbol', 'date', 'open_raw', 'high_raw', 'low_raw', 'close_raw', 'close_final_raw',
                  'ref_price_raw', 'volume', 'value', 'trade_count']
    if not ids:
        return pd.DataFrame(columns=price_cols), pd.DataFrame(columns=['symbol', 'date'] + CLIENT_TYPE_COLUMNS)
    placeholders = ','.join('?' for _ in ids)
    query = f"""WITH scoped AS (
        SELECT dp.*, ROW_NUMBER() OVER (PARTITION BY symbol_id ORDER BY date DESC) AS row_number
        FROM daily_prices dp WHERE source='tsetmc' AND symbol_id IN ({placeholders})
        AND (? IS NULL OR date < ?))
        SELECT s.symbol_fa AS symbol, p.date, {', '.join('p.'+c for c in price_cols[2:])},
               {', '.join('ct.'+c for c in CLIENT_TYPE_COLUMNS)}
        FROM scoped p JOIN symbols s ON s.id=p.symbol_id
        LEFT JOIN client_types_daily ct ON ct.symbol_id=p.symbol_id AND ct.date=p.date AND ct.source='tsetmc'
        WHERE (? IS NULL OR p.row_number <= ?) ORDER BY s.symbol_fa, p.date"""
    frame = pd.read_sql_query(query, db.conn, params=ids + [before, before, tail_per_symbol, tail_per_symbol])
    return frame[price_cols].copy(), frame[['symbol', 'date'] + CLIENT_TYPE_COLUMNS].copy()
