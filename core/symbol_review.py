"""توصیف شواهد یک نماد؛ بدون توصیه، تخمین نوع مجمع یا تغییر دادهٔ خام."""

import numpy as np
import pandas as pd

from core import market_rules
from core.data_quality import quant_anomaly_flags

SYMBOL_REVIEW_VERSION = 'symbol-review-v2'


def volume_comparability(prices: pd.DataFrame, prior_sessions: int) -> pd.Series:
    """n حجم قبلی و حجم امروز، n گذار مرجع معلوم و بدون شکاف لازم دارند.

    نبود شکاف مرجع، نبود قطعی تغییر تعداد سهم را اثبات نمی‌کند. وجود شکاف یا
    مرجع نامعلوم، مقایسه تعداد برگه‌ها را نامعلوم می‌کند؛ تبدیل حجم به ارزش یک فرضیه دیگر است.
    """
    if isinstance(prior_sessions, bool) or not isinstance(prior_sessions, int) or prior_sessions < 1:
        raise ValueError('تعداد جلسات قبلی باید عدد صحیح مثبت باشد')
    factors = market_rules.corporate_action_factors(prices)
    transition_ok = factors.eq(1).where(factors.notna()).astype(float)
    if 'symbol' in prices:
        enough = prices.groupby('symbol', sort=False).cumcount() >= prior_sessions
        return transition_ok.groupby(prices.symbol, sort=False).transform(
            lambda s: s.rolling(prior_sessions, min_periods=prior_sessions).sum()).eq(prior_sessions) & enough
    enough = pd.Series(np.arange(len(prices)) >= prior_sessions, index=prices.index)
    return transition_ok.rolling(prior_sessions, min_periods=prior_sessions).sum().eq(prior_sessions) & enough


def review_history(prices: pd.DataFrame, clients: pd.DataFrame) -> dict:
    """سابقه مرتب یک نماد؛ روزهای اضافهٔ حقیقی‌حقوقی، توقف فرض نمی‌شوند."""
    if prices.empty:
        return dict(version=SYMBOL_REVIEW_VERSION, rows=0, reference_gaps=[], price_anomalies=0,
                    volume_anomalies=0, extra_client_dates=sorted(set(clients.get('date', []))),
                    missing_client_dates=[], latest=None)
    p = prices.sort_values('date').reset_index(drop=True)
    if p.date.duplicated().any() or ('symbol' in p and p.symbol.nunique() != 1):
        raise ValueError('سابقهٔ یکتای یک نماد لازم است')
    factors = market_rules.corporate_action_factors(p)
    previous = market_rules.official_close(p).shift(1)
    gaps = p[['date', 'ref_price_raw']].copy()
    gaps['previous_official_close_rial'] = previous
    gaps['reference_ratio'] = factors
    gaps['calendar_gap_days'] = pd.to_datetime(p.date).diff().dt.days
    gaps = gaps.loc[factors.notna() & factors.ne(1)]
    returns = market_rules.pit_daily_returns(p)
    # همان آستانهٔ تشخیصی اعتبارسنج، بدون حذف یا پاک‌کردن پرش روز بازگشایی.
    flags = quant_anomaly_flags(p)
    latest = p.iloc[-1]
    def finite(v):
        return float(v) if pd.notna(v) and np.isfinite(v) else None
    return dict(version=SYMBOL_REVIEW_VERSION, rows=len(p), first=str(p.date.iloc[0]), last=str(latest.date),
        reference_gaps=gaps.replace({np.nan: None}).to_dict('records'),
        unknown_reference_transitions=int(factors.isna().sum()),
        price_anomalies=int(flags.price_anomaly.sum()), volume_anomalies=int(flags.volume_anomaly.sum()),
        anomaly_rows=int(flags.any(axis=1).sum()),
        extra_client_dates=sorted(set(clients.get('date', []))-set(p.date)),
        missing_client_dates=sorted(set(p.date)-set(clients.get('date', []))),
        volume_comparable_20d=bool(volume_comparability(p, 20).iloc[-1]),
        volume_comparable_30d=bool(volume_comparability(p, 30).iloc[-1]),
        latest=dict(date=str(latest.date), official_close_rial=finite(latest.close_final_raw),
                    last_rial=finite(latest.close_raw), daily_return=finite(returns.iloc[-1]),
                    volume=finite(latest.volume), value_rial=finite(latest.value)),
        event_type_inferred=False, signals_enabled=False)
