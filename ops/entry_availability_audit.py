"""ممیزی فقط‌خواندنیِ شواهدِ عدم ورود؛ خروجی آن راهبرد یا توصیه معاملاتی نیست."""

import argparse
import hashlib
import io
import json
import math
import re
import uuid
from numbers import Real
from pathlib import Path

import numpy as np
import pandas as pd

from config import settings
from core.market_rules import daily_vwap, lock_flags
from core.text import normalize_symbol
from core.timeutils import is_canonical_iso_date, now_tehran_iso
from ops.report_files import private_report_path, write_private_report

VERSION = 'entry-availability-audit-v1'
HARD_SEAL = '2025-03-21'
MAX_INPUT_BYTES = 512 * 1024 * 1024
PRICE_FIELDS = ['volume', 'value', 'low_raw', 'high_raw', 'close_raw', 'close_final_raw', 'ref_price_raw']
CATEGORIES = ('absent_entry_price_row', 'observed_zero_activity_row', 'locked_up_daily_proxy',
              'unlocked_valid_row_conflicts_with_blocked_label', 'invalid_or_incomplete_entry_row')


def _checked_cutoff(cutoff=None):
    configured = settings.RESEARCH_CONFIG['holdout_start']
    if not is_canonical_iso_date(configured):
        raise ValueError('مرز پیکربندی دوره مهروموم معتبر نیست')
    limit = min(configured, HARD_SEAL)
    cutoff = limit if cutoff is None else cutoff
    if not is_canonical_iso_date(cutoff) or cutoff > limit:
        raise ValueError('مرز ممیزی نمی‌تواند دوره مهروموم را باز کند')
    return cutoff


def _snapshot(path, expected):
    if not isinstance(expected, str) or not re.fullmatch(r'[a-f0-9]{64}', expected):
        raise ValueError('هش SHA-256 باید ۶۴ نویسه کوچک هگزادسیمال باشد')
    with Path(path).open('rb') as source:
        data = source.read(MAX_INPUT_BYTES + 1)
    if len(data) > MAX_INPUT_BYTES:
        raise ValueError('تصویر ورودی از سقف ۵۱۲ مگابایت بیشتر است')
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError('هش ورودی با تصویر بایتی مطابقت ندارد')
    return data


def _missing(value):
    return value is None or value is pd.NA or (isinstance(value, (float, np.floating)) and math.isnan(value))


def _check_dates(frame, columns, cutoff):
    """این ستون‌ها پیش از هر رقم مالی، از همان تصویر بایتی کنترل می‌شوند."""
    for column, optional in columns:
        for day in frame[column].unique():
            if optional and (day == '' if isinstance(day, str) else _missing(day)):
                continue
            if not is_canonical_iso_date(day) or day >= cutoff:
                raise ValueError('تاریخ نامعتبر یا در دوره مهروموم است')


def _identities(frame):
    if not frame.symbol.map(lambda x: isinstance(x, str) and bool(x.strip())).all():
        raise ValueError('نماد خالی یا نامعتبر است')
    result = frame.copy()
    result['symbol'] = result.symbol.map(normalize_symbol)
    if not result.symbol.map(bool).all():
        raise ValueError('نماد پس از یکسان‌سازی خالی است')
    if result.duplicated(['date', 'symbol']).any():
        raise ValueError('نماد/تاریخ پس از یکسان‌سازی تکراری است')
    return result


def _numeric(series):
    """رشته و بولی عدد نیستند؛ مقدار نامعلوم با صفر جایگزین نمی‌شود."""
    if pd.api.types.is_numeric_dtype(series.dtype) and not pd.api.types.is_bool_dtype(series.dtype):
        values = pd.to_numeric(series, errors='coerce').astype(float)
    else:
        def checked(value):
            if not isinstance(value, Real) or isinstance(value, (bool, np.bool_)):
                return np.nan
            try:
                return float(value)
            except (OverflowError, ValueError):
                return np.nan
        values = series.map(checked).astype(float)
    return values.where(np.isfinite(values))


def audit_frames(labels, prices, *, horizon, cutoff=None):
    """شمار توصیفی را نگه می‌دارد؛ علت واقعی توقف یا پرشدن سفارش را تشخیص نمی‌دهد."""
    if type(horizon) is not int or horizon < 1:
        raise ValueError('افق باید عدد صحیح و مثبت باشد')
    cutoff = _checked_cutoff(cutoff)
    status = f'label_status_{horizon}d'
    required_labels = {'date', 'symbol', 'entry_date', 'entry_executable', status}
    if not required_labels <= set(labels) or not {'date', 'symbol', *PRICE_FIELDS} <= set(prices):
        raise ValueError('ستون‌های هویت، وضعیت یا کندل لازم موجود نیستند')
    exit_col = f'exit_date_{horizon}d'
    columns = [('date', False), ('entry_date', True)] + ([(exit_col, True)] if exit_col in labels else [])
    _check_dates(labels, columns, cutoff)
    _check_dates(prices, [('date', False)], cutoff)
    labels, prices = _identities(labels), _identities(prices)
    if not labels[status].isin(['complete', 'entry_blocked', 'invalid_data', 'censored']).all():
        raise ValueError('وضعیت برچسب ناشناخته است')
    if not labels.entry_executable.map(lambda value: isinstance(value, (bool, np.bool_))).all():
        raise ValueError('پرچم ورود برچسب باید بولی باشد')
    blocked = labels.loc[labels[status].eq('entry_blocked'), ['date', 'symbol', 'entry_date', 'entry_executable']].copy()
    if (blocked.entry_executable.any() or blocked.entry_date.map(lambda d: not is_canonical_iso_date(d)).any()
            or blocked.entry_date.le(blocked.date).any()):
        raise ValueError('برچسب عدم ورود با تاریخ یا پرچم اجرا ناسازگار است')
    bars = prices[['date', 'symbol', *PRICE_FIELDS]].rename(columns={'date': 'entry_date'})
    data = blocked.merge(bars, how='left', on=['entry_date', 'symbol'], validate='many_to_one', indicator=True)
    if len(data) != len(blocked):
        raise ValueError('مخرج برچسب‌ها در اتصال به کندل تغییر کرد')
    numbers = pd.DataFrame({field: _numeric(data[field]) for field in PRICE_FIELDS}, index=data.index)
    found = data['_merge'].eq('both')
    zero = found & numbers.volume.eq(0) & numbers.value.eq(0)
    vwap = daily_vwap(numbers)
    coherent = (found & numbers.ref_price_raw.gt(0) & numbers.close_final_raw.gt(0)
                & numbers.close_raw.between(numbers.low_raw, numbers.high_raw)
                & np.isfinite(vwap))
    proxy = coherent & lock_flags(numbers).locked_up
    unlocked = coherent & ~proxy
    invalid = found & ~zero & ~coherent
    masks = (~found, zero, proxy, unlocked, invalid)
    if not sum(mask.astype(int) for mask in masks).eq(1).all():
        raise ValueError('دسته‌های شواهد باید بدون هم‌پوشانی و کامل باشند')
    counts = {name: int(mask.sum()) for name, mask in zip(CATEGORIES, masks, strict=True)}
    move = numbers.high_raw / numbers.ref_price_raw - 1
    bins = {'below_2_percent': proxy & move.lt(.02),
            '2_to_below_3_percent': proxy & move.ge(.02) & move.lt(.03),
            '3_to_below_5_percent': proxy & move.ge(.03) & move.lt(.05),
            'at_least_5_percent': proxy & move.ge(.05)}
    by_year = []
    years = data.date.str[:4]
    for year in sorted(years.unique()):
        within = years.eq(year)
        by_year.append(dict(decision_year=year, blocked_labels=int(within.sum()),
                            categories={name: int((mask & within).sum())
                                        for name, mask in zip(CATEGORIES, masks, strict=True)}))
    n = len(data)
    return dict(version=VERSION, horizon=horizon, total_label_rows=len(labels), total_price_rows=len(prices),
                blocked_labels=n, categories=counts,
                missing_entry_row_fraction=counts['absent_entry_price_row'] / n if n else None,
                locked_proxy_move_bins={name: int(mask.sum()) for name, mask in bins.items()}, by_year=by_year,
                locked_proxy_minimum_move=settings.BACKTEST_CONFIG['lock_min_move_pct'],
                entry_rows_and_labels_same_generation_verified=False, historical_universe_complete_verified=False,
                historical_price_band_verified=False, actual_queue_position_verified=False,
                actual_order_fill_verified=False, real_trading_performance_measured=False,
                accepted_for_signals=False, holdout_eligible=False,
                limitations=['Absent entry row is not proof of a halt or actual nonexecution.',
                    'A zero-activity row is not independent proof of an unfillable hypothetical order.',
                    'Daily locked-up flag is a single-price/minimum-move proxy, not a verified exchange price band or queue.',
                    'An actual no-session fill and the sign of its hypothetical profit are unknown.',
                    'Counts describe blocked labels only, not portfolio PnL or the entire historical market.',
                    'Hashes authenticate bytes against supplied values, not external provenance or completeness.'])


def audit_snapshots(labels_data, prices_data, *, horizon, cutoff):
    cutoff = _checked_cutoff(cutoff)
    if type(horizon) is not int or horizon < 1:
        raise ValueError('افق باید عدد صحیح و مثبت باشد')
    labels_file, prices_file = io.BytesIO(labels_data), io.BytesIO(prices_data)
    exit_col = f'exit_date_{horizon}d'
    # هر دو تاریخ پیش از خواندن ارقام هر یک از دو فایل کنترل می‌شوند.
    label_dates = pd.read_parquet(labels_file, columns=['date', 'entry_date', exit_col])
    price_dates = pd.read_parquet(prices_file, columns=['date'])
    _check_dates(label_dates, [('date', False), ('entry_date', True), (exit_col, True)], cutoff)
    _check_dates(price_dates, [('date', False)], cutoff)
    labels_file.seek(0)
    prices_file.seek(0)
    labels = pd.read_parquet(labels_file, columns=['date', 'symbol', 'entry_date', 'entry_executable',
                                                    f'label_status_{horizon}d'])
    prices = pd.read_parquet(prices_file, columns=['date', 'symbol', *PRICE_FIELDS])
    report = audit_frames(labels, prices, horizon=horizon, cutoff=cutoff)
    report.update(cutoff_exclusive=cutoff, labels_sha256=hashlib.sha256(labels_data).hexdigest(),
                  prices_sha256=hashlib.sha256(prices_data).hexdigest())
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--labels', required=True, help='Parquet برچسب‌های پژوهش پایه')
    parser.add_argument('--labels-sha256', required=True)
    parser.add_argument('--prices', required=True, help='Parquet قیمت خام همان منبع پژوهش')
    parser.add_argument('--prices-sha256', required=True)
    parser.add_argument('--horizon', required=True, type=int)
    parser.add_argument('--before', default=None)
    args = parser.parse_args(argv)
    try:
        label_bytes = _snapshot(args.labels, args.labels_sha256)
        price_bytes = _snapshot(args.prices, args.prices_sha256)
        report = audit_snapshots(label_bytes, price_bytes, horizon=args.horizon, cutoff=args.before)
        dependencies = ['core/market_rules.py', 'core/text.py', 'core/timeutils.py',
                        'config/settings.py', 'ops/report_files.py', 'requirements.lock.txt']
        report.update(created_at=now_tehran_iso(), code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                      dependency_sha256={name: hashlib.sha256((settings.BASE_DIR / name).read_bytes()).hexdigest()
                                         for name in dependencies})
        target = settings.DATA_DIR / 'private' / 'entry-availability-audit' / uuid.uuid4().hex / 'report.json'
        target = private_report_path(target, forbidden_paths=(args.labels, args.prices, settings.DB_PATH))
        write_private_report(target, report, forbidden_paths=(args.labels, args.prices, settings.DB_PATH))
    except (ValueError, OSError, TypeError, KeyError) as exc:
        parser.exit(2, str(exc) + '\n')
    print(json.dumps(dict(report=str(target), blocked_labels=report['blocked_labels'],
                          categories=report['categories'], accepted_for_signals=False), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
