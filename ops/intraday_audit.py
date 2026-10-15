"""Retrospective capture-quality diagnostics, never a forecast or a fill calibration."""

from datetime import date, datetime

import numpy as np
import pandas as pd

from core.risk import intraday_entry_constraints
from core.timeutils import TEHRAN_TZ, now_tehran_iso


def audit_intraday(db, session_date=None):
    """Read one consistent, local snapshot session. All times are interpreted in Tehran."""
    own_snapshot = not db.conn.in_transaction
    if own_snapshot:
        db.conn.execute('BEGIN')
    try:
        if session_date is None:
            session_date = db.conn.execute("SELECT substr(MAX(captured_at),1,10) FROM snapshots WHERE source='tsetmc'").fetchone()[0]
        if session_date is not None:
            date.fromisoformat(session_date)
        symbols = db.get_universe_symbols(include_inactive=True)
        ids = [s['id'] for s in symbols]
        expected = sum(bool(s['is_active']) for s in symbols)
        if ids and session_date:
            placeholders = ','.join('?' for _ in ids)
            frame = pd.read_sql_query(f"SELECT s.symbol_fa AS symbol, q.* FROM snapshots q JOIN symbols s ON s.id=q.symbol_id "
                f"WHERE q.source='tsetmc' AND substr(q.captured_at,1,10)=? AND q.symbol_id IN ({placeholders}) ORDER BY q.captured_at",
                db.conn, params=[session_date] + ids)
        else:
            frame = pd.DataFrame()
    finally:
        if own_snapshot:
            db.conn.rollback()
    result = dict(version='intraday-quality-v1', created_at=now_tehran_iso(), session_date=session_date,
                  scope='current ordinary-equity metadata, including inactive; not historical membership',
                  expected_current_active_symbols=expected, rows=len(frame), symbols=0, status='no_data',
                  full_session_coverage_known=False, independent_book_timestamp_available=False,
                  trading_state_feed_available=False, own_fill_calibration_available=False,
                  prospective_independence=False, per_symbol=[])
    if frame.empty:
        return result
    def aware(value):
        try:
            return datetime.fromisoformat(value).tzinfo is not None
        except (ValueError, TypeError):
            return False
    receive = pd.to_datetime(frame['captured_at'], format='ISO8601', utc=True, errors='coerce').where(frame['captured_at'].map(aware))
    provider = pd.to_datetime(frame['provider_timestamp'], format='ISO8601', utc=True, errors='coerce').where(frame['provider_timestamp'].map(aware))
    ages = (receive - provider).dt.total_seconds()
    observable = []
    for row, timestamp in zip(frame.to_dict('records'), receive):
        if pd.isna(timestamp):
            observable.append(False)
            continue
        entry = row.get('ask_price_1')
        if entry is None or not np.isfinite(entry) or entry <= 0:
            observable.append(False)
            continue
        blockers, caps = intraday_entry_constraints(row, float(entry), at=timestamp.to_pydatetime().astimezone(TEHRAN_TZ))
        observable.append(not blockers and bool(caps) and min(caps.values()) > 0)
    frame['received'], frame['age'], frame['observable'] = receive, ages, observable
    fresh = ages.between(-5, 45)
    result.update(status='descriptive_only', symbols=int(frame['symbol'].nunique()),
                  timestamp_unknown_rows=int((receive.isna() | provider.isna()).sum()),
                  provider_stale_rows=int((ages > 45).sum()), provider_future_rows=int((ages < -5).sum()),
                  provider_fresh_fraction=float(fresh.mean()), observable_ask_rows=int(sum(observable)))
    for symbol, group in frame.groupby('symbol', sort=True):
        gaps = group['received'].dropna().sort_values().diff().dt.total_seconds().dropna()
        known_ages = group['age'].dropna()
        result['per_symbol'].append(dict(symbol=symbol, rows=len(group),
            observed_start=group['received'].min().isoformat() if group['received'].notna().any() else None,
            observed_end=group['received'].max().isoformat() if group['received'].notna().any() else None,
            gap_p95_seconds=float(gaps.quantile(.95)) if len(gaps) else None,
            max_gap_seconds=float(gaps.max()) if len(gaps) else None,
            gaps_over_45_seconds=int((gaps > 45).sum()),
            provider_age_p95_seconds=float(known_ages.quantile(.95)) if len(known_ages) else None,
            observable_ask_rows=int(group['observable'].sum())))
    return result
