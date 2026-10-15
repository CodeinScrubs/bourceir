"""Descriptive equity screen and conservative freshness checks; no return forecasts."""

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from core import market_rules
from core.timeutils import TEHRAN_TZ, is_trading_weekday, now_tehran


def expected_completed_session(at=None) -> str:
    """Weekday fallback only; missing official holiday calendar causes conservative blocking."""
    at = at or now_tehran()
    if at.tzinfo is None:
        raise ValueError("زمان باید دارای منطقه زمانی باشد")
    at = at.astimezone(TEHRAN_TZ)
    day = at.date()
    if at.hour < 14:
        day -= timedelta(days=1)
    while not is_trading_weekday(day):
        day -= timedelta(days=1)
    return day.isoformat()


def current_daily_row(date_value, at=None) -> bool:
    return isinstance(date_value, str) and date_value == expected_completed_session(at)


def current_snapshot(timestamp, at=None, max_delay_seconds=45) -> bool:
    try:
        parsed = datetime.fromisoformat(timestamp)
        if parsed.tzinfo is None:
            return False
        age = ((at or now_tehran()) - parsed).total_seconds()
        return -5 <= age <= max_delay_seconds
    except (ValueError, TypeError):
        return False


def descriptive_screen(frame: pd.DataFrame, min_value_rial: float, at=None) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    out = frame.loc[frame["instrument_type_code"].isin([300, 303]) & (frame["is_active"] == 1)].copy()
    out["current"] = out["date"].map(lambda date: current_daily_row(date, at))
    out["change_pct"] = market_rules.pit_daily_returns(out) * 100
    out = pd.concat([out, market_rules.lock_flags(out)], axis=1)
    valid = np.isfinite(out[["high_raw", "low_raw", "ref_price_raw", "value", "volume"]]).all(axis=1)
    valid &= (out["low_raw"] > 0) & (out["high_raw"] >= out["low_raw"]) & (out["ref_price_raw"] > 0) & (out["volume"] > 0)
    valid &= np.isfinite(market_rules.official_close(out)) & (market_rules.official_close(out) > 0)
    valid &= market_rules.daily_vwap(out).notna()
    out["data_valid"] = valid
    out["liquid"] = valid & (out["value"] >= min_value_rial)
    return out.sort_values(["current", "liquid", "value", "symbol_fa"], ascending=[False, False, False, True])
