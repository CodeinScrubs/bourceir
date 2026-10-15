"""Append-only local imports. Never fabricate fills from holdings or overwrite market quotes."""

import hashlib
import json
from datetime import datetime
from pathlib import Path

from core.timeutils import now_tehran, now_tehran_iso


class BrokerRepository:
    def __init__(self, db):
        self.db = db

    def store_report(self, data: bytes, filename: str, parsed, *, parser_version: str):
        """Persist a report already validated by the collection layer."""
        digest = hashlib.sha256(data).hexdigest()
        key = hashlib.sha256(f'{parser_version}:{parsed["kind"]}:{digest}'.encode()).hexdigest()
        metadata = {k: v for k, v in parsed.items() if k != 'rows'}
        conn = self.db.conn
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            if not conn.execute('SELECT 1 FROM broker_imports WHERE id=?', (key,)).fetchone():
                conn.execute('INSERT INTO broker_imports VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                             (key, parsed['kind'], digest, parser_version, parsed['captured_at'], now_tehran_iso(),
                              Path(filename).name, json.dumps(metadata, ensure_ascii=False, allow_nan=False)))
                conn.executemany('INSERT INTO broker_records VALUES (?, ?, ?)',
                                 [(key, i, json.dumps(row, ensure_ascii=False, allow_nan=False))
                                  for i, row in enumerate(parsed['rows'])])
        return key

    def latest(self, kind):
        row = self.db.conn.execute('SELECT * FROM broker_imports WHERE kind=? ORDER BY captured_at DESC, imported_at DESC, rowid DESC LIMIT 1',
                                   (kind,)).fetchone()
        if row is None:
            return None
        result = {**dict(row), **json.loads(row['metadata_json'])}
        result['rows'] = [json.loads(x[0]) for x in self.db.conn.execute(
            'SELECT record_json FROM broker_records WHERE import_id=? ORDER BY ordinal', (row['id'],))]
        return result

    def adopt_baseline(self, import_id, max_loss_fraction=0.1):
        """Explicit adoption only. Later imports never reset the loss floor automatically."""
        if not 0 < max_loss_fraction < 1:
            raise ValueError('سقف زیان نامعتبر است')
        conn = self.db.conn
        conn.execute('BEGIN IMMEDIATE')
        try:
            source = conn.execute("SELECT metadata_json FROM broker_imports WHERE id=? AND kind='portfolio'", (import_id,)).fetchone()
            if source is None:
                raise ValueError('اسنپ‌شات معتبر سبد لازم است')
            if conn.execute('SELECT COUNT(*) FROM journal_executions').fetchone()[0]:
                raise ValueError('شروع مرحله جدید با دفتر موجود نیازمند انتقال مستقل دفتر است')
            metadata = json.loads(source[0])
            if datetime.fromisoformat(metadata['captured_at']).date() != now_tehran().date():
                raise ValueError('مبنای مرحله جدید به برداشت معتبر همان روز نیاز دارد')
            capital = metadata['summary']['net_assets_rial']
            limit = int(capital * max_loss_fraction)
            if capital <= 0 or not 0 < limit < capital:
                raise ValueError('سرمایه مبنا نامعتبر است')
            stamp = now_tehran_iso()
            previous = conn.execute('SELECT * FROM broker_baselines ORDER BY id DESC LIMIT 1').fetchone()
            if previous and previous['portfolio_import_id'] == import_id and previous['max_loss_rial'] == limit:
                conn.rollback()
                return previous['id']
            conn.execute('INSERT INTO personal_profile VALUES (1, ?, ?, 0, ?) ON CONFLICT(id) DO UPDATE SET '
                         'capital_rial=excluded.capital_rial,max_loss_rial=excluded.max_loss_rial,actual_history_complete=0,updated_at=excluded.updated_at',
                         (capital, limit, stamp))
            cursor = conn.execute('INSERT INTO broker_baselines(portfolio_import_id,adopted_at,capital_rial,max_loss_rial) VALUES(?,?,?,?)',
                                  (import_id, stamp, capital, limit))
            conn.commit()
            return cursor.lastrowid
        except Exception:
            conn.rollback()
            raise

    def baseline(self):
        row = self.db.conn.execute('SELECT * FROM broker_baselines ORDER BY id DESC LIMIT 1').fetchone()
        return dict(row) if row else None

    def valuation_metadata(self, symbol):
        known = self.db.get_symbol(symbol)
        if known:
            return known
        row = self.db.conn.execute('SELECT payload_json FROM account_market_observations WHERE symbol_fa=? '
                                   'ORDER BY observed_at DESC, id DESC LIMIT 1', (symbol,)).fetchone()
        return json.loads(row[0]) if row else None

    def record_market_observation(self, metadata, prices):
        from datetime import date

        from core.risk import finite_number
        from core.text import normalize_symbol

        symbol = normalize_symbol(metadata['symbol_fa'])
        if date.fromisoformat(prices['date']) > now_tehran().date():
            raise ValueError('تاریخ مشاهده قیمت عمومی در آینده است')
        last_price = finite_number(prices['close_raw'])
        if not symbol or last_price > 9_000_000_000_000_000:
            raise ValueError('مشاهده قیمت عمومی نامعتبر است')
        payload = dict(symbol_fa=symbol, date=prices['date'], close_raw=last_price,
                       sector_code=metadata.get('sector_code'), instrument_type_code=metadata.get('instrument_type_code'),
                       source='tsetmc', ins_code=str(metadata['ins_code']))
        with self.db.conn:
            self.db.conn.execute('INSERT INTO account_market_observations(symbol_fa,ins_code,observed_at,payload_json) VALUES(?,?,?,?)',
                                 (symbol, payload['ins_code'], now_tehran_iso(), json.dumps(payload, ensure_ascii=False, allow_nan=False)))


def broker_report(db):
    from core.broker_account import reconcile_account

    repo = BrokerRepository(db)
    market = [dict(r) for r in db.conn.execute(
        "SELECT s.symbol_fa,s.sector_code,s.instrument_type_code,p.date,p.close_raw,p.captured_at AS quote_observed_at "
        "FROM symbols s LEFT JOIN daily_prices p ON p.id=(SELECT q.id FROM daily_prices q "
        "WHERE q.symbol_id=s.id AND q.source='tsetmc' ORDER BY q.date DESC LIMIT 1)")]
    by_symbol = {r['symbol_fa']: r for r in market}
    for row in db.conn.execute('SELECT payload_json,observed_at FROM account_market_observations ORDER BY observed_at, id'):
        quote = json.loads(row[0])
        quote['quote_observed_at'] = row[1]
        current = by_symbol.get(quote['symbol_fa'])
        def stamp(value):
            try:
                parsed = datetime.fromisoformat(value)
                return parsed.timestamp() if parsed.tzinfo is not None else float('-inf')
            except (ValueError, TypeError):
                return float('-inf')
        newer_same_date = bool(current and quote['date'] == current['date']
                               and stamp(quote['quote_observed_at']) >= stamp(current.get('quote_observed_at')))
        if current is None or current['date'] is None or quote['date'] > current['date'] or newer_same_date:
            by_symbol[quote['symbol_fa']] = quote
    sources = {kind: repo.latest(kind) for kind in ('portfolio', 'turnover', 'fills')}
    report = reconcile_account(sources['portfolio'], sources['turnover'], sources['fills'],
                               market_rows=list(by_symbol.values()), baseline=repo.baseline())
    if report is not None:
        report['sources'] = {kind: {key: source[key] for key in ('id', 'source_sha256', 'parser_version', 'source_name', 'captured_at')}
                             for kind, source in sources.items() if source}
    return report
