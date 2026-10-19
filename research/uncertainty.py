"""بررسی زوجی منحنی روزانه با بلوک‌های ایستا؛ آزمون پذیرش مزیت معاملاتی نیست.

مرجع: Politis & Romano (1994)، بازنمونه‌گیری ایستا و مستندات arch:
https://bashtage.github.io/arch/bootstrap/timeseries-bootstraps.html
فاصله‌های percentile تقریبی‌اند و به فرض ایستایی/وابستگی مناسب نیاز دارند؛
انتخاب روش، تغییر رژیم بازار یا خطای مدل اجرا با آن‌ها اصلاح نمی‌شود.
"""

import argparse
import hashlib
import io
import json
from pathlib import Path

import numpy as np
import pandas as pd

from core.timeutils import is_canonical_iso_date
from ops.report_files import private_report_path, write_private_report

VERSION = 'paired-stationary-diagnostics-v4'
MAX_INPUT_BYTES = 64 * 1024 * 1024


def _read_snapshot(path):
    with Path(path).open('rb') as handle:
        contents = handle.read(MAX_INPUT_BYTES + 1)
    if len(contents) > MAX_INPUT_BYTES:
        raise ValueError('Diagnostic input exceeds the 64 MiB snapshot limit; use a smaller development fold')
    return contents


def _canonical_dates(dates):
    return bool(dates) and all(is_canonical_iso_date(d) for d in dates) and dates == sorted(set(dates))


def _load_development_curve(path, cutoff):
    """ابتدا تاریخ خوانده شود؛ ارزش فقط پس از کنترل مرز روی همان بایت‌ها باز می‌شود."""
    if not is_canonical_iso_date(cutoff):
        raise ValueError('Invalid sealed-period boundary')
    contents = _read_snapshot(path)
    buffer = io.BytesIO(contents)
    dates = pd.read_parquet(buffer, columns=['date']).date.tolist()
    if not _canonical_dates(dates):
        raise ValueError('Curve dates must be sorted, unique canonical ISO dates')
    if any(d >= cutoff for d in dates):
        raise ValueError('This development CLI cannot inspect sealed-period equity')
    buffer.seek(0)
    frame = pd.read_parquet(buffer, columns=['date', 'equity_rial'])
    frame.attrs['input_sha256'] = hashlib.sha256(contents).hexdigest()
    return frame


def _indices(rng, n, block_length):
    """شروع دوباره یکنواخت با احتمال ۱/L؛ در غیر این صورت ادامه با بازگشت از انتهای نمونه."""
    restarts = rng.random(n) < 1. / block_length
    restarts[0] = True
    positions = np.arange(n)
    last_restart = np.maximum.accumulate(np.where(restarts, positions, 0))
    starts = rng.integers(0, n, size=n)
    return (starts[last_restart] + positions - last_restart) % n


def compare_equity_curves(strategy, benchmark, *, initial_capital_rial, block_lengths=(5, 10, 20),
                          replications=5000, seed=41, confidence=.95, min_sessions=60, trials_attempted=None,
                          expected_sessions=None, family_comparisons=None):
    """منحنی خالص هم‌تاریخ؛ جفت نامعلوم حذف نمی‌شود و تولید فاصله را متوقف می‌کند.

    خروجی میانگین حسابی اختلاف بازده روزانه است؛ CAGR یا احتمال سود آینده نیست.
    حساب برش‌های مستقل جدا مقایسه شود. هزینه، جریان وجه، حقوق اقتصادی،
    هم‌ریسک‌بودن و دامنه باید جداگانه بررسی شوند.
    """
    if (isinstance(initial_capital_rial, bool) or not isinstance(initial_capital_rial, (int, float))
            or not np.isfinite(initial_capital_rial) or initial_capital_rial <= 0):
        raise ValueError('A finite positive common initial capital is required')
    if (isinstance(replications, bool) or not isinstance(replications, int) or not 1000 <= replications <= 100000
            or isinstance(min_sessions, bool) or not isinstance(min_sessions, int) or min_sessions < 3
            or isinstance(seed, bool) or not isinstance(seed, int) or seed < 0
            or isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not .5 < confidence < 1):
        raise ValueError('Invalid bootstrap size, confidence, minimum length or random seed')
    if trials_attempted is not None and (isinstance(trials_attempted, bool) or not isinstance(trials_attempted, int)
                                        or trials_attempted < 1):
        raise ValueError('Trial count must be positive or explicitly unknown')
    blocks = tuple(block_lengths)
    if not blocks or len(set(blocks)) != len(blocks) or any(isinstance(b, bool) or not isinstance(b, int) or b < 2 for b in blocks):
        raise ValueError('Distinct integer block lengths >= 2 are required')
    if family_comparisons is not None and (type(family_comparisons) is not int or family_comparisons < 1
            or trials_attempted is None or family_comparisons < trials_attempted * len(blocks)):
        raise ValueError('The declared family must cover all attempted configurations and reported block variants')
    minimum_family_p = None if family_comparisons is None else min(1., family_comparisons / (replications + 1))
    resolution_adequate = None if minimum_family_p is None else minimum_family_p <= 1. - confidence
    frames = []
    for frame in (strategy, benchmark):
        if not {'date', 'equity_rial'} <= set(frame) or frame.empty:
            raise ValueError('A nonempty daily equity curve with date and equity_rial is required')
        dates = frame.date.tolist()
        if not _canonical_dates(dates):
            raise ValueError('Curve dates must be sorted, unique ISO dates')
        if any(isinstance(v, (str, bool, np.bool_)) for v in frame.equity_rial):
            raise ValueError('Equity must be numeric, not text or boolean')
        values = pd.to_numeric(frame.equity_rial, errors='coerce').to_numpy(dtype=float, na_value=np.nan)
        if np.isinf(values).any() or (values < 0).any():
            raise ValueError('Negative or infinite equity is invalid')
        previous = np.r_[float(initial_capital_rial), values[:-1]]
        returns = np.full(len(values), np.nan)
        np.divide(values, previous, out=returns, where=np.isfinite(values) & np.isfinite(previous) & (previous > 0))
        frames.append(returns - 1.)
    if strategy.date.tolist() != benchmark.date.tolist():
        raise ValueError('Strategy and benchmark must retain exactly the same session dates')
    calendar = None if expected_sessions is None else list(expected_sessions)
    if calendar is not None and not _canonical_dates(calendar):
        raise ValueError('Expected sessions must be sorted, unique canonical ISO dates')
    calendar_matches = None if calendar is None else calendar == strategy.date.tolist()
    difference = frames[0] - frames[1]
    unknown = int((~np.isfinite(difference)).sum())
    blockers = ['selection_not_adjusted', 'stationarity_and_regime_stability_unproven',
                'risk_and_economic_accounting_not_verified_by_this_tool', 'execution_model_error_not_quantified',
                'fixed_sample_only_not_valid_for_optional_stopping']
    if calendar is None:
        blockers.append('session_calendar_completeness_unverified')
    else:
        blockers.append('calendar_source_not_verified_by_this_tool')
        if not calendar_matches:
            blockers.append('session_calendar_mismatch')
    if trials_attempted is None:
        blockers.append('number_of_attempted_trials_unknown')
    if resolution_adequate is False:
        blockers.append('bootstrap_resolution_cannot_reach_declared_family_alpha')
    if unknown:
        blockers.append('unknown_daily_pairs')
    if len(difference) < min_sessions:
        blockers.append('below_configured_computational_sample_floor')
    if any(b >= len(difference) for b in blocks):
        blockers.append('block_not_shorter_than_sample')
    complete = (not unknown and calendar_matches is not False
                and len(difference) >= min_sessions and all(b < len(difference) for b in blocks))
    if complete and np.ptp(difference) == 0:
        blockers.append('degenerate_daily_difference')
        complete = False
    intervals = []
    if complete:
        alpha = (1. - confidence) / 2.
        for block in blocks:
            rng = np.random.default_rng(np.random.SeedSequence([seed, block]))
            means = np.empty(replications)
            for iteration in range(replications):
                means[iteration] = difference[_indices(rng, len(difference), block)].mean()
            lower, upper = np.quantile(means, [alpha, 1.-alpha])
            # H0: میانگین اختلاف <= صفر. بازنمونه مرکززدایی‌شده، آزمون یک‌طرفه تقریبی است.
            observed = float(difference.mean())
            null_means = means - observed
            p_value = (1 + np.count_nonzero(null_means >= observed)) / (replications + 1)
            intervals.append(dict(expected_block_sessions=block, mean_daily_difference_lower=float(lower),
                                  mean_daily_difference_upper=float(upper), confidence=confidence,
                                  one_sided_p_value=float(p_value),
                                  declared_family_bonferroni_p_value=None if family_comparisons is None else
                                      min(1., float(p_value) * family_comparisons),
                                  p_value_resolution=1 / (replications + 1),
                                  family_tail_resolution_adequate=resolution_adequate))
    return dict(version=VERSION, sessions=len(difference), unknown_daily_pairs=unknown,
                known_daily_pairs=len(difference)-unknown, min_sessions=min_sessions,
                mean_daily_return_difference=float(difference.mean()) if not unknown and calendar_matches is not False else None,
                calendar_supplied=calendar is not None, calendar_matches_curve=calendar_matches,
                missing_expected_sessions=None if calendar is None else len(set(calendar) - set(strategy.date)),
                unexpected_sessions=None if calendar is None else len(set(strategy.date) - set(calendar)),
                intervals=intervals, replications=replications, seed=seed, trials_attempted=trials_attempted,
                family_comparisons=family_comparisons, historical_trial_count_verified=False,
                minimum_attainable_family_p_value=minimum_family_p,
                family_tail_resolution_adequate=resolution_adequate,
                p_value_method='one-sided mean <= 0; centered stationary bootstrap; plus-one Monte Carlo count',
                blockers=blockers, selection_corrected=False, accepted_for_signals=False,
                probability_of_future_profit=None, statistic='arithmetic mean of paired daily net return differences',
                interval_method='stationary-block percentile; individual comparison, not family-wise adjusted',
                sample_floor_is_not_statistical_sufficiency=True)


def main(argv=None):
    from config import settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--strategy', required=True)
    parser.add_argument('--benchmark', required=True)
    parser.add_argument('--initial-capital-rial', type=float, required=True)
    parser.add_argument('--blocks', nargs='+', type=int, default=[5, 10, 20])
    parser.add_argument('--replications', type=int, default=5000)
    parser.add_argument('--seed', type=int, default=41)
    parser.add_argument('--trials-attempted', type=int, default=None)
    parser.add_argument('--family-comparisons', type=int, default=None)
    parser.add_argument('--calendar', default=None, help='JSON array of expected development sessions for this fold')
    parser.add_argument('--output', default='data/private/uncertainty.json')
    args = parser.parse_args(argv)
    inputs = [args.strategy, args.benchmark] + ([args.calendar] if args.calendar else [])
    try:
        output = private_report_path(args.output, forbidden_paths=inputs + [settings.DB_PATH])
        calendar = None
        if args.calendar:
            calendar = json.loads(_read_snapshot(args.calendar))
            if (not isinstance(calendar, list) or not _canonical_dates(calendar)
                    or any(d >= settings.RESEARCH_CONFIG['holdout_start'] for d in calendar)):
                raise ValueError('Calendar must be a sorted, unique JSON array of development dates')
        curves = [_load_development_curve(name, settings.RESEARCH_CONFIG['holdout_start'])
                  for name in (args.strategy, args.benchmark)]
        report = compare_equity_curves(*curves, initial_capital_rial=args.initial_capital_rial,
                                      block_lengths=args.blocks, replications=args.replications, seed=args.seed,
                                      trials_attempted=args.trials_attempted, expected_sessions=calendar,
                                      family_comparisons=args.family_comparisons)
        report['input_sha256'] = dict(strategy=curves[0].attrs['input_sha256'], benchmark=curves[1].attrs['input_sha256'])
        if args.calendar:
            # هویت تقویم، محتوای تجزیه‌شده‌ای است که واقعاً در محاسبه بالا مصرف شد.
            report['input_sha256']['calendar'] = hashlib.sha256(json.dumps(calendar).encode()).hexdigest()
        write_private_report(output, report, forbidden_paths=inputs + [settings.DB_PATH])
    except (ValueError, OSError, TypeError) as exc:
        parser.exit(2, str(exc) + '\n')
    print(json.dumps(dict(sessions=report['sessions'], unknown_daily_pairs=report['unknown_daily_pairs'],
                          intervals=report['intervals'], accepted_for_signals=False)))
    return 0 if report['intervals'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
