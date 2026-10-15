"""تصویر فقط‌خواندنی و سازگار از شواهد عمومی نماد؛ جدا از داده حساب."""

import pandas as pd

from core.screening import current_snapshot
from core.symbol_review import review_history
from core.timeutils import now_tehran


def load_symbol_review(db, symbol: str, *, at=None) -> dict:
    clock = at or now_tehran()
    own_snapshot = not db.conn.in_transaction
    if own_snapshot:
        db.conn.execute('BEGIN')
    try:
        meta = db.get_symbol(symbol)
        if not meta:
            raise ValueError('نماد یا نام شرکت یکتا شناسایی نشد')
        sid = meta['id']
        prices = db.get_daily_prices_df(meta['symbol_fa'])
        clients = db.get_client_types_df(meta['symbol_fa'])
        review = review_history(prices, clients)
        snapshot_row = db.conn.execute(
            "SELECT * FROM snapshots WHERE symbol_id=? AND source='tsetmc' ORDER BY captured_at DESC, id DESC LIMIT 1",
            (sid,)).fetchone()
        snapshot = dict(snapshot_row) if snapshot_row else None
        actions = db.conn.execute('SELECT COUNT(*) FROM corporate_actions WHERE symbol_id=?', (sid,)).fetchone()[0]
        announcements = [dict(r) for r in db.conn.execute(
            'SELECT tracing_no, title, published_at, captured_at, url FROM announcements WHERE symbol_id=? '
            'ORDER BY published_at DESC, id DESC LIMIT 20', (sid,)).fetchall()]
        review.update(symbol=meta['symbol_fa'], name=meta['name_fa'], ins_code=meta['ins_code'],
            asof=clock.isoformat(), recorded_action_rows=actions, announcements=announcements,
            snapshot=snapshot, snapshot_current=bool(snapshot and current_snapshot(snapshot['provider_timestamp'], at=clock)
                                                    and current_snapshot(snapshot['captured_at'], at=clock)),
            historical_membership_verified=False, historical_availability_verified=False,
            economic_actions_reconciled=False)
        if not prices.empty:
            # جلسه از مشاهدات بازار استنتاج می‌شود؛ تقویم رسمی یا علت توقف نیست.
            observed = pd.read_sql_query("""SELECT dp.date FROM daily_prices dp JOIN symbols s ON s.id=dp.symbol_id
                WHERE dp.source='tsetmc' AND s.instrument_type_code IN (300,303) AND dp.date>=? AND dp.date<=?
                AND substr(s.symbol_fa,-1,1) NOT GLOB '[0-9۰-۹٠-٩]'
                GROUP BY dp.date HAVING COUNT(DISTINCT dp.symbol_id)>=20 ORDER BY dp.date""",
                db.conn, params=(review['first'], review['last']))
            absent = sorted(set(observed.date)-set(prices.date))
            dates = pd.to_datetime(prices.date)
            gaps = dates.diff().dt.days
            end_index = gaps.idxmax() if gaps.notna().any() else None
            long_gap = None
            if end_index is not None:
                start, end = str(prices.date.loc[end_index-1]), str(prices.date.loc[end_index])
                long_gap = dict(start=start, end=end, calendar_days=int(gaps.loc[end_index]),
                    observed_market_sessions_without_symbol=sum(start < d < end for d in absent), cause_known=False)
            review.update(observed_market_sessions_without_symbol=len(absent), longest_calendar_gap=long_gap,
                          market_calendar_basis='>=20 known ordinary equities with recorded prices; not official calendar')
        return review
    finally:
        if own_snapshot:
            db.conn.rollback()
