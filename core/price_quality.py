"""نقص عددی قیمت خام با علت مستقل؛ نامعلوم با خارج‌بودن از دامنه یکی نیست."""

import numpy as np
import pandas as pd

VERSION = 'raw-price-quality-v1'
RAW_PRICES = ('open_raw', 'high_raw', 'low_raw', 'close_raw', 'close_final_raw', 'ref_price_raw', 'volume', 'value')


def numeric_prices(frame, fields=RAW_PRICES):
    """Coerce raw fields without turning booleans into prices, quantities or value.

    A fresh float buffer also handles nullable columns and never edits the caller's
    snapshot while replacing unknown/nonfinite numbers with NaN.
    """
    result={}
    for field in fields:
        source=frame[field] if field in frame else pd.Series(np.nan,index=frame.index)
        if pd.api.types.is_bool_dtype(source.dtype):
            source=pd.Series(np.nan,index=frame.index)
        elif not pd.api.types.is_numeric_dtype(source.dtype):
            source=source.mask(source.map(lambda value:isinstance(value,(bool,np.bool_))))
        numbers=pd.to_numeric(source,errors='coerce').to_numpy(dtype=float,na_value=np.nan,copy=True)
        numbers[~np.isfinite(numbers)]=np.nan
        result[field]=numbers
    return pd.DataFrame(result,index=frame.index)


def price_quality(frame):
    """اعتبار عددی مشاهده؛ قیمت پایانی رسمی لازم نیست داخل بازه معاملات باشد."""
    numeric = numeric_prices(frame)
    finite = pd.DataFrame(np.isfinite(numeric.to_numpy(dtype=float, na_value=np.nan)),
                          index=frame.index, columns=RAW_PRICES)
    reasons = {}
    for field in RAW_PRICES:
        reasons[f'missing_or_nonfinite_{field}'] = ~finite[field]
        reasons[f'nonpositive_{field}'] = finite[field] & numeric[field].le(0)
    reasons['fractional_volume'] = finite.volume & numeric.volume.ne(np.floor(numeric.volume))
    comparable = finite.low_raw & finite.high_raw & numeric.low_raw.gt(0) & numeric.high_raw.gt(0)
    reasons['inverted_range'] = comparable & numeric.low_raw.gt(numeric.high_raw)
    ordered = comparable & ~reasons['inverted_range']
    for field in ('open_raw', 'close_raw'):
        reasons[f'outside_range_{field}'] = (ordered & finite[field] & numeric[field].gt(0)
                                           & ~numeric[field].between(numeric.low_raw, numeric.high_raw))
    implied = numeric.value / numeric.volume.where(numeric.volume.gt(0))
    reasons['vwap_outside_range'] = (ordered & finite.value & finite.volume & numeric.value.gt(0)
                                    & numeric.volume.gt(0)
                                    & ~implied.between(numeric.low_raw - 1., numeric.high_raw + 1.))
    flags = pd.DataFrame(reasons, index=frame.index).fillna(False).astype(bool)
    return numeric, flags


def validated_feature_prices(frame):
    """A non-mutating raw-price view for features; invalid dependent cells stay unknown.

    Official close/reference are independent of the traded range (base volume can
    put the official close outside it). A contradictory value/volume pair cannot
    identify which total is wrong, so neither total is used as a feature input.
    This does not repair the database or certify historical source availability.
    """
    numeric, flags = price_quality(frame)
    clean = numeric.where(numeric.gt(0))
    clean['volume'] = clean.volume.where(~flags.fractional_volume)
    range_known = clean.low_raw.notna() & clean.high_raw.ge(clean.low_raw)
    clean[['low_raw', 'high_raw']] = clean[['low_raw', 'high_raw']].where(range_known, axis=0)
    for field in ('open_raw', 'close_raw'):
        clean[field] = clean[field].where(clean[field].between(clean.low_raw, clean.high_raw))
    clean.loc[flags.vwap_outside_range, ['volume', 'value']] = np.nan
    clean['value'] = clean.value.where(range_known & clean.volume.notna())
    result = frame.copy(deep=True)
    result[list(RAW_PRICES)] = clean
    return result


def json_price_row(row):
    """فقط ستون‌های عمومی قیمت؛ داده حساب یا قیمت تعدیل‌شده منتقل نمی‌شود."""
    result = {}
    for key in ('date', *RAW_PRICES, 'trade_count'):
        value = row.get(key)
        if key == 'date':
            result[key] = str(value) if value is not None else None
        else:
            if isinstance(value,(bool,np.bool_)):
                result[key]=None
                continue
            number = pd.to_numeric(value, errors='coerce')
            result[key] = float(number) if number is not None and pd.notna(number) and np.isfinite(number) else None
    return result
