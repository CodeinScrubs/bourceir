"""Descriptive equity screen and conservative freshness checks; no return forecasts."""

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from core import market_rules
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
    out = pd.concat([out, market_rules.lock_flags(out)], axis=1)
    valid = np.isfinite(out[["high_raw", "low_raw", "ref_price_raw", "value", "volume"]]).all(axis=1)
    valid &= (out["low_raw"] > 0) & (out["high_raw"] >= out["low_raw"]) & (out["ref_price_raw"] > 0) & (out["volume"] > 0)
    valid &= np.isfinite(market_rules.official_close(out)) & (market_rules.official_close(out) > 0)
    valid &= market_rules.daily_vwap(out).notna()
    out["data_valid"] = valid
    out["liquid"] = valid & (out["value"] >= min_value_rial)
    return out.sort_values(["current", "liquid", "value", "symbol_fa"], ascending=[False, False, False, True])
