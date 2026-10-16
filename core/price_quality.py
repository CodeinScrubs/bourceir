"""نقص عددی قیمت خام با علت مستقل؛ نامعلوم با خارج‌بودن از دامنه یکی نیست."""

import numpy as np
import pandas as pd

VERSION = 'raw-price-quality-v1'
RAW_PRICES = ('open_raw', 'high_raw', 'low_raw', 'close_raw', 'close_final_raw', 'ref_price_raw', 'volume', 'value')


def price_quality(frame):
    """اعتبار عددی مشاهده؛ قیمت پایانی رسمی لازم نیست داخل بازه معاملات باشد."""
    numeric = frame.reindex(columns=RAW_PRICES).apply(pd.to_numeric, errors='coerce')
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


def json_price_row(row):
    """فقط ستون‌های عمومی قیمت؛ داده حساب یا قیمت تعدیل‌شده منتقل نمی‌شود."""
    result = {}
    for key in ('date', *RAW_PRICES, 'trade_count'):
        value = row.get(key)
        if key == 'date':
            result[key] = str(value) if value is not None else None
        else:
            number = pd.to_numeric(value, errors='coerce')
            result[key] = float(number) if number is not None and pd.notna(number) and np.isfinite(number) else None
    return result
