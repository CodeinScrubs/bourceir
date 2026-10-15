"""مطالعه توصیفیِ بازه‌های از پیش تعیین‌شده؛ هیچ مدل، سیگنال یا احتمال سود نمی‌سازد."""

from decimal import Decimal

import numpy as np
import pandas as pd

from core import market_rules
from core.text import normalize_symbol

STOCK_STUDY_VERSION = 'stock-study-v1'


def finite(value):
    if isinstance(value, (bool, np.bool_)):
        return None
    try:
        value = float(value)
        return value if np.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def validate_panel(frame):
    result = frame.copy()
    if not {'symbol', 'date'} <= set(result):
        raise ValueError('داده مطالعه به نماد و تاریخ نیاز دارد')
    if result.empty:
        return result
    result['symbol'] = result.symbol.map(normalize_symbol)
    dates = pd.to_datetime(result.date, format='%Y-%m-%d', errors='coerce')
    if (dates.isna().any() or not dates.dt.strftime('%Y-%m-%d').equals(result.date)
            or result.duplicated(['symbol', 'date']).any() or result.symbol.eq('').any()):
        raise ValueError('نماد/تاریخ معتبر و یکتا برای مطالعه لازم است')
    return result.sort_values(['symbol', 'date']).reset_index(drop=True)


def describe_window(prices, start, end):
    """قیمت شروع/پایان دقیق؛ نزدیک‌ترین روز جای تاریخ انتخابی نمی‌نشیند."""
    if start > end:
        raise ValueError('بازه مطالعه وارونه است')
    p = prices.sort_values('date').reset_index(drop=True)
    if p.date.duplicated().any() or ('symbol' in p and p.symbol.nunique() > 1):
        raise ValueError('سابقه یکتای یک نماد لازم است')
    w = p.loc[p.date.between(start, end)].copy()
    exact = bool(len(w) and w.date.iloc[0] == start and w.date.iloc[-1] == end)
    result = dict(start=start, end=end, observed_rows=len(w), exact_endpoints=exact,
                  observation_calendar_verified=False,
                  first_observed=str(w.date.iloc[0]) if len(w) else None,
                  last_observed=str(w.date.iloc[-1]) if len(w) else None,
                  last_price_multiple=None, official_price_multiple=None, synthetic_multiple=None,
                  synthetic_close_drawdown=None, reference_gaps=[], unknown_returns=0,
                  economic_return=None, executable_return=None, causality_proven=False)
    if w.empty:
        return result
    factors = market_rules.corporate_action_factors(p)
    action = p.loc[p.date.between(start, end) & p.date.gt(start) & factors.notna() & factors.ne(1), ['date']].copy()
    action['reference_ratio'] = factors.loc[action.index]
    result['reference_gaps'] = action.to_dict('records')
    returns = market_rules.pit_daily_returns(w).iloc[1:]
    result['unknown_returns'] = int(returns.isna().sum())
    if not exact:
        return result
    for name, col in (('last_price_multiple', 'close_raw'), ('official_price_multiple', 'close_final_raw')):
        first, last = (finite(w[col].iloc[index]) for index in (0, -1))
        if first is not None and first > 0 and last is not None and last > 0:
            result[name] = last / first
    if returns.notna().all():
        path = pd.Series([1., *(1+returns).cumprod().tolist()])
        result['synthetic_multiple'] = finite(path.iloc[-1])
        result['synthetic_close_drawdown'] = finite((path/path.cummax()-1).min())
    return result


def describe_pre_window(prices, before, sessions):
    """فقط مشاهدات قبل از روز انتخاب‌شده؛ مفقود با حذف سطر پر نمی‌شود."""
    if isinstance(sessions, bool) or not isinstance(sessions, int) or sessions < 1:
        raise ValueError('اندازه پنجره باید صحیح مثبت باشد')
    p = prices.loc[prices.date < before].sort_values('date').tail(sessions)
    result = dict(sessions_requested=sessions, observed_rows=len(p), end=str(p.date.iloc[-1]) if len(p) else None,
                  calendar_days_since_last_observation=(pd.Timestamp(before)-pd.Timestamp(p.date.iloc[-1])).days if len(p) else None,
                  observation_calendar_verified=False,
                  synthetic_return=None, mean_value_rial=None, zero_volume_rows=0, locked_up_rows=0, locked_down_rows=0)
    if len(p) != sessions:
        return result
    ret = market_rules.pit_daily_returns(p)
    if ret.notna().all():
        result['synthetic_return'] = finite((1+ret).prod()-1)
    values = pd.to_numeric(p.value, errors='coerce')
    if np.isfinite(values).all() and values.ge(0).all():
        result['mean_value_rial'] = float(values.mean())
    result['zero_volume_rows'] = int(p.volume.eq(0).sum())
    locks = market_rules.lock_flags(p)
    result['locked_up_rows'], result['locked_down_rows'] = (int(locks[key].sum()) for key in ('locked_up', 'locked_down'))
    return result


def peer_summary(records):
    """زیرمجموعه معلوم آشکار است؛ میانگین کامل با یک نتیجه نامعلوم عدد نمی‌گیرد."""
    values = [row['synthetic_multiple'] for row in records if row['synthetic_multiple'] is not None]
    return dict(expected=len(records), known=len(values), unknown=len(records)-len(values),
                mean_multiple=float(np.mean(values)) if values and len(values) == len(records) else None,
                known_subset_mean_multiple=float(np.mean(values)) if values else None,
                known_subset_median_multiple=float(np.median(values)) if values else None,
                known_subset_doubled=sum(value >= 2 for value in values),
                known_subset_negative=sum(value < 1 for value in values),
                official_index=False, historical_membership_verified=False,
                model='equal weight endpoint synthetic multiples of current-metadata peers; no fees, no fills')


def compare_peers(panel, metadata, symbol, start, end, *, _window_cache=None):
    symbol = normalize_symbol(symbol)
    meta = {normalize_symbol(row['symbol_fa']): row for row in metadata
            if row.get('instrument_type_code') in (300, 303) and not normalize_symbol(row['symbol_fa'])[-1:].isdigit()}
    if symbol not in meta:
        raise ValueError('نماد مطالعه داخل دامنه متادیتای معلوم نیست')
    groups = {key: group for key, group in panel.groupby('symbol', sort=False)}
    peers = []
    target_adv = describe_pre_window(groups.get(symbol, panel.iloc[:0]), start, 20)['mean_value_rial']
    for name, row in sorted(meta.items()):
        if name == symbol:
            continue
        p = groups.get(name, panel.iloc[:0])
        key=(name,start,end)
        cached=_window_cache.get(key) if _window_cache is not None else None
        if cached is None:
            cached=(describe_window(p,start,end),describe_pre_window(p,start,20))
            if _window_cache is not None:
                _window_cache[key]=cached
        window,prior=cached
        adv = prior['mean_value_rial']
        distance = abs(float(np.log(adv/target_adv))) if adv and target_adv else None
        peers.append(dict(symbol=name, sector_code=row.get('sector_code'), **window, pre_20=prior,
                          pre_liquidity_distance=distance))
    sector_code = meta[symbol].get('sector_code')
    sector = [row for row in peers if sector_code is not None and row['sector_code'] == sector_code]
    # انتخاب کنترل فقط از صنعت فعلی و ارزش معاملات پیشین؛ نتیجه آینده در رتبه‌بندی مصرف نمی‌شود.
    matched = sorted((row for row in sector if row['pre_liquidity_distance'] is not None),
                     key=lambda row:(row['pre_liquidity_distance'],row['symbol']))[:5]
    return dict(market=peer_summary(peers), sector=peer_summary(sector), sector_members=sector,
                matched_by_prior_liquidity=matched, market_members=peers,
                control_selection='same current sector, nearest absolute log prior20 ADV ratio, max5; no future-outcome selection',
                historical_size_and_float_matching_verified=False)


def observed_market_caps(instruments, snapshots):
    """رتبه مشاهده فعلی، با مخرج و کرانِ مقادیر مفقود؛ هیچ رتبه تاریخی جعل نمی‌شود."""
    quotes = {}
    for row in snapshots:
        quotes.setdefault(str(row['ins_code']), []).append(row)
    rows = []
    for item in instruments:
        if item.get('instrument_type_code') not in (300,303) or normalize_symbol(item['symbol_fa'])[-1:].isdigit():
            continue
        obs = quotes.get(str(item['ins_code']), [])
        shares = finite(item.get('shares_outstanding'))
        price = finite(obs[0].get('last_price')) if len(obs)==1 else None
        cap = None
        if shares and shares > 0 and shares.is_integer() and price and price > 0:
            cap = int(Decimal(str(int(shares)))*Decimal(str(price)))
        rows.append(dict(symbol=normalize_symbol(item['symbol_fa']), ins_code=str(item['ins_code']),
                         shares_observed=shares, last_price_observed_rial=price, market_cap_rial=cap,
                         provider_timestamp=obs[0].get('provider_timestamp') if len(obs)==1 else None,
                         historical_effective_shares_verified=False, historic_rank=None))
    if len({row['ins_code'] for row in rows}) != len(rows) or len({row['symbol'] for row in rows}) != len(rows):
        raise ValueError('جامعه رتبه‌بندی باید شناسه و نماد یکتا داشته باشد')
    known = [row['market_cap_rial'] for row in rows if row['market_cap_rial'] is not None]
    unknown = len(rows)-len(known)
    for row in rows:
        value = row['market_cap_rial']
        rank = 1+sum(cap > value for cap in known) if value is not None else None
        row.update(rank_among_known=rank, rank_lower_bound=rank,
                   rank_upper_bound=rank+unknown if rank is not None else None,
                   universe_total=len(rows), universe_known=len(known), universe_missing=unknown,
                   ranking_basis='current observed shares x current observed last; effective date unverified')
    return rows
