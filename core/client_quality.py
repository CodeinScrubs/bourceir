"""کنترل مشترک آمار حقیقی/حقوقی؛ خام دست‌نخورده می‌ماند و تناقض، نامعلوم است."""

import math

import numpy as np
import pandas as pd

CLIENT_QUALITY_VERSION = 'client-quality-v1'


def snapshot_buyer_power(snapshot):
    """قدرت خریدار حجمی زنده؛ شمار ناصحیح/حجم ناسازگار، عدد صفر یا اطمینان نمی‌سازد."""
    def number(name):
        raw = snapshot.get(name)
        if isinstance(raw, (bool, np.bool_)):
            return None
        try:
            value = float(raw)
            return value if math.isfinite(value) else None
        except (TypeError, ValueError, OverflowError):
            return None

    values = [number(key) for key in ('real_buy_volume', 'real_buy_count', 'real_sell_volume', 'real_sell_count')]
    if any(value is None or value <= 0 for value in values):
        return None
    buy_vol, buy_count, sell_vol, sell_count = values
    if not buy_count.is_integer() or not sell_count.is_integer():
        return None
    total = number('volume')
    if snapshot.get('volume') is not None:
        if total is None or total <= 0:
            return None
        tolerance = max(1., total*1e-6)
        for side, volume in (('buy', buy_vol), ('sell', sell_vol)):
            if volume > total + tolerance:
                return None
            legal = number(f'legal_{side}_volume')
            if snapshot.get(f'legal_{side}_volume') is not None:
                if legal is None or legal < 0 or abs(volume + legal - total) > tolerance:
                    return None
    buy_cap, sell_cap = buy_vol / buy_count, sell_vol / sell_count
    if not all(math.isfinite(value) and value > 0 for value in (buy_cap, sell_cap)):
        return None
    power = buy_cap / sell_cap
    return power if math.isfinite(power) else None


def finite_number(frame, name, *, allow_boolean=False):
    raw = frame.get(name, pd.Series(np.nan, index=frame.index))
    if not allow_boolean:
        raw = raw.mask(raw.map(lambda value: isinstance(value, (bool, np.bool_))))
    values = pd.to_numeric(raw, errors='coerce').astype('float64')
    return values.where(np.isfinite(values))


def _divide(a, b):
    return (a / b.where(b > 0)).replace([np.inf, -np.inf], np.nan)


def _participant_side(frame, side):
    """صفر مشاهده‌شده معتبر است؛ شمار ناصحیح یا ناسازگار با حجم/ارزش معتبر نیست."""
    count = finite_number(frame, f'{side}_i_count')
    volume = finite_number(frame, f'{side}_i_volume')
    value = finite_number(frame, f'{side}_i_value')
    valid_count = (count >= 0) & (count == np.floor(count))
    vol_known = valid_count & (volume >= 0) & ((count > 0) == (volume > 0))
    val_known = valid_count & (value >= 0) & ((count > 0) == (value > 0))
    return count, volume.where(vol_known), value.where(val_known)


def coherent_side(prices, clients, side):
    """حجم مستقل از ارزش کنترل می‌شود؛ ورودی‌ها باید به همان ردیف‌های روزانه تراز باشند.

    تحمل جمع: یک ریال/سهم یا یک میلیونیم کل؛ تحمل قیمت ضمنی: یک ریال.
    نبود حقوقی، تأیید جمع کامل نیست. این کنترل، تأیید مستقل صحت منبع نیز نیست.
    """
    if len(prices) != len(clients) or not prices.index.equals(clients.index):
        raise ValueError('ردیف‌های قیمت و حقیقی/حقوقی باید تراز باشند')
    if side not in ('buy', 'sell'):
        raise ValueError('سمت خرید یا فروش لازم است')
    count, retail_volume, retail_value = _participant_side(clients, side)
    total_volume, total_value = finite_number(prices, 'volume'), finite_number(prices, 'value')
    legal_volume = finite_number(clients, f'{side}_n_volume')
    legal_value = finite_number(clients, f'{side}_n_value')
    vol_tol, value_tol = np.maximum(1., total_volume.abs()*1e-6), np.maximum(1., total_value.abs()*1e-6)
    volume_ok = (total_volume >= 0) & (retail_volume <= total_volume + vol_tol)
    legal_vol_present = legal_volume.notna()
    volume_ok &= (~legal_vol_present | ((legal_volume >= 0)
                  & ((retail_volume + legal_volume - total_volume).abs() <= vol_tol)))
    low, high = finite_number(prices, 'low_raw'), finite_number(prices, 'high_raw')
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


def validated_client_data(prices, client_types=None):
    """یک نماد، یک ردیف در هر تاریخ؛ تراز بر حسب تاریخ و حفظ ترتیب قیمت ورودی.

    حجم سالم با خراب بودن ارزش از بین نمی‌رود. حقوقی فقط با جمع حجمی سالم نگه داشته می‌شود.
    ستون‌های کیفیت، شاهد سازگاری‌اند و مقدار نامعلوم را با صفر پر نمی‌کنند.
    """
    p = prices.reset_index(drop=True)
    clients = client_types if client_types is not None else pd.DataFrame(columns=['date'])
    for frame in (p, clients):
        if 'date' not in frame or frame['date'].isna().any() or frame['date'].duplicated().any():
            raise ValueError('برای هر نماد و تاریخ فقط یک ردیف با تاریخ معلوم مجاز است')
    c = p[['date']].merge(clients, on='date', how='left', validate='one_to_one')
    out = p[['date']].copy()
    out['volume'], out['value'] = finite_number(p, 'volume'), finite_number(p, 'value')
    for side in ('buy', 'sell'):
        count, volume, value, reason = coherent_side(p, c, side)
        out[f'{side}_i_count'] = count.where((count >= 0) & (count == np.floor(count)))
        out[f'{side}_i_volume'], out[f'{side}_i_value'] = volume, value
        out[f'{side}_value_quality'] = reason
        legal = finite_number(c, f'{side}_n_volume').where(lambda x: x >= 0)
        tol = np.maximum(1., out['volume'].abs()*1e-6)
        complete = volume.notna() & ((volume + legal - out['volume']).abs() <= tol)
        out[f'{side}_n_volume'] = legal.where(complete)
    return out
