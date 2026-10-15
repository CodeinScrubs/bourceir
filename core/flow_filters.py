"""Causal, descriptive flow diagnostics. A filter match is never a trading signal.

This is a separately versioned research sidecar; it does not modify daily-v5.
Windows count recorded symbol sessions, include observed zero-volume sessions,
and never bridge a missing client-type observation by dropping that session.
"""

import numpy as np
import pandas as pd

from core import market_rules
from core.symbol_review import volume_comparability
from core.text import normalize_symbol

FLOW_FILTER_VERSION = 'flow-filters-v3'
RESEARCH_FILTERS = ('retail_acceleration', 'volume_breakout', 'daily_pressure')
FILTER_NAMES = {
    'retail_acceleration': 'تقویت سرانه حقیقی — فرضیه روزانه',
    'volume_breakout': 'حجم و شکست سقف کامل ۶۰ جلسه — فرضیه روزانه',
    'daily_pressure': 'فشار خرید روزانه — بدون شرط نزدیکی به سقف مجاز',
    'special_original': 'فیلتر ۹ شرطی پیوست — تعریف و داده ناکامل',
}


def _number(frame, name, *, allow_boolean=False):
    raw = frame.get(name, pd.Series(np.nan, index=frame.index))
    if not allow_boolean:
        raw = raw.mask(raw.map(lambda value: isinstance(value, (bool, np.bool_))))
    values = pd.to_numeric(raw, errors='coerce')
    return values.where(np.isfinite(values))


def _divide(a, b):
    return (a / b.where(b > 0)).replace([np.inf, -np.inf], np.nan)


def _participant_side(frame, side):
    """An observed zero/zero is known; positive value/volume with zero count is invalid."""
    count = _number(frame, f'{side}_i_count')
    volume = _number(frame, f'{side}_i_volume')
    value = _number(frame, f'{side}_i_value')
    valid_count = (count >= 0) & (count == np.floor(count))
    vol_known = valid_count & (volume >= 0) & ((count > 0) == (volume > 0))
    val_known = valid_count & (value >= 0) & ((count > 0) == (value > 0))
    return count, volume.where(vol_known), value.where(val_known)


def _coherent_side(prices, clients, side):
    """خام حفظ می‌شود؛ حدود هر سمت و میانگین قیمت از واحد ریال مستقل کنترل می‌شوند.

    تحمل جمع: یک ریال یا یک میلیونیم کل؛ تحمل قیمت ضمنی: یک ریال.
    نبود حقوقی، جمع کامل را تأیید نمی‌کند اما کنترل مستقل حقیقی را حذف نمی‌کند.
    """
    count, retail_volume, retail_value = _participant_side(clients, side)
    total_volume, total_value = _number(prices, 'volume'), _number(prices, 'value')
    legal_volume, legal_value = _number(clients, f'{side}_n_volume'), _number(clients, f'{side}_n_value')
    vol_tol, value_tol = np.maximum(1., total_volume.abs()*1e-6), np.maximum(1., total_value.abs()*1e-6)
    volume_ok = (total_volume >= 0) & (retail_volume <= total_volume + vol_tol)
    legal_vol_present = legal_volume.notna()
    volume_ok &= (~legal_vol_present | ((legal_volume >= 0)
                  & ((retail_volume + legal_volume - total_volume).abs() <= vol_tol)))
    low, high = _number(prices, 'low_raw'), _number(prices, 'high_raw')
    range_ok = (low > 0) & (high >= low)
    implied = _divide(retail_value, retail_volume)
    price_ok = ((retail_volume == 0) & (retail_value == 0)) | (range_ok & implied.between(low-1., high+1.))
    market_price = _divide(total_value, total_volume)
    market_ok = ((total_value == 0) & (total_volume == 0)) | (range_ok & market_price.between(low-1., high+1.))
    value_ok = volume_ok & market_ok & (total_value >= 0) & (retail_value <= total_value + value_tol) & price_ok
    legal_present = legal_value.notna()
    value_ok &= (~legal_present | ((legal_value >= 0)
                 & ((retail_value + legal_value - total_value).abs() <= value_tol)))
    legal_price = _divide(legal_value, legal_volume)
    legal_price_ok = ((legal_volume == 0) & (legal_value == 0)) | (range_ok & legal_price.between(low-1., high+1.))
    value_ok &= (~(legal_present & legal_vol_present) | legal_price_ok)
    known = retail_value.notna() & retail_volume.notna() & total_value.notna() & total_volume.notna()
    reason = pd.Series(np.where(value_ok, 'verified_component',
                       np.where(known, 'inconsistent_totals_or_implied_price', 'missing')), index=prices.index)
    reason.loc[value_ok & legal_present & legal_vol_present] = 'verified_side_total'
    return count, retail_volume.where(volume_ok), retail_value.where(value_ok), reason


def _one_symbol(prices, clients):
    p = prices.sort_values('date').reset_index(drop=True)
    c = p[['date']].merge(clients, on='date', how='left', validate='one_to_one')
    out = p[['symbol', 'date']].copy()
    volume, value = _number(p, 'volume'), _number(p, 'value')
    volume, value = volume.where(volume >= 0), value.where(value >= 0)
    last = _number(p, 'close_raw').where(lambda x: x > 0)
    official = market_rules.official_close(p)
    ref = _number(p, 'ref_price_raw').where(lambda x: x > 0)
    high, low = _number(p, 'high_raw'), _number(p, 'low_raw')
    range_known = (low > 0) & (high >= low) & last.between(low, high)
    out['volume'], out['value'], out['trade_count'] = volume, value, _number(p, 'trade_count')
    out['last_change_pct'] = ((_divide(last, ref) - 1) * 100).where(range_known)
    out['last_vs_official_pp'] = (_divide(last - official, ref) * 100).where(range_known)
    out['range_position'] = _divide(last - low, high - low).where(range_known)
    out['last_above_official'] = (last >= official).astype('boolean').where(range_known & official.notna())
    buy_count, buy_volume, buy_value, buy_quality = _coherent_side(p, c, 'buy')
    sell_count, sell_volume, sell_value, sell_quality = _coherent_side(p, c, 'sell')
    out['client_buy_value_quality'], out['client_sell_value_quality'] = buy_quality, sell_quality
    buy_cap, sell_cap = _divide(buy_value, buy_count), _divide(sell_value, sell_count)
    out['per_capita_buy_rial'], out['per_capita_sell_rial'] = buy_cap, sell_cap
    out['buyer_power_value'] = _divide(buy_cap, sell_cap)
    out['buyer_power_volume'] = _divide(_divide(buy_volume, buy_count), _divide(sell_volume, sell_count))
    out['net_real_value_rial'] = buy_value - sell_value
    legal = _number(c, 'buy_n_volume').where(lambda x: x >= 0)
    total_buy = buy_volume + legal
    # Inconsistent market/client totals cannot masquerade as institutional participation.
    coherent = (total_buy - volume).abs() <= np.maximum(1.0, volume * 1e-6)
    out['legal_buy_fraction'] = _divide(legal, total_buy).where(coherent)
    for n in (3, 5):
        out[f'per_capita_buy_mean_{n}d_rial'] = buy_cap.rolling(n, min_periods=n).mean()
        # This is a different statistic: counts are participant-days, not unique people.
        out[f'per_capita_buy_pooled_{n}d_rial'] = _divide(
            buy_value.rolling(n, min_periods=n).sum(), buy_count.where(buy_value.notna()).rolling(n, min_periods=n).sum())
    out['per_capita_buy_prior_20d_ratio'] = _divide(buy_cap, buy_cap.shift(1).rolling(20, min_periods=20).mean())
    for n in (20, 30):
        prior = volume.shift(1).rolling(n, min_periods=n).mean()
        comparable = volume_comparability(p, n)
        out[f'volume_prior_{n}d_mean'] = prior
        out[f'volume_comparable_{n}d'] = comparable
        # شکاف مرجع نوع اقدام شرکتی را ثابت نمی‌کند؛ تعداد سهم را از آن حدس نمی‌زنیم.
        out[f'volume_ratio_{n}d'] = _divide(volume, prior).where(comparable)
    out['value_prior_20d_mean_rial'] = value.shift(1).rolling(20, min_periods=20).mean()
    # Normalize each raw high/last to a causal return index. Never compare pre/post-action raw prices.
    ret = market_rules.pit_daily_returns(p)
    index = (1 + ret.fillna(0)).cumprod()
    previous = index.shift(1).fillna(1.0)
    high_index = (_divide(high, ref) * previous).where(range_known & ret.notna())
    last_index = (_divide(last, ref) * previous).where(range_known & ret.notna())
    ceiling = high_index.shift(1).rolling(60, min_periods=60).max()
    path_known = ret.rolling(61, min_periods=61).count() == 61
    out['breakout_60d'] = (last_index > ceiling).astype('boolean').where(path_known & ceiling.notna() & last_index.notna())
    return out


def flow_metrics(prices: pd.DataFrame, clients: pd.DataFrame) -> pd.DataFrame:
    """One row per symbol/date; caller supplies ordinary equities and an explicit cutoff."""
    keys = ['symbol', 'date']
    prices, clients = prices.copy(), clients.copy()
    for frame in (prices, clients):
        if not set(keys) <= set(frame):
            raise ValueError('ورودی فیلتر به نماد/تاریخ یکتا نیاز دارد')
        frame['symbol'] = frame['symbol'].map(normalize_symbol)
        if frame.duplicated(keys).any():
            raise ValueError('ورودی فیلتر به نماد/تاریخ یکتا نیاز دارد')
    if prices.empty:
        return pd.DataFrame(columns=keys)
    groups = {symbol: group.drop(columns='symbol') for symbol, group in clients.groupby('symbol', sort=False)}
    rows = [_one_symbol(group, groups.get(symbol, pd.DataFrame(columns=['date'])))
            for symbol, group in prices.groupby('symbol', sort=True)]
    return pd.concat(rows, ignore_index=True)


def filter_conditions(metrics: pd.DataFrame, name: str) -> pd.DataFrame:
    """Nullable predicates: missing or nonfinite input is unknown, never zero or a pass."""
    if name not in FILTER_NAMES:
        raise ValueError('فیلتر شناخته‌شده لازم است')
    conditions = {}

    def compare(label, left, op, right):
        a = _number(metrics, left, allow_boolean=True)
        b = _number(metrics, right, allow_boolean=True) if isinstance(right, str) else pd.Series(right, index=metrics.index)
        known = a.notna() & b.notna()
        conditions[label] = getattr(a, op)(b).astype('boolean').where(known)

    if name in RESEARCH_FILTERS:
        compare('میانگین ارزش ۲۰ جلسه قبل > ۲۰ میلیارد ریال', 'value_prior_20d_mean_rial', 'gt', 20_000_000_000)
    if name == 'retail_acceleration':
        compare('میانگین سرانه ارزشی ۳ جلسه > ۵ جلسه (شامل امروز)', 'per_capita_buy_mean_3d_rial', 'gt', 'per_capita_buy_mean_5d_rial')
        compare('سهم حجمی خرید حقوقی < ۳۰٪', 'legal_buy_fraction', 'lt', .30)
        compare('تعداد معاملات > ۵۰', 'trade_count', 'gt', 50)
        compare('قدرت خریدار ارزشی > ۲', 'buyer_power_value', 'gt', 2)
        compare('خالص ارزش خرید حقیقی مثبت', 'net_real_value_rial', 'gt', 0)
    elif name == 'volume_breakout':
        compare('حجم > ۳ برابر میانگین ۳۰ جلسه قبل', 'volume_ratio_30d', 'gt', 3)
        compare('قدرت خریدار حجمی ≥ ۱٫۵', 'buyer_power_volume', 'ge', 1.5)
        compare('آخرین ≥ پایانی', 'last_above_official', 'eq', 1)
        compare('درصد آخرین مثبت', 'last_change_pct', 'gt', 0)
        compare('تعداد معاملات > ۱۰۰', 'trade_count', 'gt', 100)
        compare('شکست تمام سقف‌های ۶۰ جلسه قبل با تعدیل علّی', 'breakout_60d', 'eq', 1)
    elif name == 'daily_pressure':
        compare('حجم ≥ ۲ برابر میانگین ۲۰ جلسه قبل', 'volume_ratio_20d', 'ge', 2)
        compare('قدرت خریدار حجمی > ۲', 'buyer_power_volume', 'gt', 2)
    else:
        compare('حجم > نصف میانگین ۳۰ جلسه قبل', 'volume_ratio_30d', 'gt', .5)
        compare('حجم هفتگی / شناوری تاریخی > ۲٪ — داده موجود نیست', 'weekly_float_turnover', 'gt', .02)
        compare('آخرین < سقف مجاز همان روز — تاریخچه موجود نیست', 'below_actual_upper_limit', 'eq', 1)
        compare('سهم حجمی خرید حقوقی < ۳۰٪', 'legal_buy_fraction', 'lt', .30)
        compare('سرانه امروز > میانگین سرانه ۳ جلسه', 'per_capita_buy_rial', 'gt', 'per_capita_buy_mean_3d_rial')
        compare('تعداد معاملات > ۵۰', 'trade_count', 'gt', 50)
        compare('سرانه فروش / میانگین بازار < ۱٫۱ — تعریف/جامعه نامعلوم', 'sell_to_market_mean', 'lt', 1.1)
        compare('میانگین سرانه ۳ جلسه > ۵ جلسه', 'per_capita_buy_mean_3d_rial', 'gt', 'per_capita_buy_mean_5d_rial')
        compare('سرانه امروز > میانگین بازار — تعریف/جامعه نامعلوم', 'buy_to_market_mean', 'gt', 1)
    return pd.DataFrame(conditions, index=metrics.index)


def filter_diagnostics(metrics: pd.DataFrame, name: str) -> pd.DataFrame:
    conditions = filter_conditions(metrics, name)
    out = metrics[['symbol', 'date']].copy()
    out['known_conditions'] = conditions.notna().sum(axis=1)
    out['total_conditions'] = len(conditions.columns)
    out['failed_conditions'] = (conditions == False).sum(axis=1)  # noqa: E712
    out['match'] = conditions.fillna(False).all(axis=1)
    out['status'] = np.where(out['match'], 'منطبق', np.where(out['failed_conditions'] > 0, 'رد شده', 'نامعلوم'))
    # Construct each distinct explanation once. A Python row callback on a full
    # market archive is needlessly expensive and repeats identical large strings.
    bits = np.left_shift(np.uint64(1), np.arange(len(conditions.columns), dtype=np.uint64))
    patterns = (conditions.isna().to_numpy(dtype=np.uint64) * bits).sum(axis=1)
    explanations = {int(code): '؛ '.join(label for i, label in enumerate(conditions.columns) if int(code) & (1 << i))
                    for code in np.unique(patterns)}
    out['missing_conditions'] = pd.Series(patterns, index=metrics.index).map(explanations)
    return out
