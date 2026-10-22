"""Shared-cash multi-equity daily replay. Research proxy, not proof of brokerage fills."""

import math
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from backtest.fractions import SyntheticFractionBook
from config import settings
from core import market_rules
from core.risk import finite_number

PORTFOLIO_VERSION = "portfolio-v4"


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


def _calendar(values):
    result = np.asarray(values).astype(str)
    if result.ndim != 1 or not len(result) or list(result) != sorted(set(result)):
        raise ValueError("تقویم باید یکتا، مرتب و غیرخالی باشد")
    if pd.to_datetime(result, format="%Y-%m-%d", errors="coerce").isna().any():
        raise ValueError("تاریخ تقویم معتبر نیست")
    return result


def replay_portfolio(prices: pd.DataFrame, selections: pd.DataFrame, calendar,
                     config: PortfolioConfig, start: str | None = None, end: str | None = None,
                     dip_rule=None, *, cash_dividends=None, bonus_shares=None, subscription_rights=None) -> PortfolioResult:
    """Decisions t -> next session; shared cash, slots, partial fills and delayed exits.

    Purchases precede sales in daily bookkeeping, so same-day sale proceeds cannot fund an earlier
    purchase without intraday ordering evidence. Position size uses decision-day equity and value.
    Stress buys at high and sells at low. No lookahead columns enter selection or sizing.
    Unknown action/mark for an open position invalidates returns and halts the replay.
    """
    calendar = _calendar(calendar)
    if subscription_rights is not None:
        from backtest.subscription_rights import LIMIT, SubscriptionRightsBook

        if (not isinstance(subscription_rights, SubscriptionRightsBook) or bonus_shares is not None
                or cash_dividends is not None or config.initial_capital_rial >= LIMIT):
            raise ValueError('دفتر مستقل حق‌تقدم و سرمایه در دامنه دقیق لازم است؛ سود نقدی داخل همین دفتر تعریف می‌شود')
    if bonus_shares is not None:
        from backtest.bonus_shares import BonusShareBook

        if not isinstance(bonus_shares, BonusShareBook) or cash_dividends is not None:
            raise ValueError('دفتر مستقل سهام جایزه لازم است؛ ترکیب با سود نقدی هنوز تعریف نشده')
    if cash_dividends is not None:
        from backtest.cash_dividends import CashDividendBook

        if not isinstance(cash_dividends, CashDividendBook):
            raise ValueError('دفتر صریح سود نقدی لازم است')
        cash_dividends.start(calendar)
    if dip_rule is not None:
        if dip_rule.hold != config.horizon or dip_rule.stop_loss is not None:
            raise ValueError('قاعده DIP باید هم‌افق سبد و بدون حد زیان باشد؛ این نسخه فقط خروج هدف/زمان را مدل می‌کند')
    if prices.duplicated(["symbol", "date"]).any() or selections.duplicated(["symbol", "date"]).any():
        raise ValueError("نماد/تاریخ تکراری مجاز نیست")
    required = {"symbol", "date", "low_raw", "high_raw", "close_raw", "close_final_raw", "ref_price_raw", "volume", "value"}
    if not required <= set(prices):
        raise ValueError("ستون‌های خام لازم قیمت موجود نیستند")
    if dip_rule is not None and 'open_raw' not in prices:
        raise ValueError('قیمت گشایش برای مدل DIP لازم است')
    prices = prices.sort_values(["symbol", "date"]).copy()
    prices["vwap"] = market_rules.daily_vwap(prices)
    prices["mark"] = market_rules.official_close(prices)
    share_book = bonus_shares if bonus_shares is not None else subscription_rights
    if cash_dividends is not None or share_book is not None:
        prices['previous_official'] = prices.groupby('symbol')['mark'].shift(1)
        prices['previous_date'] = prices.groupby('symbol')['date'].shift(1)
    prices["action"] = market_rules.corporate_action_factors(prices)
    locks = market_rules.lock_flags(prices)
    prices["up"], prices["down"] = locks["locked_up"], locks["locked_down"]
    prices["valid"] = np.isfinite(prices[["vwap", "ref_price_raw", "mark"]]).all(axis=1)
    prices["valid"] &= (prices["ref_price_raw"] > 0) & (prices["mark"] > 0)
    by_day = {d: g.set_index("symbol").to_dict("index") for d, g in prices.groupby("date", sort=False)}
    order = selections.sort_values(["date", "rank", "symbol"]) if "rank" in selections else selections.sort_values(["date", "symbol"])
    selected = {d: g["symbol"].tolist() for d, g in order.groupby("date")}
    days = [d for d in calendar if (start is None or d >= start) and (end is None or d < end)]
    if not days:
        raise ValueError("بازه اجرای سبد خالی است")
    if share_book is not None:
        share_book.start(days)
    positions, pending, executions, decisions, curve = {}, [], [], [], []
    cash, realized, fees = float(config.initial_capital_rial), 0.0, 0.0
    equity = cash
    buy_fee, sell_fee = settings.FEES["buy"], settings.FEES["sell"]
    fractions = SyntheticFractionBook(sell_fee=sell_fee)
    slip, stopped, invalid, action_count = config.slippage_bps / 10000, False, False, 0
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
        return bonus_shares is not None and bonus_shares.pending_units > 0

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
        index = day_position[day]
        if cash_dividends is not None:
            try:
                cash_dividends.begin_day(day, session)
            except ValueError as error:
                note(day, '', 'unknown_corporate_entitlement', reason=str(error))
                invalid = True
                curve.append(dict(date=day, equity_rial=np.nan, cash_rial=cash, positions=len(positions),
                                  stale_positions=0, stale_fractional_exposures=0))
                break
        if share_book is not None:
            try:
                if subscription_rights is None:
                    share_book.begin_day(day, session, positions)
                else:
                    cash_delta, offset_income = subscription_rights.begin_day(day, session, positions, cash)
                    cash += cash_delta
                    realized += offset_income
                for symbol in sorted({p['symbol'] for p in positions.values()} | share_book.pending_symbols):
                    row = session.get(symbol)
                    if row is None:
                        continue
                    if not row['valid'] or not math.isfinite(row['action']):
                        raise ValueError('قیمت یا نوع اقدام برای دارایی/حق باز نامعلوم است')
                    if not np.isclose(row['action'], 1.0) or row['ref_price_raw'] != row['previous_official']:
                        share_book.check_action(symbol, day, row)
                        action_count += 1
                    for pos in positions.values():
                        if pos['symbol'] == symbol and day >= pos.get('valuation_not_before', day):
                            pos['mark'], pos['mark_date'] = row['mark'], day
            except ValueError as error:
                note(day, '', 'unknown_corporate_entitlement', reason=str(error))
                invalid = True
        # مسیر قدیمی فقط با نبود دفتر جایزه، تعداد مصنوعی و کسر نمایشی را می‌سازد.
        legacy_symbols = sorted(set(positions) | set(fractions.exposures)) if share_book is None else []
        for symbol in legacy_symbols:
            pos = positions.get(symbol)
            row = session.get(symbol)
            if row is None:
                continue
            factor = row["action"]
            known_mark = all(math.isfinite(row[key]) and row[key] > 0 for key in ('mark', 'ref_price_raw'))
            if not known_mark or (pos is not None and not row['valid']) or not math.isfinite(factor) or factor <= 0:
                note(day, symbol, "unknown_open_position")
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
            if row is None:
                note(day, symbol, "no_session", decision_date=decision["date"])
                continue
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
            if subscription_rights is not None and day < pos.get('valuation_not_before', day):
                note(day, symbol, 'credited_share_waiting_ex_price')
                continue
            lot_dip = None if pos.get('credit_only') else dip_rule
            if lot_dip is None and index < pos["due"]:
                continue
            row = session.get(symbol)
            if row is None or not row["valid"] or row["down"]:
                note(day, symbol, "exit_delayed")
                continue
            exit_kind = 'time'
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
                    pos.update(entry_date=day, due=index+1,
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
            if bonus_shares.pending_units > 0:
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
        if index + 1 < len(calendar) and (end is None or calendar[index + 1] < end) and not stopped and not rights_pause:
            reservable_cash = cash
            occupied=exposure_symbols()
            slots = config.max_positions - len(occupied)
            for symbol in selected.get(day, []):
                if held(symbol) or (symbol not in occupied and slots <= 0):
                    note(day, symbol, 'slots_full')
                    continue
                row = session.get(symbol)
                if row is None or not row["valid"]:
                    note(day, symbol, "invalid_decision")
                    continue
                budget = min(reservable_cash, equity / config.max_positions)
                pending.append(dict(date=day, symbol=symbol, value=row["value"], volume=row["volume"], budget=budget))
                reservable_cash -= budget
                if symbol not in occupied:
                    slots -= 1
                    occupied.add(symbol)
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
                   actual_corporate_entitlements_verified=action_count == 0,
                   stopped_new_entries=stopped, stale_mark_days=int((equity_frame['stale_positions'].gt(0)
                       | equity_frame.get('stale_fractional_exposures', pd.Series(0, index=equity_frame.index)).gt(0)).sum()))
    assumptions = dict(version=PORTFOLIO_VERSION, config=asdict(config), fees=dict(settings.FEES),
        execution_model="bounded_daily_vwap_proxy" if not config.stress else "buy_high_sell_low_stress",
        corporate_action_model="synthetic_whole_shares_plus_noncash_fraction", same_day_sale_cash_reused=False,
        fractional_valuation='current_observed_mark_and_subsequent_reference_factors; noncash, no settlement assumed',
        mark_basis="last_observed_official_close_net_sell_fee", shorting=False, queue_position_proven=False,
        historical_same_day_resale_permissions_verified=False,
        limitations=["historical universe incomplete", "synthetic corporate actions are not actual entitlements",
                     "halting days retain stale marks", "daily volume is not proof of own fills", "no intraday stop execution"])
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
            noncash_receivable_rial=None if bonus_shares.pending_units > 0 else 0.,
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
    return PortfolioResult(equity_frame, pd.DataFrame(executions), pd.DataFrame(decisions), metrics, assumptions)
