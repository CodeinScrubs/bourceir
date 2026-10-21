"""Read-only diagnostics for the explicitly selected legacy TSETMC CSV format.

FIRST/LAST describe actual traded prices; CLOSE is the official close. The
ambiguous source OPEN is retained separately and never promoted to a verified
native reference. This is not an adjusted history, listing registry or backtest.
"""
import csv
import hashlib
import io
from datetime import datetime
from decimal import Decimal, InvalidOperation

import numpy as np
import pandas as pd

from core.market_rules import daily_vwap
from core.text import normalize_symbol, to_en_digits

VERSION = 'legacy-price-csv-audit-v1'
FIELDS = {'<FIRST>': 'open_raw', '<HIGH>': 'high_raw', '<LOW>': 'low_raw',
          '<LAST>': 'close_raw', '<CLOSE>': 'close_final_raw', '<OPEN>': 'source_open_raw',
          '<VOL>': 'volume', '<VALUE>': 'value'}


def _number(value, *, integer=False):
    try:
        parsed = Decimal(to_en_digits(value).strip())
        if not parsed.is_finite() or (integer and (parsed != parsed.to_integral_value() or abs(parsed) > 2**53-1)):
            return np.nan
        result = float(parsed)
        return result if np.isfinite(result) else np.nan
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        return np.nan


def inspect_legacy_price_csv(raw, *, symbol, unit):
    """Return all normalized rows and a descriptive audit, without any DB writes.

    Dates/duplicate days/mixed native tickers fail closed. Bad numerical rows are
    retained with reasons, and missing volumes are not zero-volume placeholders.
    The explicit unit is a caller declaration; source units remain unverified.
    """
    if (not isinstance(raw, bytes) or not isinstance(symbol, str) or not normalize_symbol(symbol)
            or unit != 'Rial'):
        raise ValueError('بایت CSV، نماد صریح و واحد Rial لازم است')
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeError as exc:
        raise ValueError('CSV باید UTF-8 باشد؛ کدگذاری حدس زده نمی‌شود') from exc
    reader = csv.DictReader(io.StringIO(text), skipinitialspace=True)
    required = {'<DTYYYYMMDD>', *FIELDS}
    if (not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames)
            or not required <= set(reader.fieldnames)):
        raise ValueError('ستون‌های قالب قیمت قدیمی کامل و یکتا نیستند')
    rows, tickers = [], set()
    for source_row, item in enumerate(reader, 2):
        if None in item or any(item.get(k) is None for k in required):
            raise ValueError('تعداد ستون‌های ردیف CSV ناسازگار است')
        day = to_en_digits(item['<DTYYYYMMDD>']).strip()
        try:
            if len(day) != 8 or not day.isascii() or not day.isdigit():
                raise ValueError('YYYYMMDD')
            day = datetime.strptime(day, '%Y%m%d').date().isoformat()
        except ValueError as exc:
            raise ValueError(f'تاریخ ردیف {source_row} معتبر نیست') from exc
        ticker = item.get('<TICKER>', '').strip()
        if ticker:
            tickers.add(ticker)
        row = dict(date=day, symbol=normalize_symbol(symbol), source_row=source_row)
        for native, column in FIELDS.items():
            row[column] = _number(item[native], integer=column == 'volume')
        rows.append(row)
    if not rows or len(tickers) > 1:
        raise ValueError('فایل خالی یا دارای هویت‌های بومی متفاوت است')
    frame = pd.DataFrame(rows).sort_values('date').reset_index(drop=True)
    if frame.date.duplicated().any():
        raise ValueError('تاریخ تکراری ممکن است اصلاح داده باشد؛ انتخاب دلخواه مجاز نیست')
    volume = frame.volume
    positive = volume.gt(0)
    known_volume = np.isfinite(volume) & volume.ge(0)
    no_trade = known_volume & volume.eq(0)
    trade_dates = frame.loc[positive, 'date']
    first, last = (trade_dates.iloc[0], trade_dates.iloc[-1]) if len(trade_dates) else (None, None)
    issues, valid, single = [], [], []
    vwap = daily_vwap(frame)
    for row in frame.itertuples(index=False):
        reasons = []
        vol = row.volume
        if not np.isfinite(vol) or vol < 0:
            reasons.append('invalid_volume')
        elif vol == 0 and (not np.isfinite(row.value) or row.value != 0):
            reasons.append('inconsistent_nontraded_value')
        elif vol > 0:
            prices = [row.open_raw, row.high_raw, row.low_raw, row.close_raw, row.close_final_raw]
            if not all(np.isfinite(p) and p > 0 for p in prices):
                reasons.append('missing_or_nonpositive_price')
            if not row.high_raw >= row.low_raw > 0:
                reasons.append('invalid_range')
            if not row.low_raw <= row.open_raw <= row.high_raw or not row.low_raw <= row.close_raw <= row.high_raw:
                reasons.append('traded_price_outside_range')
            if not np.isfinite(row.value) or row.value <= 0:
                reasons.append('invalid_traded_value')
        issues.append(reasons)
        valid.append(vol > 0 and not reasons)
        single.append(vol > 0 and not reasons and row.open_raw == row.high_raw == row.low_raw == row.close_raw)
    frame['vwap_rial'] = vwap
    for pos in np.flatnonzero(positive.to_numpy()):
        if not np.isfinite(vwap.iloc[pos]):
            issues[pos].append('invalid_or_out_of_range_vwap')
            valid[pos] = False
            single[pos] = False
    frame['issues'] = [';'.join(r) for r in issues]
    frame['eligible_for_ohlc_description'] = valid
    frame['one_traded_price'] = single
    frame['observed_traded_session'] = positive.cumsum().where(positive).astype('Int64')
    # A price-limited single-price day can have an official close outside its prints.
    official_outside = positive & ((frame.close_final_raw < frame.low_raw) | (frame.close_final_raw > frame.high_raw))
    frame['official_close_outside_trade_range'] = official_outside
    prior = frame.loc[positive, 'close_final_raw'].shift(1)
    native_open = frame.loc[positive, 'source_open_raw']
    comparable = prior.gt(0) & native_open.gt(0) & np.isfinite(prior) & np.isfinite(native_open)
    matches = np.isclose(prior, native_open, atol=0, rtol=0) & comparable
    # Critically, neither shifted CLOSE nor this ambiguous OPEN becomes ref_price_raw.
    frame['ref_price_raw'] = np.nan
    summary = dict(version=VERSION, input_sha256=hashlib.sha256(raw).hexdigest(), symbol=normalize_symbol(symbol),
        declared_unit=unit, source_unit_independently_verified=False,
        native_ticker=next(iter(tickers), None), native_identity_independently_verified=False,
        source_rows=len(frame), positive_volume_rows=int(positive.sum()), zero_volume_rows=int(no_trade.sum()),
        invalid_volume_rows=int((~known_volume).sum()), invalid_rows=sum(bool(r) for r in issues),
        valid_traded_rows=sum(valid), one_traded_price_rows=sum(single),
        official_close_outside_trade_range_rows=int(official_outside.sum()),
        first_observed_trade=first, last_observed_trade=last,
        actual_listing_date=None, historical_membership_verified=False,
        zero_volume_before_first=int((no_trade & frame.date.lt(first)).sum()) if first else None,
        zero_volume_during_span=int((no_trade & frame.date.between(first, last)).sum()) if first else None,
        zero_volume_after_last=int((no_trade & frame.date.gt(last)).sum()) if last else None,
        source_open_matches_previous_official_close=dict(matches=int(matches.sum()), comparable=int(comparable.sum())),
        source_open_semantics='ambiguous_previous_official_close_or_reference', verified_native_reference=False,
        safe_for_feature_store=False, accepted_for_signals=False, profitability_proven=False,
        note='Diagnostic only. Missing reference, publication chronology and corporate entitlements remain unverified.')
    return frame, summary
