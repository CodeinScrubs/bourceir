"""پورتفوی مجازی مستقل، تحلیل پیشین و دفتر فقط‌افزودنی؛ بدون ارتباط با سفارش کارگزاری."""

import json
import math
import uuid
from datetime import timedelta

from config import settings
from core.paper import (
    HORIZONS,
    MODES,
    SUPPORTED_VERSIONS,
    VERSION,
    aware,
    canonical,
    digest,
    event_digest,
    integer,
    portfolio_state,
    quote_fill,
    text,
)
from core.risk import finite_number
from core.screening import current_daily_row, current_snapshot, expected_completed_session
from core.text import normalize_symbol
from core.timeutils import TEHRAN_TZ, is_trading_session, now_tehran
from database.broker import BrokerRepository

CODE_FILES = ('core/paper.py', 'core/journal.py', 'core/risk.py', 'core/screening.py',
              'core/market_rules.py', 'core/client_quality.py', 'core/timeutils.py', 'database/paper.py',
              'core/text.py', 'config/settings.py', 'database/broker.py',
              'database/migrations/013_paper_portfolios.sql', 'database/migrations/014_paper_opening_holdings.sql',
              'requirements.lock.txt')


def code_identity():
    sources = {name: (settings.BASE_DIR / name).read_text(encoding='utf-8').replace('\r\n', '\n') for name in CODE_FILES}
    return dict(sha256=digest(sources), sources=sources, self_contained_replay=False)


def _clean(row):
    return None if row is None else {k: None if isinstance(v, float) and not math.isfinite(v) else v
                                     for k, v in dict(row).items()}


class PaperRepository:
    def __init__(self, db):
        self.db = db

    def portfolios(self):
        return [dict(r) for r in self.db.conn.execute('SELECT * FROM paper_portfolios ORDER BY created_at,id')]

    def _protocol(self, *, name, capital_rial, rationale, mode):
        name, rationale = text(name, 'نام پورتفو', 120), text(rationale, 'روش و معیار ارزیابی')
        capital = integer(capital_rial)
        if capital < 10 or mode not in MODES:
            raise ValueError('سرمایه حداقل ده ریال و نوع پورتفوی معتبر لازم است')
        fees = {side: finite_number(settings.FEES[side], positive=False) for side in ('buy', 'sell')}
        slip = finite_number(settings.BACKTEST_CONFIG['default_slippage_bps'], positive=False)
        participation = min(.05, finite_number(settings.BACKTEST_CONFIG['max_volume_participation_pct']))
        if max(fees.values()) >= 1 or slip >= 10000:
            raise ValueError('کارمزد یا لغزش تنظیم‌شده معتبر نیست')
        key, at = 'PAPER-' + uuid.uuid4().hex, now_tehran().isoformat()
        protocol = dict(id=key, name=name, created_at=at, capital_rial=capital, max_loss_rial=capital // 10,
                        mode=mode, rationale=rationale, version=VERSION, project_version=settings.VERSION,
                        fees=fees, slippage_bps=slip, participation=participation, code=code_identity(),
                        trading_hours=dict(settings.TRADING_HOURS), trading_days=list(settings.TRADING_DAYS),
                        accepted_for_signals=False, profitability_proven=False, externally_timestamped=False,
                        cash_interest_rial=0, corporate_action_model='unresolved_blocks_performance',
                        actual_account_linked=False, opening_kind='cash',
                        valuation_policy='fresh_min_last_bid_else_archived_last_trade')
        return protocol

    def _insert_protocol(self, protocol):
        self.db.conn.execute('INSERT INTO paper_portfolios VALUES(?,?,?,?,?,?,?,?)',
                             tuple(protocol[k] for k in ('id', 'name', 'created_at', 'capital_rial', 'max_loss_rial', 'mode'))
                             + (canonical(protocol), digest(protocol)))

    def create(self, *, name, capital_rial, rationale, mode='manual'):
        protocol = self._protocol(name=name, capital_rial=capital_rial, rationale=rationale, mode=mode)
        with self.db.conn:
            self._insert_protocol(protocol)
        return protocol['id']

    def _source(self, import_id):
        row = self.db.conn.execute("SELECT * FROM broker_imports WHERE id=? AND kind='portfolio'", (import_id,)).fetchone()
        if row is None:
            raise ValueError('گزارش معتبر سبد فارابی پیدا نشد')
        source = {**dict(row), **json.loads(row['metadata_json'])}
        source.pop('metadata_json')
        source['rows'] = [json.loads(r[0]) for r in self.db.conn.execute(
            'SELECT record_json FROM broker_records WHERE import_id=? ORDER BY ordinal', (import_id,))]
        return source

    def create_from_broker(self, import_id, *, name='کپی مجازی سبد فارابی', mode='observed', request_id=None):
        request = dict(import_id=import_id, name=text(name, 'نام پورتفو', 120), mode=mode)
        request_id = text(request_id or 'broker-opening-' + import_id, 'شناسه درخواست', 160)
        with self.db.conn:
            self.db.conn.execute('BEGIN IMMEDIATE')
            prior = self.db.conn.execute('SELECT * FROM paper_openings WHERE request_id=?', (request_id,)).fetchone()
            if prior:
                protocol, _, _, _ = self._load(prior['portfolio_id'])
                if self._opening(protocol)['request'] != request:
                    raise ValueError('شناسه تکراری برای کپی متفاوت استفاده شده است')
                return prior['portfolio_id']
            source = self._source(import_id)
            at = now_tehran().astimezone(TEHRAN_TZ)
            age = (at - aware(source['captured_at'])).total_seconds()
            if not 0 <= age <= 900:
                raise ValueError('کپی سبد جاری به برداشت حداکثر ۱۵ دقیقه اخیر نیاز دارد؛ ابتدا فارابی را دوباره بخوان')
            rows, summary = source['rows'], source['summary']
            if not rows or len(rows) != source['expected_positions'] or len({normalize_symbol(r['symbol_fa']) for r in rows}) != len(rows):
                raise ValueError('تعداد یا نام‌های گزارش سبد سازگار نیستند')
            cash = finite_number(summary['cash_rial'], positive=False)
            power = finite_number(summary['buying_power_rial'], positive=False)
            blocked = finite_number(summary['blocked_rial'], positive=False)
            if not all(v.is_integer() for v in (cash, power, blocked)) or blocked > cash:
                raise ValueError('نقد یا وجه بلوکه‌شده نیازمند تطبیق است؛ اعتبار به پول مجازی تبدیل نمی‌شود')
            available = int(min(power, cash - blocked))
            capital = integer(summary['net_assets_rial'])
            rounding = capital - int(cash) - sum(r['value_rial'] for r in rows)
            if abs(rounding) > len(rows) + 1 or abs(capital - cash - summary['portfolio_value_rial']) > 1:
                raise ValueError('ارزش سبد و نقد با خلاصه فارابی سازگار نیست')
            protocol = self._protocol(name=request['name'], capital_rial=capital,
                                      rationale='کپی موجودی گزارش‌شده فارابی؛ سنجش تغییرات از شروع کپی، بدون بازسازی خریدهای گذشته', mode=mode)
            key, created = protocol['id'], protocol['created_at']
            positions, passive, opening_ideas = {}, [], []
            for row in rows:
                symbol = normalize_symbol(row['symbol_fa'])
                quantity = integer(row['quantity'])
                finite_number(row['value_rial'], positive=False)
                finite_number(row['last_price_rial'], positive=False)
                meta = BrokerRepository(self.db).valuation_metadata(symbol)
                known = self.db.get_symbol(symbol)
                kind = meta.get('instrument_type_code') if meta else None
                eligible = bool(known and known['instrument_type_code'] in (300, 303) and known['is_active']
                                and row['value_rial'] > 0 and row['last_price_rial'] > 0)
                holding = dict(symbol_fa=symbol, quantity=quantity, cost_rial=row['value_rial'],
                               source_price_rial=row['last_price_rial'], instrument_type_code=kind,
                               ins_code=str(meta['ins_code']) if meta and meta.get('ins_code') else None,
                               source_row=dict(row), tradable=eligible)
                if not eligible:
                    passive.append(holding)
                    continue
                idea_id = 'IDEA-' + uuid.uuid4().hex
                holding['symbol_id'] = known['id']
                positions[idea_id] = holding
                opening_ideas.append(dict(id=idea_id, portfolio_id=key, symbol_id=known['id'], symbol_fa=symbol,
                    ins_code=known['ins_code'], created_at=created, label='موجودی آغازین — ' + symbol,
                    thesis='انتقال تعداد موجود از گزارش فارابی؛ تحلیل خرید پیشین ثبت نشده و فرضیه خرید جدید نیست',
                    invalidation='برای فروش مجازی، دلیل تصمیم اکنون را ثبت کن؛ خرید بیشتر نیازمند تحلیل تازه است',
                    horizon=None, stop_rial=None, target_rial=None, origin='broker_opening',
                    historical_purchase_analysis_known=False, market=self._market(known['id'], aware(created)),
                    code_sha256=protocol['code']['sha256']))
            opening = dict(portfolio_id=key, created_at=created, source_captured_at=source['captured_at'],
                source=source, source_sha256=digest(source), request=request, request_id=request_id,
                positions=positions, passive=passive, available_cash_rial=available,
                locked_cash_rial=int(cash) - available, rounding_rial=rounding,
                rounding_is_spendable=False, purchases_generated=0, historical_profit_known=False)
            protocol.update(opening_kind='broker_snapshot', opening_sha256=digest(opening))
            self._insert_protocol(protocol)
            self.db.conn.execute('INSERT INTO paper_openings VALUES(?,?,?,?,?,?,?)',
                (key, import_id, request_id, created, source['captured_at'], canonical(opening), digest(opening)))
            self.db.conn.executemany('INSERT INTO paper_ideas VALUES(?,?,?,?,?,?)',
                [(p['id'], key, p['symbol_id'], created, canonical(p), digest(p)) for p in opening_ideas])
        return key

    def _opening(self, protocol):
        if protocol.get('opening_kind', 'cash') == 'cash':
            return None
        row = self.db.conn.execute('SELECT * FROM paper_openings WHERE portfolio_id=?', (protocol['id'],)).fetchone()
        if row is None:
            raise ValueError('موجودی آغازین پورتفو پیدا نشد')
        payload = json.loads(row['payload_json'])
        if (digest(payload) != row['payload_sha256'] or row['payload_sha256'] != protocol['opening_sha256'] or
            payload['portfolio_id'] != protocol['id'] or payload['created_at'] != protocol['created_at'] or
            row['created_at'] != protocol['created_at'] or row['request_id'] != payload['request_id'] or
            row['source_captured_at'] != payload['source_captured_at'] or
            row['broker_import_id'] != payload['source']['id'] or
            digest(self._source(row['broker_import_id'])) != payload['source_sha256'] or
            digest(payload['source']) != payload['source_sha256']):
            raise ValueError('منبع یا موجودی آغازین پورتفو مخدوش است')
        return payload

    def _load(self, key):
        row = self.db.conn.execute('SELECT * FROM paper_portfolios WHERE id=?', (key,)).fetchone()
        if row is None:
            raise ValueError('پورتفوی مجازی پیدا نشد')
        protocol = json.loads(row['protocol_json'])
        if digest(protocol) != row['protocol_sha256'] or protocol.get('version') not in SUPPORTED_VERSIONS:
            raise ValueError('پروتکل پورتفو مخدوش یا نسخه آن پشتیبانی‌نشده است')
        for field in ('id', 'name', 'created_at', 'capital_rial', 'max_loss_rial', 'mode'):
            if protocol[field] != row[field]:
                raise ValueError('فراداده پورتفو با پروتکل یکسان نیست')
        opening = self._opening(protocol)
        ideas = {}
        for item in self.db.conn.execute('SELECT * FROM paper_ideas WHERE portfolio_id=? ORDER BY created_at,id', (key,)):
            payload = json.loads(item['payload_json'])
            if digest(payload) != item['payload_sha256'] or any(payload[k] != item[k] for k in
                ('id', 'portfolio_id', 'symbol_id', 'created_at')) or aware(item['created_at']) < aware(row['created_at']):
                raise ValueError('تحلیل پورتفو مخدوش است')
            ideas[item['id']] = payload
        if opening and (not set(opening['positions']) <= set(ideas) or any(
            ideas[k].get('origin') != 'broker_opening' or ideas[k]['symbol_id'] != p['symbol_id'] or
            ideas[k]['symbol_fa'] != p['symbol_fa'] for k, p in opening['positions'].items())):
            raise ValueError('موجودی آغازین با شناسه‌های دفتر یکسان نیست')
        events, previous, last_time = [], row['protocol_sha256'], aware(row['created_at'])
        for event in self.db.conn.execute('SELECT * FROM paper_events WHERE portfolio_id=? ORDER BY id', (key,)):
            payload = json.loads(event['payload_json'])
            expected = event_digest(key, event['idea_id'], event['kind'], event['recorded_at'], payload, previous)
            if event['previous_sha256'] != previous or event['event_sha256'] != expected or aware(event['recorded_at']) < last_time:
                raise ValueError('زنجیره یا ترتیب زمانی دفتر مجازی مخدوش است')
            if event['kind'] == 'trade':
                idea = ideas.get(event['idea_id'])
                if (not idea or aware(idea['created_at']) > aware(event['recorded_at']) or
                    payload['idea_sha256'] != digest(idea) or payload['request_id'] != event['request_id']):
                    raise ValueError('اجرای مجازی به تحلیل پیشین معتبر متصل نیست')
            events.append(dict(event, payload=payload))
            previous, last_time = expected, aware(event['recorded_at'])
        return protocol, ideas, events, previous

    def _market(self, symbol_id, at):
        snapshot = self.db.conn.execute(
            "SELECT * FROM snapshots WHERE symbol_id=? AND source='tsetmc' AND captured_at<=? "
            'ORDER BY captured_at DESC,id DESC LIMIT 1', (symbol_id, at.isoformat())).fetchone()
        daily = self.db.conn.execute(
            "SELECT date,open_raw,high_raw,low_raw,close_raw,close_final_raw,ref_price_raw,volume,value,captured_at "
            "FROM daily_prices WHERE symbol_id=? AND source='tsetmc' AND date<=? ORDER BY date DESC LIMIT 1",
            (symbol_id, expected_completed_session(at, calendar=self.db.get_market_calendar()))).fetchone()
        return dict(snapshot=_clean(snapshot), daily=_clean(daily), observed_at=at.isoformat())

    def register_idea(self, portfolio_id, *, symbol, label, thesis, invalidation, horizon='swing',
                      stop_rial=None, target_rial=None):
        label = text(label, 'نام تحلیل', 200)
        thesis, invalidation = text(thesis, 'مبنای تحلیل'), text(invalidation, 'شرط رد تحلیل و خروج')
        if horizon not in HORIZONS:
            raise ValueError('افق تحلیل نامعتبر است')
        stop = None if stop_rial is None else integer(stop_rial)
        target = None if target_rial is None else integer(target_rial)
        if stop is not None and target is not None and stop >= target:
            raise ValueError('قیمت سناریوی زیان باید کمتر از هدف باشد')
        with self.db.conn:
            self.db.conn.execute('BEGIN IMMEDIATE')
            protocol, existing_ideas, events, _ = self._load(portfolio_id)
            at = self._clock(protocol, events)
            if any(at < aware(p['created_at']) for p in existing_ideas.values()):
                raise ValueError('ساعت سیستم از ثبت تحلیل قبلی عقب‌تر است')
            row = self.db.get_symbol(normalize_symbol(symbol))
            if not row or row['instrument_type_code'] not in (300, 303) or not row['is_active']:
                raise ValueError('نماد فعال سهام عادی بورس/فرابورس لازم است')
            key = 'IDEA-' + uuid.uuid4().hex
            idea = dict(id=key, portfolio_id=portfolio_id, symbol_id=row['id'], symbol_fa=row['symbol_fa'],
                        ins_code=row['ins_code'], created_at=at.isoformat(), label=label, thesis=thesis,
                        invalidation=invalidation, horizon=horizon, stop_rial=stop, target_rial=target,
                        market=self._market(row['id'], at), code_sha256=code_identity()['sha256'])
            self.db.conn.execute('INSERT INTO paper_ideas VALUES(?,?,?,?,?,?)',
                                 (key, portfolio_id, row['id'], at.isoformat(), canonical(idea), digest(idea)))
        return key

    @staticmethod
    def _clock(protocol, events):
        at = now_tehran()
        if at.tzinfo is None:
            raise ValueError('ساعت محلی منطقه زمانی ندارد')
        at = at.astimezone(TEHRAN_TZ)
        last = aware(events[-1]['recorded_at'] if events else protocol['created_at'])
        if at < last:
            raise ValueError('ساعت سیستم عقب رفته؛ ثبت با زمان گذشته مجاز نیست')
        return at

    @staticmethod
    def _trades(events):
        return [dict(e['payload'], idea_id=e['idea_id'], recorded_at=e['recorded_at'])
                for e in events if e['kind'] == 'trade']

    def _integrity(self, idea, trades, end_date, calendar, snapshot, opening_position=None, opening_started_at=None):
        """وقفه و شکست مرجع در دوره نگهداری؛ هیچ حق یا سود نقدی مصنوعی ساخته نمی‌شود."""
        own = [t for t in trades if t['idea_id'] == idea['id']]
        quantity = opening_position['quantity'] if opening_position else 0
        opened = aware(opening_started_at).date().isoformat() if opening_position else None
        intervals = []
        for trade in own:
            day = aware(trade['recorded_at']).astimezone(TEHRAN_TZ).date().isoformat()
            if not quantity and trade['side'] == 'buy':
                opened = day
            quantity += trade['quantity'] * (1 if trade['side'] == 'buy' else -1)
            if not quantity:
                intervals.append((opened, day))
        if quantity:
            intervals.append((opened, end_date))
        issues = []
        for start, end in intervals:
            if end <= start:
                continue
            previous = self.db.conn.execute(
                "SELECT date,close_final_raw,ref_price_raw FROM daily_prices WHERE symbol_id=? AND source='tsetmc' "
                'AND date<=? ORDER BY date DESC LIMIT 1', (idea['symbol_id'], start)).fetchone()
            rows = list(self.db.conn.execute(
                "SELECT date,close_final_raw,ref_price_raw FROM daily_prices WHERE symbol_id=? AND source='tsetmc' "
                'AND date>? AND date<=? ORDER BY date', (idea['symbol_id'], start, end)).fetchall())
            # برای جلسه هنوز تمام‌نشده، فقط مرجع مشاهده‌شده بررسی می‌شود؛ قیمت پایانی روز جعل نمی‌شود.
            if snapshot and snapshot.get('source') == 'tsetmc' and snapshot.get('price_unit') == 'Rial':
                try:
                    probe_day = aware(snapshot['provider_timestamp']).astimezone(TEHRAN_TZ).date().isoformat()
                except (TypeError, ValueError):
                    probe_day = None
                if probe_day == end and (not rows or rows[-1]['date'] < end):
                    rows.append(dict(date=end, ref_price_raw=snapshot.get('ref_price'), close_final_raw=None))
            by_date = {r['date']: r for r in rows}
            day = aware(start + 'T00:00:00+03:30').date() + timedelta(days=1)
            while day.isoformat() <= end:
                if is_trading_session(day, calendar) and day.isoformat() not in by_date:
                    issues.append('تاریخچه کامل دوره نگهداری یا تقویم توقف/تعطیلی تأیید نشده')
                    break
                day += timedelta(days=1)
            for row in rows:
                try:
                    ratio = finite_number(row['ref_price_raw']) / finite_number(previous['close_final_raw'] if previous else None)
                    if abs(ratio - 1) > 1e-6:
                        issues.append('شکست مرجع در دوره نگهداری؛ حقوق اقدام شرکتی حل نشده')
                except ValueError:
                    issues.append('مرجع یا پایانی دوره نگهداری نامعلوم است')
                previous = row
            known_action = self.db.conn.execute(
                'SELECT 1 FROM corporate_actions WHERE symbol_id=? AND event_date>? AND event_date<=? LIMIT 1',
                (idea['symbol_id'], start, end)).fetchone()
            if known_action:
                issues.append('اقدام شرکتی ثبت‌شده در دوره نگهداری نیازمند تطبیق است')
        return sorted(set(issues))

    def _report(self, protocol, ideas, events, at):
        trades, observations, marks, integrity = self._trades(events), {}, {}, {}
        calendar = self.db.get_market_calendar()
        opening = self._opening(protocol)
        opening_positions = opening['positions'] if opening else {}
        for key, idea in ideas.items():
            market = self._market(idea['symbol_id'], at)
            snapshot, daily = market['snapshot'], market['daily']
            price, mark_date, basis = None, None, None
            if snapshot and all(current_snapshot(snapshot.get(k), at) and aware(snapshot[k]) <= at
                                for k in ('captured_at', 'provider_timestamp')):
                try:
                    last, bid = finite_number(snapshot['last_price']), finite_number(snapshot['bid_price_1'])
                    lower, upper = finite_number(snapshot['price_limit_down']), finite_number(snapshot['price_limit_up'])
                    if snapshot.get('price_unit') == 'Rial' and lower <= min(last, bid) <= max(last, bid) <= upper:
                        price, mark_date, basis = min(last, bid), at.date().isoformat(), 'fresh_min_last_bid'
                except ValueError:
                    pass
            if price is None and daily:
                try:
                    price = finite_number(daily['close_raw'])
                    mark_date, basis = daily['date'], 'archived_last_trade'
                except ValueError:
                    pass
            own = [t for t in trades if t['idea_id'] == key]
            initial = opening_positions.get(key)
            open_quantity = (initial['quantity'] if initial else 0) + sum(t['quantity'] * (1 if t['side'] == 'buy' else -1) for t in own)
            problems = self._integrity(idea, trades, mark_date or expected_completed_session(at, calendar=calendar), calendar, snapshot,
                                       initial, opening['created_at'] if initial else None)
            if open_quantity:
                latest_trade_day = (expected_completed_session(aware(opening['created_at']), calendar=calendar) if initial
                                    else aware(own[-1]['recorded_at']).astimezone(TEHRAN_TZ).date().isoformat())
                if price is None or mark_date < latest_trade_day:
                    price = None
                    problems.append('قیمت پس از معامله هنوز موجود نیست')
                elif basis == 'archived_last_trade' and not current_daily_row(mark_date, at, calendar=calendar):
                    problems.append('قیمت ارزش‌گذاری روزانه کهنه است')
                if price is not None:
                    marks[key] = price
            integrity[key] = sorted(set(problems)) if own or initial else []
            observations[key] = dict(market, mark_rial=price, mark_date=mark_date, basis=basis,
                                     sale_proven=False, issues=integrity[key])
        passive_values, passive_observations = {}, {}
        if opening and opening['passive']:
            latest = BrokerRepository(self.db).latest('portfolio')
            by_symbol = {r['symbol_fa']: r for r in latest['rows']} if latest else {}
            for holding in opening['passive']:
                symbol = holding['symbol_fa']
                row = by_symbol.get(symbol)
                current = bool(latest and aware(latest['captured_at']) <= at and
                    expected_completed_session(aware(latest['captured_at']), calendar=calendar) == expected_completed_session(at, calendar=calendar))
                same_quantity = bool(row and row['quantity'] == holding['quantity'])
                value = row['value_rial'] if current and same_quantity and row['value_rial'] > 0 else None
                passive_values[symbol] = value
                passive_observations[symbol] = dict(source_import_id=latest['id'] if latest else None,
                    captured_at=latest['captured_at'] if latest else None, market_date=latest.get('market_date') if latest else None,
                    value_rial=value, current_report_observation=current, quantity_unchanged=same_quantity,
                    quote_session_verified=bool(latest and latest.get('market_date')), sale_proven=False,
                    basis='broker_reported_value_only', issue=None if value is not None else 'گزارش تازه با تعداد ثابت این دارایی موجود نیست')
        state = portfolio_state(protocol, ideas, trades, marks, integrity=integrity,
                                opening=opening, passive_values=passive_values)
        state.update(protocol=protocol, ideas=ideas, events=events, observations=observations, evaluated_at=at.isoformat(),
                     opening=opening, passive_observations=passive_observations, calendar_exceptions=calendar,
                     runtime_matches=self._runtime_matches(protocol))
        if not state['runtime_matches']:
            state['equity_rial'], state['total_net_rial'], state['return_pct'] = None, None, None
            state['loss_limit_reached'] = None
            # نقد/کارمزد ثبت‌شده معلوم می‌مانند؛ نتیجه وابسته به نسخه در تمام مسیرهای export نامعلوم است.
            for field in ('equity_rial', 'total_net_rial', 'loss_limit_reached', 'realized_net_rial'):
                state['ledger'][field] = None
            for position in state['ledger']['positions'].values():
                position['liquidation_value_rial'] = position['unrealized_net_rial'] = None
            for item in state['attribution']:
                item['realized_net_rial'] = item['unrealized_net_rial'] = None
        return state

    @staticmethod
    def _runtime_matches(protocol):
        return (protocol['code']['sha256'] == code_identity()['sha256'] and
                protocol['trading_hours'] == settings.TRADING_HOURS and protocol['trading_days'] == list(settings.TRADING_DAYS))

    def report(self, portfolio_id):
        with self.db.conn:
            self.db.conn.execute('BEGIN')
            protocol, ideas, events, _ = self._load(portfolio_id)
            at = self._clock(protocol, events)
            return self._report(protocol, ideas, events, at)

    def _append(self, portfolio_id, idea_id, kind, at, payload, previous, request_id):
        sha = event_digest(portfolio_id, idea_id, kind, at.isoformat(), payload, previous)
        self.db.conn.execute('INSERT INTO paper_events(request_id,portfolio_id,idea_id,kind,recorded_at,payload_json,'
                             'previous_sha256,event_sha256) VALUES(?,?,?,?,?,?,?,?)',
                             (request_id, portfolio_id, idea_id, kind, at.isoformat(), canonical(payload), previous, sha))

    def trade(self, portfolio_id, *, idea_id, side, quantity, rationale, price_rial=None, request_id=None,
              expected_snapshot_id=None):
        quantity, rationale = integer(quantity), text(rationale, 'دلیل معامله مجازی')
        if side not in ('buy', 'sell'):
            raise ValueError('سمت خرید یا فروش لازم است')
        requested_price = None if price_rial is None else integer(price_rial)
        request = dict(portfolio_id=portfolio_id, idea_id=idea_id, side=side, quantity=quantity,
                       rationale=rationale, requested_price_rial=requested_price, expected_snapshot_id=expected_snapshot_id)
        request_id = text(request_id or uuid.uuid4().hex, 'شناسه درخواست', 100)
        with self.db.conn:
            self.db.conn.execute('BEGIN IMMEDIATE')
            protocol, ideas, events, previous = self._load(portfolio_id)
            duplicate = self.db.conn.execute('SELECT * FROM paper_events WHERE request_id=?', (request_id,)).fetchone()
            if duplicate:
                payload = json.loads(duplicate['payload_json'])
                if duplicate['kind'] != 'trade' or payload['request'] != request:
                    raise ValueError('شناسه تکراری برای درخواست متفاوت استفاده شده است')
                return request_id
            if not self._runtime_matches(protocol):
                raise ValueError('کد محاسبه از زمان افتتاح تغییر کرده؛ پورتفوی جدید بساز')
            if idea_id not in ideas:
                raise ValueError('تحلیل به این پورتفو مربوط نیست')
            idea = ideas[idea_id]
            current_symbol = self.db.conn.execute(
                'SELECT instrument_type_code,is_active,ins_code FROM symbols WHERE id=?', (idea['symbol_id'],)).fetchone()
            if (not current_symbol or current_symbol['instrument_type_code'] not in (300, 303) or
                not current_symbol['is_active'] or str(current_symbol['ins_code']) != str(idea['ins_code'])):
                raise ValueError('معامله مجازی فقط برای سهام عادی فعال با شناسه ثابت مجاز است؛ اطلاعات نماد نیازمند بازبینی است')
            if side == 'buy' and idea.get('origin') == 'broker_opening':
                raise ValueError('موجودی آغازین تحلیل خرید تازه نیست؛ برای خرید بیشتر تحلیل جدید ثبت کن')
            at = self._clock(protocol, events)
            if at < aware(idea['created_at']):
                raise ValueError('معامله قبل از زمان ثبت تحلیل مجاز نیست')
            report = self._report(protocol, ideas, events, at)
            own = [t for t in self._trades(events) if t['idea_id'] == idea_id]
            holding = report['ledger']['positions'].get(idea_id)
            if own and not holding and side == 'buy':
                raise ValueError('این تحلیل بسته شده؛ برای ورود دوباره تحلیل تازه ثبت کن')
            if side == 'buy' and (report['equity_rial'] is None or report['loss_limit_reached']):
                raise ValueError('ورود جدید با ارزش نامعلوم سبد یا رسیدن به سقف زیان متوقف است')
            market = self._market(idea['symbol_id'], at)
            if protocol['mode'] == 'observed':
                if requested_price is not None:
                    raise ValueError('قیمت دلخواه در پورتفوی مظنه‌ای پذیرفته نیست')
                if report['integrity'].get(idea_id):
                    raise ValueError('موجودی این تحلیل به تطبیق اقدام شرکتی/تاریخچه نیاز دارد')
                same_day = [e for e in events if e['kind'] == 'trade' and ideas[e['idea_id']]['symbol_id'] == idea['symbol_id']
                            and aware(e['recorded_at']).astimezone(TEHRAN_TZ).date() == at.date()]
                used = [e['payload'] for e in same_day]
                snapshot = market['snapshot']
                if expected_snapshot_id is not None and (not snapshot or snapshot['id'] != expected_snapshot_id):
                    raise ValueError('مظنه از زمان نمایش تغییر کرده؛ صفحه را تازه کن و قیمت جدید را ببین')
                depth_used = sum(t['quantity'] for t in used if t['side'] == side and snapshot
                                 and (t['evidence']['snapshot']['id'] == snapshot['id'] or
                                      t['evidence']['snapshot']['provider_timestamp'] == snapshot['provider_timestamp']))
                evidence = quote_fill(snapshot, side=side, quantity=quantity, at=at, decision_at=idea['created_at'],
                                      protocol=protocol, consumed_volume=sum(t['quantity'] for t in used),
                                      consumed_value=sum(t['quantity'] * t['price_rial'] for t in used),
                                      consumed_depth=depth_used, calendar=self.db.get_market_calendar())
                evidence['calendar_exceptions'] = self.db.get_market_calendar()
                price = evidence['price_rial']
            else:
                if requested_price is None:
                    raise ValueError('قیمت سناریوی دستی را وارد کن')
                price = requested_price
                evidence = dict(model='user_price_scenario', market=market, execution_proven=False,
                                liquidity_checked=False, queues_checked=False)
            if side == 'buy' and ((idea['stop_rial'] is not None and price <= idea['stop_rial']) or
                                  (idea['target_rial'] is not None and price >= idea['target_rial'])):
                raise ValueError('قیمت خرید بین سناریوی زیان و هدف تحلیل نیست؛ تحلیل تازه لازم است')
            trade = dict(side=side, quantity=quantity, price_rial=price, fee_rial=quantity * price * protocol['fees'][side],
                         fee_source='config_estimate', request=request, rationale=rationale, evidence=evidence,
                         idea_sha256=digest(idea), mode=protocol['mode'], request_id=request_id)
            candidate = self._trades(events) + [dict(trade, idea_id=idea_id, recorded_at=at.isoformat())]
            marks = {key: item['mark_rial'] for key, item in report['observations'].items() if item['mark_rial'] is not None}
            checked = portfolio_state(protocol, ideas, candidate, marks, opening=report['opening'],
                                      passive_values=report['passive_values'])  # نقد مشترک و موجودی واقعی آغاز آزمایش.
            if side == 'buy' and checked['loss_limit_reached']:
                raise ValueError('این خرید با هزینه و قیمت مشاهده‌شده، سبد را به کف زیان می‌رساند')
            self._append(portfolio_id, idea_id, 'trade', at, trade, previous, request_id)
        return request_id

    def save_valuation(self, portfolio_id):
        with self.db.conn:
            self.db.conn.execute('BEGIN IMMEDIATE')
            protocol, ideas, events, previous = self._load(portfolio_id)
            at = self._clock(protocol, events)
            report = self._report(protocol, ideas, events, at)
            payload = {k: report[k] for k in ('evaluated_at', 'ledger', 'equity_rial', 'total_net_rial', 'return_pct',
                                             'observations', 'integrity', 'runtime_matches', 'calendar_exceptions',
                                             'passive_observations', 'passive_values', 'locked_cash_rial', 'opening_rounding_rial')}
            self._append(portfolio_id, None, 'valuation', at, payload, previous, uuid.uuid4().hex)
        return payload

    def export(self, portfolio_id):
        report = self.report(portfolio_id)
        return dict(protocol=report['protocol'], ideas=report['ideas'], events=report['events'],
                    opening=report['opening'],
                    exported_at=now_tehran().isoformat(), report=report,
                    accepted_for_signals=False, self_contained_replay=False)
