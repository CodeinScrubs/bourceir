"""پرچم آماری برای بازبینی؛ وقوع آن به‌تنهایی به معنی داده خراب نیست."""

import pandas as pd

from core.market_rules import pit_daily_returns

PRICE_JUMP_THRESHOLD = 0.105
VOLUME_SPIKE_MULTIPLIER = 10.0


def quant_anomaly_flags(df: pd.DataFrame) -> pd.DataFrame:
    flags = pd.DataFrame(index=df.index)
    flags['price_anomaly'] = pit_daily_returns(df).abs() > PRICE_JUMP_THRESHOLD
    prior_median = df['volume'].shift(1).rolling(30, min_periods=10).median()
    flags['volume_anomaly'] = (df['volume'] > VOLUME_SPIKE_MULTIPLIER * prior_median).fillna(False)
    return flags
