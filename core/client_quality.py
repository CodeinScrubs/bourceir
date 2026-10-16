"""کنترل مشترک آمار حقیقی/حقوقی؛ خام دست‌نخورده می‌ماند و تناقض، نامعلوم است."""

import math

import numpy as np
import pandas as pd

CLIENT_QUALITY_VERSION = 'client-quality-v2'

VOLUME_QUALITY_LABELS_FA = {
    'verified_component': 'مؤلفه حقیقی سازگار؛ جمع کامل تأیید نشده',
    'verified_side_total': 'جمع حجم حقیقی و حقوقی سازگار',
    'missing_or_invalid_retail_volume': 'حجم حقیقی غایب یا نامعتبر',
    'invalid_count_volume_pair': 'تعداد و حجم حقیقی ناسازگار',
    'invalid_legal_volume': 'حجم حقوقی نامعتبر',
    'invalid_total_volume': 'حجم کل نامعتبر یا بدون معامله',
    'retail_volume_exceeds_total': 'حجم حقیقی بیشتر از کل معاملات',
    'inconsistent_side_total': 'جمع حجم حقیقی و حقوقی ناسازگار',
}


def _missing_scalar(value):
    """None/NaN/pd.NA غایب‌اند؛ inf، boolean و متن خراب غایب نیستند."""
    if value is None or value is pd.NA:
        return True
    return isinstance(value, (float, np.floating)) and math.isnan(value)


def _snapshot_number(value):
    if _missing_scalar(value) or isinstance(value, (bool, np.bool_)):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def snapshot_client_metrics(snapshot):
    """نسبت سرانه و تخمین جریان، با قرارداد مشترک حجم و unknown واقعی.

    نبود شمار، تخمین حجمی را حذف نمی‌کند؛ شمار موجودِ متناقض آن را رد می‌کند.
    بدون کل حجم، فقط نسبت مؤلفه‌های حقیقی قابل محاسبه است؛ تخمین جریان نیست.
    این تابع تطابق ساعت دو منبع یا ارزش واقعی معاملات حقیقی را اثبات نمی‌کند.
    """
    total_raw = snapshot.get('volume')
    total = _snapshot_number(total_raw)
    sides = {}
    for side in ('buy', 'sell'):
        volume = _snapshot_number(snapshot.get(f'real_{side}_volume'))
        count_raw = snapshot.get(f'real_{side}_count')
        count = _snapshot_number(count_raw)
        legal_raw = snapshot.get(f'legal_{side}_volume')
        legal = _snapshot_number(legal_raw)
        reason = 'verified_component'
        if volume is None or volume < 0 or not volume.is_integer():
            reason = 'missing_or_invalid_retail_volume'
        elif not _missing_scalar(count_raw) and (count is None or count < 0 or not count.is_integer()
                                                or ((count > 0) != (volume > 0))):
            reason = 'invalid_count_volume_pair'
        elif not _missing_scalar(legal_raw) and (legal is None or legal < 0 or not legal.is_integer()):
            reason = 'invalid_legal_volume'
        elif not _missing_scalar(total_raw):
            if total is None or total <= 0 or not total.is_integer():
                reason = 'invalid_total_volume'
            else:
                tolerance = max(1., total * 1e-6)
                if volume > total + tolerance:
                    reason = 'retail_volume_exceeds_total'
                elif legal is not None:
                    reason = ('verified_side_total' if abs(volume + legal - total) <= tolerance
                              else 'inconsistent_side_total')
        valid = reason in ('verified_component', 'verified_side_total')
        sides[side] = dict(volume=volume if valid else None, count=count, quality=reason)
    buy, sell = sides['buy'], sides['sell']
    power = None
    if all(s['volume'] is not None and s['volume'] > 0 and s['count'] is not None
           and s['count'] > 0 and s['count'].is_integer() for s in (buy, sell)):
        buy_cap, sell_cap = buy['volume'] / buy['count'], sell['volume'] / sell['count']
        if all(math.isfinite(value) and value > 0 for value in (buy_cap, sell_cap)):
            value = buy_cap / sell_cap
            if math.isfinite(value):
                power = value
    flow = None
    last = _snapshot_number(snapshot.get('last_price'))
    if (total is not None and total > 0 and last is not None and last > 0
            and buy['volume'] is not None and sell['volume'] is not None):
        value = (buy['volume'] - sell['volume']) * last
        if math.isfinite(value):
            flow = value
    return dict(buyer_power=power, real_flow_estimate_rial=flow,
                buy_volume_quality=buy['quality'], sell_volume_quality=sell['quality'],
                buy_volume_quality_label=VOLUME_QUALITY_LABELS_FA[buy['quality']],
                sell_volume_quality_label=VOLUME_QUALITY_LABELS_FA[sell['quality']],
                clock_coupling_verified=False)


def snapshot_buyer_power(snapshot):
    return snapshot_client_metrics(snapshot)['buyer_power']


def finite_number(frame, name, *, allow_boolean=False):
    raw = frame.get(name, pd.Series(np.nan, index=frame.index))
    if not allow_boolean:
        raw = raw.mask(raw.map(lambda value: isinstance(value, (bool, np.bool_))))
    values = pd.to_numeric(raw, errors='coerce').astype('float64')
    return values.where(np.isfinite(values))


def optional_number_quality(frame, name):
    """علت نبود مؤلفه حقوقی، مستقل از امکان استفاده از مؤلفه حقیقی سالم."""
    raw = frame.get(name, pd.Series(np.nan, index=frame.index))
    missing = raw.map(_missing_scalar)
    numeric = finite_number(frame, name)
    observed = numeric.ge(0)
    if name.endswith('_volume'):
        observed &= numeric.eq(np.floor(numeric))
    return pd.Series(np.where(missing, 'missing', np.where(observed, 'observed', 'invalid')),
                     index=frame.index)


def _divide(a, b):
    return (a / b.where(b > 0)).replace([np.inf, -np.inf], np.nan)


def _participant_side(frame, side):
    """صفر مشاهده‌شده معتبر است؛ شمار ناصحیح یا ناسازگار با حجم/ارزش معتبر نیست."""
    count = finite_number(frame, f'{side}_i_count')
    volume = finite_number(frame, f'{side}_i_volume')
    value = finite_number(frame, f'{side}_i_value')
    valid_count = (count >= 0) & (count == np.floor(count))
    vol_known = valid_count & (volume >= 0) & volume.eq(np.floor(volume)) & ((count > 0) == (volume > 0))
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
    volume_ok = (total_volume >= 0) & total_volume.eq(np.floor(total_volume)) & (retail_volume <= total_volume + vol_tol)
    legal_vol_present = legal_volume.notna()
    volume_ok &= (~legal_vol_present | ((legal_volume >= 0) & legal_volume.eq(np.floor(legal_volume))
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
        out[f'{side}_legal_volume_quality'] = optional_number_quality(c, f'{side}_n_volume')
        out[f'{side}_legal_value_quality'] = optional_number_quality(c, f'{side}_n_value')
        legal = finite_number(c, f'{side}_n_volume').where(lambda x: x >= 0)
        tol = np.maximum(1., out['volume'].abs()*1e-6)
        complete = volume.notna() & ((volume + legal - out['volume']).abs() <= tol)
        out[f'{side}_n_volume'] = legal.where(complete)
    return out
