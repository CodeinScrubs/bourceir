"""Shared-cash multi-equity daily replay. Research proxy, not proof of brokerage fills."""

import math
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from backtest.fractions import SyntheticFractionBook
from backtest.observed_valuation import ObservedValuation, ValuationGap
from backtest.prepared import PRICE_SNAPSHOT_VERSION, PreparedPortfolioPrices, prepare_portfolio_prices
from config import settings
from core.daily_publications import parse_publication
from core.instrument_history import receipt_time
from core.no_trade_valuation import parse_no_trade_publication
from core.risk import finite_number
from core.timeutils import TEHRAN_TZ, is_canonical_iso_date

PORTFOLIO_VERSION = "portfolio-v8"
RISK_EXIT_VERSION = "portfolio-risk-floor-exit-v2"
ENTRY_ALLOCATION_VERSION = "portfolio-entry-allocation-v1"
ENTRY_NO_TRADE_VERSION = "portfolio-entry-no-trade-v1"
NO_TRADE_VALUATION_VERSION = "portfolio-no-trade-valuation-v2"


@dataclass(frozen=True)
class PortfolioConfig:
    initial_capital_rial: float
    horizon: int = 2
    max_positions: int = 5
    participation: float = 0.05
    slippage_bps: float = 10
    max_loss_pct: float = 0.10
    stress: bool = False
    allow_entry_day_exit: bool = False

    def __post_init__(self):
        finite_number(self.initial_capital_rial)
        for value in (self.horizon, self.max_positions):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("افق و تعداد پوزیشن باید صحیح و مثبت باشند")
        if not 0 < finite_number(self.participation) <= 0.05 or not 0 < finite_number(self.max_loss_pct) < 1:
            raise ValueError("مشارکت حداکثر ۵٪ است؛ سقف زیان بین صفر و یک است")
        if finite_number(self.slippage_bps, positive=False) >= 10000:
            raise ValueError("لغزش نامعتبر است")
        if not isinstance(self.stress, bool) or not isinstance(self.allow_entry_day_exit, bool):
            raise ValueError('گزینه‌های اجرای سبد باید بولی باشند')


@dataclass
class PortfolioResult:
    equity: pd.DataFrame
    executions: pd.DataFrame
    decisions: pd.DataFrame
    metrics: dict
    assumptions: dict


class OffCalendarPriceError(ValueError):
    """An observed in-window price conflicts with the declared replay calendar."""


def _calendar(values):
    result = np.asarray(values).astype(str)
    if result.ndim != 1 or not len(result) or list(result) != sorted(set(result)):
        raise ValueError("تقویم باید یکتا، مرتب و غیرخالی باشد")
    if any(not is_canonical_iso_date(day) for day in result):
        raise ValueError("تاریخ تقویم معتبر نیست")
    return result


def _checked_window(prices, selections, calendar, start, end):
    """Reject conflicting observations in the requested window before book mutation.

    A price before the replay window may provide warmup for a reference chain.
    An explicitly bounded replay ignores decisions outside that window; without
    an explicit boundary, every supplied decision must belong to the calendar.
    Agreement with an inferred calendar does not prove its historical coverage.
    """
    if any(value is not None and not is_canonical_iso_date(value) for value in (start, end)):
        raise ValueError('تاریخ مرز اجرای سبد باید میلادی استاندارد باشد')
    if not isinstance(selections, pd.DataFrame) or selections.columns.duplicated().any() or not {'symbol', 'date'} <= set(selections):
        raise ValueError('ستون نماد و تاریخ برای انتخاب‌های سبد لازم است')
    if isinstance(prices, PreparedPortfolioPrices):
        if prices.version != PRICE_SNAPSHOT_VERSION:
            raise ValueError('Unsupported prepared-price contract')
        price_dates = list(prices.sessions)
        if sum(len(session) for session in prices.sessions.values()) != prices.row_count:
            raise ValueError('تعداد ردیف‌های تصویر قیمت آماده ناقص است')
    elif isinstance(prices, pd.DataFrame) and not prices.columns.duplicated().any() and {'symbol', 'date'} <= set(prices):
        price_dates = prices.date.unique().tolist()
    else:
        raise ValueError('ستون نماد و تاریخ برای قیمت‌های سبد لازم است')
    decision_dates = selections.date.unique().tolist()
    if any(not is_canonical_iso_date(day) for day in [*price_dates, *decision_dates]):
        raise ValueError('تاریخ ورودی سبد باید میلادی استاندارد و معلوم باشد')
    days = [day for day in calendar if (start is None or day >= start) and (end is None or day < end)]
    if not days:
        raise ValueError('بازه اجرای سبد خالی است')
    declared = set(calendar)
    price_start = start if start is not None else calendar[0]
    if any(day >= price_start and (day < end if end is not None else day <= calendar[-1])
           and day not in declared for day in price_dates):
        raise OffCalendarPriceError('تقویم اجرای سبد با تاریخ قیمت ناسازگار است؛ حذف ردیف مجاز نیست')
    if any((start is None or day >= start) and (end is None or day < end) and day not in declared
           for day in decision_dates):
        raise ValueError('تقویم اجرای سبد با تاریخ انتخاب ناسازگار است؛ حذف انتخاب مجاز نیست')
    return days


def _no_trade_proofs(observations, calendar, *, valuation=False):
    """Bind retrospective zero activity to an exact session and numeric instrument code."""
    if observations is None:
        return {}
    if not isinstance(observations, (tuple, list)):
        raise ValueError('فهرست رسیدهای بومی عدم معامله لازم است')
    sessions = set(calendar)
    cutoff = settings.RESEARCH_CONFIG['holdout_start']
    proofs = {}
    for observation in observations:
        if not isinstance(observation, dict):
            raise ValueError('رسید بومی عدم معامله نامعتبر است')
        code, day = observation.get('ins_code'), observation.get('requested_date')
        if (not isinstance(code, str) or not is_canonical_iso_date(day) or day >= cutoff or day not in sessions
                or type(observation.get('http_status')) is not int or observation['http_status'] != 200
                or (code, day) in proofs):
            raise ValueError('شناسه، تاریخ، پاسخ HTTP یا تکرار رسید عدم معامله نامعتبر است')
        proof = (parse_no_trade_publication(observation, code, day) if valuation else
                 parse_publication(observation, code, day, 'price'))
        received_day = receipt_time(observation['received_at']).astimezone(TEHRAN_TZ).date().isoformat()
        expected = 'observed_flat_mark' if valuation else 'observed_zero_activity'
        if proof['status'] != expected or received_day <= day:
            if valuation:
                raise ValueError('ارزش بدون معامله به شاهد روز کامل با قیمت پایانی و مرجع یکسان نیاز دارد')
            raise ValueError('عدم معامله باید از پاسخ روز کامل با حجم، تعداد و ارزش صفر تأیید شود')
        proofs[code, day] = proof if valuation else observation['body_sha256']
    return proofs


def replay_portfolio(prices: pd.DataFrame | PreparedPortfolioPrices, selections: pd.DataFrame, calendar,
                     config: PortfolioConfig, start: str | None = None, end: str | None = None,
                     dip_rule=None, *, cash_dividends=None, bonus_shares=None, subscription_rights=None,
                     loss_floor_exit=False, entry_allocation_fraction=None,
                     no_trade_entry_observations=None, no_trade_valuation_observations=None,
                     require_explicit_corporate_actions=False) -> PortfolioResult:
    """Decisions t -> next session; shared cash, slots, partial fills and delayed exits.

    Purchases precede sales in daily bookkeeping, so same-day sale proceeds cannot fund an earlier
    purchase without intraday ordering evidence. Position size uses decision-day equity and value.
    Stress buys at high and sells at low. No lookahead columns enter selection or sizing.
    Unknown action/mark for an open position invalidates returns and halts the replay.
    """
    calendar = _calendar(calendar)
    days = _checked_window(prices, selections, calendar, start, end)
    if type(require_explicit_corporate_actions) is not bool:
        raise ValueError('الزام دفتر صریح اقدام شرکتی باید بولی باشد')
    strict_valuation = no_trade_valuation_observations is not None
    if strict_valuation and days[-1] >= settings.RESEARCH_CONFIG['holdout_start']:
        raise ValueError('ارزش بدون معامله فقط برای پژوهش پیش از مهروموم تعریف شده است')
    if strict_valuation and subscription_rights is not None:
        raise ValueError('ارزش بدون معامله همراه دفتر حق‌تقدم به قرارداد مستقلی نیاز دارد')
    no_trade_entries = _no_trade_proofs(no_trade_entry_observations, calendar)
    no_trade_marks = _no_trade_proofs(no_trade_valuation_observations, calendar, valuation=True)
    for key, proof in no_trade_marks.items():
        no_trade_entries.setdefault(key, proof['body_sha256'])
    if type(loss_floor_exit) is not bool:
        raise ValueError('گزینه خروج پس از حد زیان باید بولی صریح باشد')
    if entry_allocation_fraction is not None:
        entry_allocation_fraction = finite_number(entry_allocation_fraction)
        if not 0 < entry_allocation_fraction <= 1:
            raise ValueError('سهم تخصیص خرید باید بزرگ‌تر از صفر و حداکثر یک باشد')
    combined_dividends = None
    if subscription_rights is not None:
        from backtest.subscription_rights import LIMIT, SubscriptionRightsBook

        if (not isinstance(subscription_rights, SubscriptionRightsBook) or bonus_shares is not None
                or cash_dividends is not None or config.initial_capital_rial >= LIMIT):
            raise ValueError('دفتر مستقل حق‌تقدم و سرمایه در دامنه دقیق لازم است؛ سود نقدی داخل همین دفتر تعریف می‌شود')
    if bonus_shares is not None:
        from backtest.bonus_shares import BonusShareBook
        from backtest.corporate_actions import BonusDividendBook

        if not isinstance(bonus_shares, BonusShareBook) or cash_dividends is not None:
            raise ValueError('دفتر مستقل سهام جایزه لازم است؛ ترکیب با سود نقدی هنوز تعریف نشده')
        if isinstance(bonus_shares, BonusDividendBook):
            combined_dividends = bonus_shares.dividends
    if cash_dividends is not None:
        from backtest.cash_dividends import CashDividendBook

        if not isinstance(cash_dividends, CashDividendBook):
            raise ValueError('دفتر صریح سود نقدی لازم است')
        cash_dividends.start(calendar)
    if dip_rule is not None:
        if dip_rule.hold != config.horizon or dip_rule.stop_loss is not None:
            raise ValueError('قاعده DIP باید هم‌افق سبد و بدون حد زیان باشد؛ این نسخه فقط خروج هدف/زمان را مدل می‌کند')
    if selections.duplicated(["symbol", "date"]).any():
        raise ValueError("نماد/تاریخ تکراری مجاز نیست")
    prepared = prices if isinstance(prices,PreparedPortfolioPrices) else prepare_portfolio_prices(prices)
    if prepared.version!=PRICE_SNAPSHOT_VERSION:
        raise ValueError('Unsupported prepared-price contract')
    if dip_rule is not None and not prepared.has_open:
        raise ValueError('قیمت گشایش برای مدل DIP لازم است')
    share_book = bonus_shares if bonus_shares is not None else subscription_rights
    valuation = ObservedValuation(no_trade_marks) if strict_valuation else None
    valuation_dividends = cash_dividends if cash_dividends is not None else combined_dividends
    by_day = prepared.sessions
    order = selections.sort_values(["date", "rank", "symbol"]) if "rank" in selections else selections.sort_values(["date", "symbol"])
    selected = {d: g["symbol"].tolist() for d, g in order.groupby("date")}
    if share_book is not None:
        share_book.start(days)
    positions, pending, executions, decisions, curve = {}, [], [], [], []
    cash, realized, fees = float(config.initial_capital_rial), 0.0, 0.0
    equity = cash
    buy_fee, sell_fee = settings.FEES["buy"], settings.FEES["sell"]
    fractions = SyntheticFractionBook(sell_fee=sell_fee)
    slip, stopped, invalid, action_count = config.slippage_bps / 10000, False, False, 0
    risk_exit_active, risk_exit_request_date = False, None
    day_position = {day: i for i, day in enumerate(calendar)}

    def held(symbol):
        return symbol in positions if share_book is None else any(p['symbol'] == symbol for p in positions.values())

    def position_count():
        return len(positions) if share_book is None else len({p['symbol'] for p in positions.values()})

    def exposure_symbols():
        return set(positions) if share_book is None else {p['symbol'] for p in positions.values()} | share_book.pending_whole_symbols

    def unvalued_share_rights():
        if subscription_rights is not None:
            return subscription_rights.has_unvalued_claims or any(not math.isfinite(p['mark']) for p in positions.values())
        return bonus_shares is not None and bonus_shares.has_unvalued_claims

    rights_pause = False

    def note(day, symbol, status, **kwargs):
        decisions.append(dict(date=day, symbol=symbol, status=status, **kwargs))

    def execute(day, symbol, side, quantity, price, cost=0):
        nonlocal fees
        fee = quantity * price * (buy_fee if side == "buy" else sell_fee)
        fees += fee
        executions.append(dict(date=day, symbol=symbol, side=side, quantity=quantity, price_rial=price,
                               fee_rial=fee, realized_net_rial=quantity * price - fee - cost if side == "sell" else None))
        return fee

    for day in days:
        session, used = by_day.get(day, {}), {}
        valuation_session, flat_marks = session, {}
        index = day_position[day]
        if valuation is not None:
            exposed = ({pos['symbol'] for pos in positions.values()} | share_book.pending_symbols
                       if share_book is not None else set(positions) | set(fractions.exposures))
            if valuation_dividends is not None:
                exposed.update(event.symbol for event in valuation_dividends.by_ex_date.get(day, ())
                               if valuation_dividends.eligible.get(event.event_id))
            try:
                valuation_session, flat_marks = valuation.session(
                    session, day, calendar[index-1] if index else None, exposed, valuation_dividends)
            except ValuationGap as error:
                note(day, error.symbol, error.status, **error.evidence)
                invalid = True
                curve.append(dict(date=day, equity_rial=np.nan, cash_rial=cash, positions=position_count(),
                                  stale_positions=0, stale_fractional_exposures=0))
                break
            for symbol, proof in flat_marks.items():
                note(day, symbol, 'observed_no_trade_mark', **proof)
        if cash_dividends is not None:
            try:
                cash_dividends.begin_day(day, valuation_session)
            except ValueError as error:
                note(day, '', 'unknown_corporate_entitlement', reason=str(error))
                invalid = True
                curve.append(dict(date=day, equity_rial=np.nan, cash_rial=cash, positions=len(positions),
                                  stale_positions=0, stale_fractional_exposures=0))
                break
        if share_book is not None:
            issue_symbol = ''
            try:
                if subscription_rights is None:
                    share_book.begin_day(day, valuation_session, positions)
                else:
                    cash_delta, offset_income = subscription_rights.begin_day(day, session, positions, cash)
                    cash += cash_delta
                    realized += offset_income
                for symbol in sorted({p['symbol'] for p in positions.values()} | share_book.pending_symbols):
                    issue_symbol = symbol
                    row = valuation_session.get(symbol)
                    if row is None:
                        continue
                    if not row['mark_valid'] or not math.isfinite(row['action']):
                        raise ValueError('قیمت یا نوع اقدام برای دارایی/حق باز نامعلوم است')
                    if not np.isclose(row['action'], 1.0) or row['ref_price_raw'] != row['previous_official']:
                        share_book.check_action(symbol, day, row)
                        action_count += 1
                    for pos in positions.values():
                        if pos['symbol'] == symbol and day >= pos.get('valuation_not_before', day):
                            pos['mark'], pos['mark_date'] = row['mark'], day
            except ValueError as error:
                note(day, issue_symbol, 'unknown_corporate_entitlement', reason=str(error))
                invalid = True
        # مسیر قدیمی فقط با نبود دفتر جایزه، تعداد مصنوعی و کسر نمایشی را می‌سازد.
        legacy_symbols = sorted(set(positions) | set(fractions.exposures)) if share_book is None else []
        for symbol in legacy_symbols:
            pos = positions.get(symbol)
            row = valuation_session.get(symbol)
            if row is None:
                continue
            factor = row["action"]
            known_mark = row['mark_valid']
            if not known_mark or not math.isfinite(factor) or factor <= 0:
                note(day, symbol, "unknown_open_position")
                invalid = True
                break
            if (require_explicit_corporate_actions and cash_dividends is None
                    and row['ref_price_raw'] != row['previous_official']):
                note(day, symbol, 'unaccounted_reference_change', previous_official_rial=row['previous_official'],
                     reference_rial=row['ref_price_raw'])
                invalid = True
                break
            # مسیر نقدی، حتی شکاف یک‌ریالی را بدون شاهد نادیده نمی‌گیرد؛ تحمل مدل قدیمی محفوظ است.
            changed = abs(factor - 1) > 1e-6 or (cash_dividends is not None and
                row['ref_price_raw'] != row['previous_official'])
            if changed:
                if cash_dividends is not None:
                    try:
                        cash_dividends.check_action(symbol, day, row)
                    except ValueError as error:
                        note(day, symbol, 'unknown_corporate_entitlement', reason=str(error))
                        invalid = True
                        break
                    # حق نقدی تعداد سهم/بهای سهم یا هدف اسمی ثبت‌شده را عوض نمی‌کند.
                    pos['mark'], pos['mark_date'] = row['mark'], day
                    action_count += 1
                    continue
                # کسر قبلی هم تعدیل می‌شود؛ حتی اگر از یک سهم بیشتر شود، قابل فروش فرض نمی‌شود.
                fractions.adjust(symbol, 1 / factor)
                if pos is not None:
                    exact = pos['quantity'] / factor
                    whole = math.floor(exact + 1e-9)
                    fraction = max(0.0, exact - whole)
                    fraction_cost = pos['cost'] * fraction / exact
                    fractions.add(symbol, fraction, fraction_cost)
                    pos['cost'] -= fraction_cost
                    pos['quantity'] = whole
                    if dip_rule is not None:
                        pos['target'] *= factor
                    if whole == 0:
                        del positions[symbol]
                        pos = None
                action_count += 1
            fractions.mark(symbol, row['mark'], day)
            if pos is not None:
                pos["mark"], pos["mark_date"] = row["mark"], day
        if invalid:
            curve.append(dict(date=day, equity_rial=np.nan, cash_rial=cash, positions=len(positions),
                              stale_positions=0, stale_fractional_exposures=0))
            break
        # Pending purchase budget was fixed after the preceding session, not today's high/low/close.
        for decision in pending:
            symbol = decision["symbol"]
            occupied=exposure_symbols()
            if stopped or rights_pause or held(symbol) or (symbol not in occupied and len(occupied) >= config.max_positions):
                note(day, symbol, 'risk_stop' if stopped else 'unvalued_rights_pause' if rights_pause else 'slots_full',
                     decision_date=decision['date'])
                continue
            row = session.get(symbol)
            evidence_sha = no_trade_entries.get((symbol, day))
            if evidence_sha is not None:
                if row is not None and (row['volume'] != 0 or row['value'] != 0):
                    note(day, symbol, 'conflicting_no_trade_entry', decision_date=decision['date'], evidence_sha256=evidence_sha)
                    invalid = True
                    break
                note(day, symbol, 'observed_no_trade_entry', decision_date=decision['date'], evidence_sha256=evidence_sha)
                continue
            if row is None:
                note(day, symbol, "no_session", decision_date=decision["date"])
                # An absent bar may be a halt or missing data. Neither the
                # ordering nor the hypothetical fill is established by absence.
                invalid = True
                break
            if not row["valid"]:
                note(day, symbol, "unknown_entry_execution", decision_date=decision["date"])
                invalid = True
                break
            if row["up"]:
                note(day, symbol, "locked_buy", decision_date=decision["date"])
                continue
            entry_kind = 'vwap'
            if dip_rule is not None:
                opening = row['open_raw']
                if not math.isfinite(opening) or not 0 < row['low_raw'] <= opening <= row['high_raw']:
                    note(day, symbol, 'unknown_entry_execution', decision_date=decision['date'])
                    invalid = True
                    break
                limit = row['ref_price_raw'] * (1 - dip_rule.dip)
                if opening <= limit:
                    price, entry_kind = opening, 'open'
                elif row['low_raw'] <= limit * (1 - 0.001 if dip_rule.conservative else 1):
                    price, entry_kind = limit, 'limit'
                else:
                    note(day, symbol, 'limit_not_filled', decision_date=decision['date'])
                    continue
                if config.stress:
                    # Remove favorable opening improvement, staying inside the daily range and buy limit.
                    price = min(limit, row['high_raw'])
                requested = math.floor(min(decision['volume'] * config.participation,
                    decision['value'] * config.participation / limit, decision['budget'] / (limit * (1 + buy_fee))))
            else:
                price = row["high_raw"] if config.stress else min(row["vwap"] * (1 + slip), row["high_raw"])
                requested = math.inf
            cap_volume = min(row["volume"], decision["volume"]) * config.participation
            cap_value = min(row["value"], decision["value"]) * config.participation
            quantity = math.floor(min(requested, cap_volume, cap_value / price, cash / (price * (1 + buy_fee)),
                                      decision["budget"] / (price * (1 + buy_fee))))
            if quantity < 1:
                note(day, symbol, "insufficient_capacity", decision_date=decision["date"])
                continue
            fee = execute(day, symbol, "buy", quantity, price)
            cost = quantity * price + fee
            cash -= cost
            positions[symbol] = dict(quantity=float(quantity), cost=cost, mark=row["mark"], mark_date=day,
                                     entry_date=day, due=index + config.horizon, entry_kind=entry_kind,
                                     target=price * (1 + dip_rule.take_profit) if dip_rule else None)
            if share_book is not None:
                key = f'ENTRY-{len(executions):08d}'
                positions[key] = positions.pop(symbol)
                positions[key]['symbol'] = symbol
                executions[-1]['lot_id'] = key
            used[symbol] = (float(quantity), quantity * price)
        pending = []
        if invalid:
            curve.append(dict(date=day, equity_rial=np.nan, cash_rial=cash, positions=len(positions),
                              stale_positions=0, stale_fractional_exposures=0))
            break
        for key, pos in list(positions.items()):
            symbol = pos.get('symbol', key)
            if risk_exit_active:
                note(day, symbol, 'risk_exit_attempt', trigger_date=risk_exit_request_date)
            if subscription_rights is not None and day < pos.get('valuation_not_before', day):
                note(day, symbol, 'credited_share_waiting_ex_price')
                continue
            lot_dip = None if pos.get('credit_only') or risk_exit_active else dip_rule
            if lot_dip is None and index < pos["due"] and not risk_exit_active:
                continue
            if symbol in flat_marks:
                if not config.stress or index >= pos['due'] or risk_exit_active:
                    note(day, symbol, 'observed_no_trade_exit_delayed', evidence_sha256=flat_marks[symbol]['evidence_sha256'])
                continue
            row = session.get(symbol)
            if row is None:
                note(day, symbol, "missing_exit_bar")
                # A missing bar cannot establish that a pending exit failed.
                invalid = True
                break
            if not row['valid']:
                # An unknown price/capacity on a required execution day cannot
                # be called a known failed order and filled profitably later.
                note(day,symbol,'unknown_exit_execution')
                invalid = True
                break
            if row['down']:
                note(day,symbol,'exit_delayed')
                continue
            exit_kind = 'loss_floor' if risk_exit_active else 'time'
            if lot_dip is not None:
                opening = row['open_raw']
                if not math.isfinite(opening) or not 0 < row['low_raw'] <= opening <= row['high_raw']:
                    note(day, symbol, 'unknown_open_position')
                    invalid = True
                    break
                same_day = day == pos['entry_date']
                can_target = not same_day or (config.allow_entry_day_exit and
                    (pos['entry_kind'] == 'open' or not lot_dip.conservative))
                if config.stress:
                    # Stress deliberately removes target fills; liquidation at the due session's low.
                    if index < pos['due']:
                        continue
                    price, exit_kind = row['low_raw'], 'time_stress'
                elif index <= pos['due'] and can_target and row['high_raw'] >= pos['target']:
                    price = max(opening, pos['target']) if not same_day else pos['target']
                    exit_kind = 'target'
                elif index >= pos['due']:
                    try:
                        last = finite_number(row['close_raw'])
                    except ValueError:
                        last = np.nan
                    if not math.isfinite(last) or not row['low_raw'] <= last <= row['high_raw']:
                        note(day, symbol, 'unknown_time_exit')
                        invalid = True
                        break
                    # We waited for the target through the session. Its earlier full-session
                    # VWAP is no longer an executable closing decision price.
                    price = max(last * (1 - slip), row['low_raw'])
                    exit_kind = 'time' if index == pos['due'] else 'time_late'
                else:
                    continue
            else:
                price = row["low_raw"] if config.stress else max(row["vwap"] * (1 - slip), row["low_raw"])
            used_volume, used_value = used.get(symbol, (0, 0))
            volume_left = max(0, row["volume"] * config.participation - used_volume)
            value_left = max(0, row["value"] * config.participation - used_value)
            quantity = min(pos["quantity"], math.floor(min(volume_left, value_left / price)))
            if quantity <= 0:
                note(day, symbol, "partial_exit_wait")
                continue
            cost = pos["cost"] * quantity / pos["quantity"]
            fee = execute(day, symbol, "sell", quantity, price, cost)
            executions[-1]['exit_kind'] = exit_kind
            if share_book is not None:
                executions[-1].update(lot_id=key, credit_disposal=bool(pos.get('credit_only')))
            cash += quantity * price - fee
            realized += quantity * price - fee - cost
            pos["quantity"] -= quantity
            pos["cost"] -= cost
            used[symbol] = (used_volume + quantity, used_value + quantity * price)
            if pos["quantity"] <= 1e-8:
                del positions[key]
            else:
                note(day, symbol, "partial_exit_wait")
        if invalid:
            curve.append(dict(date=day, equity_rial=np.nan, cash_rial=cash, positions=len(positions),
                              stale_positions=0, stale_fractional_exposures=0))
            break
        if cash_dividends is not None:
            credit = cash_dividends.end_day(day, positions)
            cash += credit
            realized += credit
        if share_book is not None:
            try:
                credits, credit_cash, credit_realized = share_book.end_day(day, positions)
                for pos in credits.values():
                    due = index+1
                    if combined_dividends is not None and bonus_shares.credit_exit_session is not None:
                        due = max(due, day_position[bonus_shares.credit_exit_session])
                    pos.update(entry_date=day, due=due,
                        entry_kind='rights_credit' if subscription_rights is not None else 'bonus_credit', target=None)
                positions.update(credits)
                cash += credit_cash
                realized += credit_realized
            except ValueError as error:
                note(day, '', 'unknown_corporate_entitlement', reason=str(error))
                invalid = True
                curve.append(dict(date=day, equity_rial=np.nan, cash_rial=cash, positions=position_count(),
                                  stale_positions=0, stale_fractional_exposures=0))
                break
        receivable = fractions.value()
        priced = [p for p in positions.values() if math.isfinite(p['mark'])] if subscription_rights is not None else positions.values()
        equity = cash + receivable + sum(p["quantity"] * p["mark"] * (1 - sell_fee) for p in priced)
        if share_book is not None and not math.isfinite(equity):
            note(day, '', 'unknown_credited_share_mark')
            invalid = True
            curve.append(dict(date=day, equity_rial=np.nan, cash_rial=cash, positions=position_count(),
                              stale_positions=0, stale_fractional_exposures=0))
            break
        # Research circuit breaker: no new buys after breaching the user's starting-capital loss floor.
        # It does not assume that held positions can be sold at the floor.
        floor_breached = equity <= config.initial_capital_rial * (1 - config.max_loss_pct)
        if unvalued_share_rights():
            # کران دارایی قیمت‌دار، NAV نیست؛ عبور آن فقط مکث موقت است، نه اثبات زیان واقعی.
            rights_pause = bool(floor_breached and not stopped)
        else:
            stopped |= floor_breached
            rights_pause = False
        # A newly observed EOD breach can only request disposal from the next loop/session.
        # An unvalued dividend/share claim is not a finite model NAV loss observation.
        nav_known = not unvalued_share_rights() and not (
            cash_dividends is not None and cash_dividends.outstanding_face_rial > 0)
        if loss_floor_exit and floor_breached and nav_known and not risk_exit_active:
            risk_exit_active, risk_exit_request_date = True, day
            note(day, '', 'risk_exit_trigger', model_equity_rial=equity,
                 floor_rial=config.initial_capital_rial*(1-config.max_loss_pct))
        stale = (sum(p['mark_date'] != day for p in positions.values()) if share_book is None else
                 len({p['symbol'] for p in positions.values() if p['mark_date'] != day}))
        curve.append(dict(date=day, equity_rial=equity, cash_rial=cash, positions=position_count(),
                          stale_positions=stale, risk_stopped=stopped,
                          stale_fractional_exposures=fractions.stale_count(day), noncash_receivable_rial=receivable))
        if cash_dividends is not None:
            # بودجه و توقف از دارایی قابل فروش مدل است؛ طلب مجهول در آنها خرج نمی‌شود.
            face = cash_dividends.outstanding_face_rial
            curve[-1].update(liquidation_assets_rial=equity, cash_dividend_receivable_face_rial=float(face))
            if face > 0:
                curve[-1]['equity_rial'] = np.nan
        if bonus_shares is not None:
            curve[-1].update(priced_assets_rial=equity, uncredited_bonus_units=dict(
                numerator=bonus_shares.pending_units.numerator, denominator=bonus_shares.pending_units.denominator),
                unvalued_rights_pause=rights_pause, issuer_exposures=len(exposure_symbols()))
            if combined_dividends is not None:
                curve[-1]['cash_dividend_receivable_face_rial'] = float(combined_dividends.outstanding_face_rial)
            if unvalued_share_rights():
                curve[-1]['equity_rial'] = np.nan
                curve[-1]['noncash_receivable_rial'] = np.nan
        if subscription_rights is not None:
            from backtest.bonus_shares import rational

            curve[-1].update(priced_assets_rial=equity, pending_subscription_units=rational(subscription_rights.pending_units),
                outstanding_dividend_face_rial=rational(subscription_rights.outstanding_dividend_face),
                unvalued_credited_positions=sum(not math.isfinite(p['mark']) for p in positions.values()),
                unvalued_rights_pause=rights_pause, issuer_exposures=len(exposure_symbols()))
            if unvalued_share_rights():
                curve[-1]['equity_rial'] = np.nan
                curve[-1]['noncash_receivable_rial'] = np.nan
        if loss_floor_exit:
            curve[-1]['risk_exit_requested'] = risk_exit_active
        if index + 1 < len(calendar) and (end is None or calendar[index + 1] < end) and not stopped and not rights_pause:
            reservable_cash = cash
            per_position_budget = equity / config.max_positions
            if entry_allocation_fraction is not None:
                # Only decision-day information. Future closes cannot resize a
                # pending order or retroactively free a reserve. Price movement
                # may later change the actual weight; this is an entry budget.
                if (not math.isfinite(curve[-1]['equity_rial']) or curve[-1]['stale_positions']
                        or curve[-1].get('stale_fractional_exposures', 0)):
                    if selected.get(day):
                        note(day, '', 'allocation_unknown_stale_marks')
                    continue
                reservable_cash = max(0., min(cash, cash-(1-entry_allocation_fraction)*equity))
                per_position_budget = equity*entry_allocation_fraction/config.max_positions
            occupied=exposure_symbols()
            slots = config.max_positions - len(occupied)
            for symbol in selected.get(day, []):
                if held(symbol) or (symbol not in occupied and slots <= 0):
                    note(day, symbol, 'slots_full')
                    continue
                row = session.get(symbol)
                if row is None or not row["valid"]:
                    note(day, symbol, "invalid_decision")
                    # A selected decision with unknown inputs is not a known
                    # no-trade/cash outcome and must not improve a backtest.
                    invalid = True
                    curve[-1]['equity_rial'] = np.nan
                    break
                budget = min(reservable_cash, per_position_budget)
                pending.append(dict(date=day, symbol=symbol, value=row["value"], volume=row["volume"], budget=budget))
                reservable_cash -= budget
                if symbol not in occupied:
                    slots -= 1
                    occupied.add(symbol)
        if invalid:
            break
    equity_frame = pd.DataFrame(curve)
    values = pd.concat([pd.Series([config.initial_capital_rial]), equity_frame["equity_rial"]], ignore_index=True)
    drawdown = values / values.cummax() - 1
    unvalued = (cash_dividends is not None or share_book is not None) and equity_frame.equity_rial.isna().any()
    outstanding = ((cash_dividends is not None and cash_dividends.outstanding_face_rial > 0)
                   or unvalued_share_rights())
    final = None if invalid or outstanding else equity
    metrics = dict(status="invalid_data" if invalid else "complete", portfolio_simulated=True,
                   total_return=None if final is None else equity / config.initial_capital_rial - 1,
                   max_drawdown=None if invalid or unvalued else float(-drawdown.min()), final_equity_rial=final,
                   initial_capital_rial=config.initial_capital_rial, realized_net_rial=realized, fees_rial=fees,
                   executions=len(executions), open_positions=position_count(), corporate_action_events=action_count,
                   noncash_receivable_rial=fractions.value(), noncash_receivable_cost_rial=fractions.cost,
                   fractional_exposures=fractions.records(),
                   # Absence of a detected reference-price discontinuity does
                   # not prove that real entitlements were absent or settled.
                   actual_corporate_entitlements_verified=False,
                   stopped_new_entries=stopped, stale_mark_days=int((equity_frame['stale_positions'].gt(0)
                       | equity_frame.get('stale_fractional_exposures', pd.Series(0, index=equity_frame.index)).gt(0)).sum()))
    assumptions = dict(version=PORTFOLIO_VERSION, engine_version=PORTFOLIO_VERSION,
        valuation_price_basis='observed official close and reference; independent of execution VWAP',
        missing_entry_bar_policy='unknown_entry_execution_and_no_total_return',
        missing_exit_bar_policy='unknown_exit_execution_and_no_total_return',
        config=asdict(config), fees=dict(settings.FEES),
        execution_model="bounded_daily_vwap_proxy" if not config.stress else "buy_high_sell_low_stress",
        corporate_action_model="synthetic_whole_shares_plus_noncash_fraction", same_day_sale_cash_reused=False,
        fractional_valuation='current_observed_mark_and_subsequent_reference_factors; noncash, no settlement assumed',
        mark_basis="last_observed_official_close_net_sell_fee", shorting=False, queue_position_proven=False,
        historical_same_day_resale_permissions_verified=False,
         limitations=["historical universe incomplete", "synthetic corporate actions are not actual entitlements",
                      ("official flat mark does not establish a liquidating price" if strict_valuation
                       else "halting days retain stale marks"),
                      "daily volume is not proof of own fills", "no intraday stop execution"])
    if no_trade_entry_observations is not None or strict_valuation:
        assumptions['entry_no_trade_version'] = ENTRY_NO_TRADE_VERSION
        assumptions['entry_no_trade_evidence_count'] = len(no_trade_entries)
        metrics['observed_no_trade_entries'] = sum(row['status'] == 'observed_no_trade_entry' for row in decisions)
    if strict_valuation:
        assumptions.update(no_trade_valuation_version=NO_TRADE_VALUATION_VERSION,
            no_trade_valuation_evidence_count=len(no_trade_marks),
            missing_exit_bar_policy='delay on a continuous dated official flat mark; otherwise unknown',
            missing_held_bar_policy='dated no-activity mark must be continuous or match explicit cash ex accounting',
            terminal_value_is_marked_nav=True)
        metrics.update(observed_no_trade_marks=sum(row['status'] == 'observed_no_trade_mark' for row in decisions),
            observed_no_trade_exit_delays=sum(row['status'] == 'observed_no_trade_exit_delayed' for row in decisions),
            observed_no_trade_cash_ex_marks=sum(row.get('declared_cash_ex', False) for row in decisions))
    if require_explicit_corporate_actions:
        assumptions['unaccounted_reference_change_policy'] = 'stop before using synthetic units or cash'
    if entry_allocation_fraction is not None:
        known = (equity_frame.equity_rial.notna() & equity_frame.equity_rial.gt(0)
            & equity_frame.stale_positions.eq(0)
            & equity_frame.get('stale_fractional_exposures', pd.Series(0, index=equity_frame.index)).eq(0))
        invested = ((equity_frame.equity_rial-equity_frame.cash_rial)/equity_frame.equity_rial).where(known)
        equity_frame['known_liquidation_proxy_invested_fraction'] = invested
        metrics.update(entry_allocation_fraction=entry_allocation_fraction,
            allocation_unknown_days=int((~known).sum()),
            maximum_known_invested_fraction=None if invested.dropna().empty else float(invested.max()),
            known_allocation_drift_days=int(invested.gt(entry_allocation_fraction+1e-12).sum()),
            actual_economic_exposure_verified=False)
        assumptions.update(entry_allocation_version=ENTRY_ALLOCATION_VERSION,
            entry_allocation_fraction=entry_allocation_fraction,
            allocation_basis='observed decision-day cash and net-sell-fee liquidation proxy; all pending budgets share the reserve',
            per_position_budget='decision-day equity * allocation_fraction / max_positions',
            new_decisions_paused_on_stale_marks=True, allocation_price_drift_rebalanced=False,
            allocation_is_guaranteed_ongoing_weight=False)
    if dip_rule is not None:
        assumptions.update(dip_rule=asdict(dip_rule),
            time_exit_price_basis='last_trade_raw_at_session_end_minus_slippage',
            execution_model='dip_limit_daily_proxy' if not config.stress else 'dip_no_open_improvement_no_target_sell_low_stress',
            requested_quantity_basis='prior budget/volume/value and current session reference limit, before OHLC fill outcome',
            intraday_order_sequence_proven=False)
    if cash_dividends is not None:
        from backtest.cash_dividends import PORTFOLIO_VERSION as CASH_PORTFOLIO_VERSION

        if unvalued and not invalid:
            metrics['status'] = 'unvalued_receivable_path'
        metrics.update(actual_corporate_entitlements_verified=False,
            cash_dividend_receivable_face_rial=float(cash_dividends.outstanding_face_rial),
            cash_dividend_income_rial=float(cash_dividends.total_paid_rial),
            stock_realized_net_rial=realized - float(cash_dividends.total_paid_rial),
            cash_dividend_claims=cash_dividends.records(), cash_dividend_ledger=cash_dividends.ledger,
            nav_unvalued_days=int(equity_frame.equity_rial.isna().sum()),
            liquidation_assets_final_rial=None if invalid else equity,
            accepted_for_signals=False, profitability_proven=False)
        assumptions.update(version=CASH_PORTFOLIO_VERSION,
            corporate_action_model='explicit_cash_dividend_fixed_shares_unvalued_receivables',
            source_events=[asdict(e) for e in cash_dividends.events],
            source_payments=[asdict(p) for p in cash_dividends.payments],
            dividend_settlement_verified=False, historical_entitlement_mapping_verified=False,
            claim_fair_value_verified=False, same_day_dividend_cash_reused=False,
            risk_and_budget_basis='cash_plus_stock_liquidation_proxy_excluding_unvalued_dividend_claims',
            dividend_target_policy='nominal_entry_price_target_unchanged',
            limitations=[*assumptions['limitations'], 'only explicit cash dividends; no synthetic fallback',
                         'unsettled claims have unknown fair value; announced payment dates are not cash receipts'])
    if bonus_shares is not None:
        from backtest.bonus_shares import PORTFOLIO_VERSION as BONUS_PORTFOLIO_VERSION
        from backtest.bonus_shares import rational

        if unvalued and not invalid:
            metrics['status'] = 'unvalued_bonus_path'
        metrics.update(actual_corporate_entitlements_verified=False, open_lots=len(positions),
            pending_bonus_units=rational(bonus_shares.pending_units), bonus_claims=bonus_shares.records(),
            bonus_pending_cost_basis_rial=rational(bonus_shares.pending_basis),
            noncash_receivable_rial=None if unvalued_share_rights() else 0.,
            noncash_receivable_cost_rial=float(bonus_shares.pending_basis),
            issuer_exposures=len(exposure_symbols()), new_entries_paused_for_unvalued_rights=rights_pause,
            unvalued_fractional_issuers=len(bonus_shares.pending_symbols-bonus_shares.pending_whole_symbols),
            bonus_ledger=bonus_shares.ledger, fraction_cash_rial=float(bonus_shares.total_fraction_cash),
            fraction_realized_rial=float(bonus_shares.total_fraction_realized),
            priced_assets_final_rial=None if invalid else equity,
            nav_unvalued_days=int(equity_frame.equity_rial.isna().sum()), accepted_for_signals=False, profitability_proven=False)
        assumptions.update(version=BONUS_PORTFOLIO_VERSION,
            corporate_action_model='explicit_bonus_fixed_original_shares_uncredited_and_fraction_claims',
            fractional_valuation='unknown until explicit net cash settlement; no market-price imputation',
            source_events=[asdict(e) for e in bonus_shares.events],
            source_releases=[asdict(r) for r in bonus_shares.releases],
            source_fraction_settlements=[asdict(s) for s in bonus_shares.settlements],
            historical_entitlement_mapping_verified=False, holder_credit_verified=False, fraction_fair_value_verified=False,
            risk_and_budget_basis='cash_plus_priced_tradable_lots_excluding_unvalued_bonus_claims; not economic NAV',
            bonus_target_policy='original nominal targets unchanged; credited lots disposed next observable session',
            bonus_cost_policy='pooled proportional basis across eligible lots; not statutory accounting proof',
            bonus_issuer_slot_policy='distinct issuers include uncredited whole shares; residual fractions remain receivables, same-issuer reentry keeps separate lot',
            bonus_risk_policy='EOD priced-assets floor pauses entries while rights unvalued; permanent stop only for known NAV breach',
            bonus_reference_policy='declared pure-bonus scenario; floor/ceil one-rial envelope, rounding rule unverified',
            bonus_release_timing='end_of_first_observed_session_on_or_after_explicit_date; no same-day disposal',
            limitations=[*(s for s in assumptions['limitations'] if s != 'synthetic corporate actions are not actual entitlements'),
                'declared share ratios are not inferred from reference prices',
                'issuer registration/deposit is not holder credit; releases are explicit model inputs',
                'pending and fractional shares have unknown fair value; no price-imputed cash',
                'mixed cash/bonus or overlapping uncredited actions require a separate verified contract'])
        from backtest.corporate_actions import PORTFOLIO_VERSION as COMBINED_PORTFOLIO_VERSION

        if combined_dividends is not None:
            dividends = combined_dividends
            cash_income = float(dividends.total_paid_rial)
            metrics.update(cash_dividend_income_rial=cash_income,
                cash_dividend_receivable_face_rial=float(dividends.outstanding_face_rial),
                stock_realized_net_rial=realized-cash_income-float(bonus_shares.total_fraction_realized),
                cash_dividend_claims=dividends.records(), cash_dividend_ledger=dividends.ledger)
            if unvalued and not invalid:
                metrics['status'] = 'unvalued_corporate_action_path'
            assumptions.update(version=COMBINED_PORTFOLIO_VERSION,
                corporate_action_model='explicit_sequential_bonus_shares_and_cash_dividend_claims',
                source_dividends=[asdict(e) for e in dividends.events],
                source_dividend_payments=[asdict(p) for p in dividends.payments],
                source_previous_price_sessions=dict(getattr(dividends, 'previous_price_sessions', {})),
                dividend_record_snapshot_sessions=dict(getattr(dividends, 'record_snapshot_sessions', {})),
                credited_share_exit_session=bonus_shares.credit_exit_session,
                bonus_target_policy='original nominal targets unchanged; credited lots wait for next session and declared exit session',
                bonus_release_timing='end_of_first_observed_session_on_or_after_explicit_date; disposal no earlier than next session and declared exit session',
                dividend_settlement_verified=False, same_day_dividend_cash_reused=False,
                risk_and_budget_basis='cash_plus_priced_tradable_lots_excluding_unvalued_bonus_and_dividend_claims',
                limitations=[s for s in assumptions['limitations'] if
                    s != 'mixed cash/bonus or overlapping uncredited actions require a separate verified contract']+[
                    'simultaneous cash/bonus actions are rejected',
                    'dividend entitlement on pending bonus shares is unknown and rejected',
                    'meeting inventory snapshots use the supplied observed calendar, not a verified market calendar',
                    'announced dividends and issuer registration do not prove holder payment or saleable shares'])
    if subscription_rights is not None:
        from backtest.bonus_shares import rational
        from backtest.subscription_rights import PORTFOLIO_VERSION as RIGHTS_PORTFOLIO_VERSION

        book = subscription_rights
        dividend_cash = float(book.total_dividend_cash)
        dividend_income = dividend_cash+float(book.total_offset_income)
        disposal_cash = float(book.total_disposal_cash)
        if unvalued and not invalid:
            metrics['status'] = 'unvalued_subscription_path'
        metrics.update(actual_corporate_entitlements_verified=False, open_lots=len(positions),
            pending_subscription_units=rational(book.pending_units), subscription_claims=book.records(),
            subscription_ledger=book.ledger, subscription_pending_basis_rial=rational(book.pending_basis),
            subscription_cash_paid_rial=float(book.total_cash_paid), exercise_fees_rial=float(book.total_fees),
            trade_fees_rial=fees, fees_rial=fees+float(book.total_fees),
            dividend_cash_received_rial=dividend_cash, dividend_offset_income_rial=float(book.total_offset_income),
            dividend_income_rial=dividend_income, outstanding_dividend_face_rial=rational(book.outstanding_dividend_face),
            rights_disposal_cash_received_rial=disposal_cash,
            stock_realized_net_rial=realized-dividend_income-disposal_cash,
            noncash_receivable_rial=None if unvalued_share_rights() else 0.,
            noncash_receivable_cost_rial=float(book.pending_basis), issuer_exposures=len(exposure_symbols()),
            new_entries_paused_for_unvalued_rights=rights_pause, priced_assets_final_rial=None if invalid else equity,
            nav_unvalued_days=int(equity_frame.equity_rial.isna().sum()),
            accepted_for_signals=False, profitability_proven=False)
        assumptions.update(version=RIGHTS_PORTFOLIO_VERSION,
            corporate_action_model='explicit_funded_subscription_fixed_original_shares_and_unvalued_claims',
            source_events=[asdict(e) for e in book.events], source_exercises=[asdict(e) for e in book.exercises],
            source_credits=[asdict(c) for c in book.credits], source_disposals=[asdict(d) for d in book.disposals],
            source_rights_payments=[asdict(p) for p in book.payments],
            source_holder_dividend_receipts=[asdict(r) for r in book.dividend_receipts],
            source_dividends=[] if book.dividends is None else [asdict(e) for e in book.dividends.events],
            source_dividend_payments=[] if book.dividends is None else [asdict(p) for p in book.dividends.payments],
            holder_entitlement_verified=False, holder_credit_verified=False, historical_fee_schedule_verified=False,
            subscription_policy='caller supplied dated elections; no independent prospective choice evidence inferred',
            subscription_cash_timing='beginning of first observed session on/after stated payment; no same-day incoming cash used',
            offset_policy='prior-session outstanding dividend of same issuer; cash plus offsets cannot exceed face',
            credit_timing='end of first observed session on/after explicit credit; disposal from next session and not before ex price',
            rights_disposal_policy='explicit holder/issuer disposal creates unvalued cash claim; only observed net receipts are spendable',
            cost_basis_policy='original share basis retained; rights zero allocated model basis; funded shares cost principal plus exercise fees',
            risk_and_budget_basis='cash plus priced tradable lots excluding all unvalued claims; not economic NAV',
            reference_policy='declared cash-rights formula within one-rial floor/ceil; actual rounding and entitlement unverified',
            limitations=[*(s for s in assumptions['limitations'] if s != 'synthetic corporate actions are not actual entitlements'),
                'cash/receivable funding and credits are explicit model inputs, not inferred actual holder records',
                'no rights market execution or queue position inferred from a disposal receipt',
                'simultaneous dividend/right ex-dates, bonus mixtures and overlapping uncredited actions require separate contracts',
                'unknown pre-ex credited-share marks and unsettled claims keep economic NAV unknown'])
    if loss_floor_exit:
        risk_sells = [row for row in executions if row.get('exit_kind') == 'loss_floor']
        unvalued_claim = unvalued_share_rights() or (
            cash_dividends is not None and cash_dividends.outstanding_face_rial > 0)
        fully_flat = position_count() == 0 and not fractions.exposures and not unvalued_claim
        metrics.update(risk_exit_request_date=risk_exit_request_date,
            risk_exit_sell_executions=len(risk_sells),
            risk_exit_first_sale_date=risk_sells[0]['date'] if risk_sells else None,
            risk_exit_remaining_positions=position_count(),
            risk_exit_fully_liquidated=None if risk_exit_request_date is None else fully_flat)
        assumptions.update(base_accounting_version=assumptions['version'], version=RISK_EXIT_VERSION,
            risk_exit_policy='finite marked model NAV EOD breach; pause entries and request all whole-share exits next session',
            risk_exit_unknown_nav_triggered=False, risk_exit_threshold_is_execution_price=False,
            risk_exit_stale_marks='retained under existing model and reported; not verified current liquidation value',
            risk_exit_queue_capacity_enforced=True, risk_exit_loss_cap_guaranteed=False)
    return PortfolioResult(equity_frame, pd.DataFrame(executions), pd.DataFrame(decisions), metrics, assumptions)
