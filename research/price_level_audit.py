"""Evaluate daily zone reactions before the research holdout, against simple rolling extrema."""

import argparse
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import date, timedelta

import numpy as np

from config import settings
from core.price_levels import VERSION, prepare_level_history, price_map_at
from core.timeutils import is_canonical_iso_date, now_tehran_iso
from database.db import DatabaseManager
from database.symbol_review import observed_price_sessions
from ops.report_files import write_private_report

TOUCH_SESSIONS = 5
FOLLOW_SESSIONS = 3
HORIZON = TOUCH_SESSIONS + FOLLOW_SESSIONS
TOUCHED = ('bounce', 'break', 'unresolved')
OUTCOMES = ('unreached', 'gap_through', *TOUCHED, 'unknown', 'unavailable')


def observed_reaction(bars, side, lower, upper, typical_range):
    """After a dated range touch, use the order of daily last trades only."""
    contact = None
    for step, bar in enumerate(bars):
        if contact is None and step == TOUCH_SESSIONS:
            return 'unreached'
        if bar is None or not bar.valid or bar.boundary:
            return 'unknown'
        if contact is None:
            past_zone = bar.open_raw < lower if side == 'support' else bar.open_raw > upper
            if past_zone:
                return 'gap_through'
            if not (bar.low_raw <= upper and bar.high_raw >= lower):
                continue
            contact = step
        if side == 'support':
            broken, bounced = bar.close_raw < lower, bar.close_raw >= upper + typical_range
        else:
            broken, bounced = bar.close_raw > upper, bar.close_raw <= lower - typical_range
        if broken:
            return 'break'
        if bounced:
            return 'bounce'
        if step - contact == FOLLOW_SESSIONS:
            return 'unresolved'
    return 'unreached' if contact is None and len(bars) >= TOUCH_SESSIONS else 'unknown'


def _rolling_extreme(levels, side, width):
    center = levels.recent_low_rial if side == 'support' else levels.recent_high_rial
    lower = center - width // 2
    upper = lower + width
    valid = lower > 0 and (upper < levels.anchor_rial if side == 'support' else lower > levels.anchor_rial)
    return (lower, upper) if valid else None


def _rates(counts):
    observable = sum(counts[k] for k in OUTCOMES if k not in {'unknown', 'unavailable'})
    touched = sum(counts[k] for k in TOUCHED)
    return dict(counts={key: counts[key] for key in OUTCOMES},
                observable=observable, touched=touched,
                bounce_per_observable=counts['bounce'] / observable if observable else None,
                bounce_per_touch=counts['bounce'] / touched if touched else None)


def _paired_difference(by_date):
    """Moving blocks of three origin dates retain the cross-section and short time dependence."""
    if not by_date:
        return dict(pairs=0, difference=None, descriptive_interval=None, origin_dates=0)
    daily = np.array([value for _, value in sorted(by_date.items())])
    total = daily[:, 1].sum()
    point = float(daily[:, 0].sum() / total)
    interval = None
    if len(daily) >= 12:
        rng = np.random.default_rng(20261107)
        block = 3
        starts = rng.integers(0, len(daily)-block+1, size=(4000, (len(daily)+block-1)//block))
        indices = (starts[..., None] + np.arange(block)).reshape(4000, -1)[:, :len(daily)]
        sums = daily[indices].sum(axis=1)
        interval = np.quantile(sums[:, 0] / sums[:, 1], [.025, .975]).tolist()
    return dict(pairs=int(total), difference=point, descriptive_interval=interval, origin_dates=len(daily))


def run_audit(db, start: str, before: str) -> dict:
    if (not all(map(is_canonical_iso_date, (start, before)))
            or not settings.RESEARCH_CONFIG['dev_start'] <= start < before <= settings.RESEARCH_CONFIG['holdout_start']):
        raise ValueError('بازهٔ ممیزی باید کاملاً پیش از دورهٔ مهر و موم باشد')
    warm_start = (date.fromisoformat(start) - timedelta(days=730)).isoformat()
    last_date = (date.fromisoformat(before) - timedelta(days=1)).isoformat()
    market = observed_price_sessions(db, warm_start, last_date)
    first = bisect_left(market, start)
    indices = range(first, len(market)-HORIZON, HORIZON)
    universe = db.get_universe_symbols(include_inactive=True)
    states = Counter()
    zones = defaultdict(Counter)
    paired = defaultdict(Counter)
    comparisons = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    repeated = defaultdict(Counter)
    for security in universe:
        prices = db.get_daily_prices_df(security['symbol_fa'], start_date=warm_start, end_date=last_date,
                                       include_price_unit=True)
        history = prepare_level_history(prices, market)
        records = {bar.date: bar for bar in history.itertuples(index=False)}
        for index in indices:
            origin = market[index]
            if origin not in records:
                states['no_origin_bar'] += 1
                continue
            levels = price_map_at(history, origin)
            states[levels.state] += 1
            if levels.state != 'ready':
                continue
            future = [records.get(day) for day in market[index+1:index+1+HORIZON]]
            for side in ('support', 'resistance'):
                nearest = next((zone for zone in levels.zones if zone.side == side), None)
                if nearest is None:
                    zones[side]['unavailable'] += 1
                    continue
                outcome = observed_reaction(future, side, nearest.lower_rial, nearest.upper_rial,
                                            levels.typical_range_rial)
                zones[side][outcome] += 1
                evidence = min(nearest.same_role_episodes, 2)
                repeated[side, evidence][outcome] += 1
                baseline = _rolling_extreme(levels, side, nearest.upper_rial-nearest.lower_rial)
                comparison = ('unavailable' if baseline is None else
                              observed_reaction(future, side, *baseline, levels.typical_range_rial))
                paired[side][outcome, comparison] += 1
                if outcome in TOUCHED and comparison in TOUCHED:
                    delta = comparisons[side][origin]
                    delta[0] += int(outcome == 'bounce') - int(comparison == 'bounce')
                    delta[1] += 1
    results = {}
    for side in ('support', 'resistance'):
        jointly_observed_zone, jointly_observed_control = Counter(), Counter()
        paired_unknown = 0
        for (outcome, comparison), count in paired[side].items():
            if outcome in {'unknown', 'unavailable'} or comparison in {'unknown', 'unavailable'}:
                paired_unknown += count
            else:
                jointly_observed_zone[outcome] += count
                jointly_observed_control[comparison] += count
        results[side] = dict(
            all_zones=_rates(zones[side]),
            by_same_role_reactions={str(n): _rates(repeated[side, n]) for n in range(3)},
            comparable_zones=_rates(jointly_observed_zone),
            comparable_rolling_extreme=_rates(jointly_observed_control),
            noncomparable_pairs=paired_unknown,
            paired_touch_bounce_difference=_paired_difference(comparisons[side]),
        )
    return dict(version=VERSION, generated_at=now_tehran_iso(), start=start, before=before, symbols=len(universe),
                origin_dates=[market[index] for index in indices], states=dict(states),
                touch_sessions=TOUCH_SESSIONS, follow_sessions=FOLLOW_SESSIONS, results=results)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', default='2023-03-21')
    parser.add_argument('--before', default=settings.RESEARCH_CONFIG['holdout_start'])
    parser.add_argument('--json', default=str(settings.DATA_DIR / 'private' / 'price-level-audit.json'))
    args = parser.parse_args(argv)
    with DatabaseManager(read_only=True) as db:
        db.conn.execute('BEGIN')
        report = run_audit(db, args.start, args.before)
    print(write_private_report(args.json, report, forbidden_paths=[settings.DB_PATH]))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
