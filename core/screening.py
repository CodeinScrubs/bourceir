"""Descriptive equity screen and conservative freshness checks; no return forecasts."""

from datetime import datetime, timedelta

import pandas as pd

from core import market_rules
from core.price_quality import numeric_prices
from core.timeutils import TEHRAN_TZ, is_trading_session, now_tehran


def expected_completed_session(at=None, *, calendar=None) -> str:
    """Use explicit calendar exceptions; unknown holidays still cause conservative blocking."""
    at = at or now_tehran()
    if at.tzinfo is None:
        raise ValueError("زمان باید دارای منطقه زمانی باشد")
    at = at.astimezone(TEHRAN_TZ)
    day = at.date()
    if at.hour < 14:
        day -= timedelta(days=1)
    for _ in range(370):
        if is_trading_session(day, calendar):
            return day.isoformat()
        day -= timedelta(days=1)
    raise ValueError('جلسه کامل در تقویم ۳۷۰ روز اخیر یافت نشد')


def current_daily_row(date_value, at=None, *, calendar=None) -> bool:
    return isinstance(date_value, str) and date_value == expected_completed_session(at, calendar=calendar)


def current_snapshot(timestamp, at=None, max_delay_seconds=45) -> bool:
    try:
        parsed = datetime.fromisoformat(timestamp)
        if parsed.tzinfo is None:
            return False
        age = ((at or now_tehran()) - parsed).total_seconds()
        return -5 <= age <= max_delay_seconds
    except (ValueError, TypeError):
        return False


def descriptive_screen(frame: pd.DataFrame, min_value_rial: float, at=None, *, calendar=None) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    out = frame.loc[frame["instrument_type_code"].isin([300, 303]) & (frame["is_active"] == 1)].copy()
    out["current"] = out["date"].map(lambda date: current_daily_row(date, at, calendar=calendar))
    out["change_pct"] = market_rules.pit_daily_returns(out) * 100
    lock_known = market_rules.lock_inputs_known(out)
    locks = market_rules.lock_flags(out).astype('boolean').where(lock_known, axis=0)
    out = pd.concat([out, locks], axis=1)
    value = numeric_prices(out, ('value',))['value']
    valid = lock_known & value.gt(0)
    valid &= market_rules.official_close(out).notna()
    valid &= market_rules.daily_vwap(out).notna()
    out["data_valid"] = valid
    out["liquid"] = valid & (value >= min_value_rial)
    out['_screen_value_sort'] = value
    return out.sort_values(["current", "liquid", "_screen_value_sort", "symbol_fa"],
                           ascending=[False, False, False, True]).drop(columns='_screen_value_sort')
