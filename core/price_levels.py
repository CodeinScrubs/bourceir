"""Point-in-time price zones supported by confirmed reactions in raw daily bars."""

from dataclasses import dataclass
from math import ceil, floor

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from core.market_rules import corporate_action_factors
from core.price_quality import numeric_prices, price_quality
from core.timeutils import is_canonical_iso_date

VERSION = 'daily-price-zones-v1'
LOOKBACK = 180
RANGE_WINDOW = 20
MIN_HISTORY = 25
MAX_PER_SIDE = 3
SWING_WINDOWS = (2, 5)
MIN_EPISODE_SEPARATION = 5
MAX_CALENDAR_GAP = 21
MAX_MISSING_SESSIONS = 4


@dataclass(frozen=True)
class Swing:
    index: int
    confirmed_index: int
    price: int
    role: str
    broad: bool


@dataclass(frozen=True)
class PriceZone:
    side: str
    lower_rial: int
    upper_rial: int
    distance_pct: float
    episodes: int
    same_role_episodes: int | None
    broad_episodes: int
    last_reaction: str
    last_confirmation: str


@dataclass(frozen=True)
class PriceMap:
    state: str
    date: str | None = None
    anchor_rial: int | None = None
    window_start: str | None = None
    bars: int = 0
    typical_range_rial: float | None = None
    recent_low_rial: int | None = None
    recent_high_rial: int | None = None
    boundary: str | None = None
    skipped_sessions: int = 0
    zones: tuple[PriceZone, ...] = ()


def prepare_level_history(prices: pd.DataFrame, market_sessions=()) -> pd.DataFrame:
    """Keep unusable rows as barriers. A missing daily bar must never become a pivot."""
    if prices.empty:
        return pd.DataFrame()
    if ('date' not in prices or not prices.date.map(is_canonical_iso_date).all()
            or prices.date.duplicated().any()
            or ('symbol' in prices and prices.symbol.nunique(dropna=False) != 1)):
        raise ValueError('تاریخچهٔ یکتای یک نماد لازم است')
    ordered = prices.sort_values('date').reset_index(drop=True)
    raw, problems = price_quality(ordered)
    count = numeric_prices(ordered, ('trade_count',)).trade_count
    traded = ('open_raw', 'high_raw', 'low_raw', 'close_raw')
    whole_rials = raw[list(traded)].lt(2**53).all(axis=1) & raw[list(traded)].mod(1).eq(0).all(axis=1)
    valid = ~problems.any(axis=1) & count.gt(0) & count.mod(1).eq(0) & whole_rials
    if 'source' in ordered:
        valid &= ordered.source.eq('tsetmc').fillna(False)
    if 'price_unit' in ordered:
        valid &= ordered.price_unit.eq('Rial').fillna(False)
    valid = valid.astype(bool)
    day_gap = pd.to_datetime(ordered.date).diff().dt.days
    sessions = np.array(sorted({day for day in market_sessions if is_canonical_iso_date(day)}), dtype='U10')
    dates = ordered.date.to_numpy(dtype='U10')
    skipped = np.zeros(len(dates), dtype=int)
    skipped[1:] = np.maximum(0, np.searchsorted(sessions, dates[1:]) -
                            np.searchsorted(sessions, dates[:-1], side='right'))
    basis_change = corporate_action_factors(raw).ne(1)
    boundary = np.select(
        [~valid.shift(fill_value=True), basis_change,
         day_gap.gt(MAX_CALENDAR_GAP) | (skipped > MAX_MISSING_SESSIONS)],
        ['invalid_previous', 'reference_change', 'long_gap'],
        default='',
    )
    true_range = np.maximum.reduce([
        raw.high_raw - raw.low_raw,
        (raw.high_raw - raw.ref_price_raw).abs(),
        (raw.low_raw - raw.ref_price_raw).abs(),
    ])
    return raw.assign(date=dates, valid=valid, boundary=boundary, skipped=skipped, true_range=true_range)


def _confirmed_swings(window: pd.DataFrame, offset: int) -> tuple[Swing, ...]:
    ranges = window.true_range.rolling(RANGE_WINDOW, min_periods=5).median().to_numpy()
    close = window.close_raw.to_numpy()
    holes = window.skipped.to_numpy()[1:] > 0
    swings = {}
    for span in SWING_WINDOWS:
        scales = ranges[span:-span]
        uninterrupted = ~sliding_window_view(holes, 2 * span).any(axis=1)
        for role, prices, direction in (
            ('resistance', window.high_raw.to_numpy(), 1),
            ('support', window.low_raw.to_numpy(), -1),
        ):
            extremes = sliding_window_view(direction * prices, 2 * span + 1)
            closings = sliding_window_view(direction * close, 2 * span + 1)
            middle = extremes[:, span]
            # On a flat top or bottom, accept only the last equal extreme.
            extreme = (middle >= extremes[:, :span].max(axis=1)) & (middle > extremes[:, span+1:].max(axis=1))
            away = middle - np.maximum(closings[:, :span].min(axis=1), closings[:, span+1:].min(axis=1))
            for hit in np.flatnonzero(uninterrupted & extreme & (scales > 0) & (away >= scales / 2)):
                local_index = int(hit + span)
                index = offset + local_index
                swings[index, role] = Swing(
                    index, index + span, int(prices[local_index]), role, span == SWING_WINDOWS[-1],
                )
    return tuple(sorted(swings.values(), key=lambda item: (item.price, item.index, item.role)))


def _groups(swings, spread):
    groups = []
    for swing in swings:
        if not groups or swing.price - groups[-1][0].price > spread:
            groups.append([])
        groups[-1].append(swing)
    return groups


def _make_zones(swings, dates, anchor, typical_range):
    # Limit the entire cluster width, so consecutive nearby points cannot form an arbitrarily wide zone.
    groups = _groups(swings, max(1., .6 * typical_range))
    pad = max(1., .2 * typical_range)
    zones = []
    for number, group in enumerate(groups):
        lower = max(1, floor(group[0].price - pad))
        upper = ceil(group[-1].price + pad)
        if number:
            lower = max(lower, (groups[number-1][-1].price + group[0].price) // 2 + 1)
        if number + 1 < len(groups):
            upper = min(upper, (group[-1].price + groups[number+1][0].price) // 2)
        side = 'support' if upper < anchor else 'resistance' if lower > anchor else 'inside'
        distance = max(0, lower - anchor, anchor - upper) / anchor * 100
        chronologic = sorted(group, key=lambda item: (item.index, item.role))
        episodes = []
        for swing in chronologic:
            if not episodes or swing.index - episodes[-1][-1].index >= MIN_EPISODE_SEPARATION:
                episodes.append([])
            episodes[-1].append(swing)
        same_role = (None if side == 'inside' else
                     sum(any(swing.role == side for swing in episode) for episode in episodes))
        zones.append(PriceZone(
            side, lower, upper, distance, len(episodes), same_role,
            sum(any(swing.broad for swing in episode) for episode in episodes),
            dates[chronologic[-1].index], max(dates[swing.confirmed_index] for swing in chronologic),
        ))
    return zones


def price_map_at(history: pd.DataFrame, as_of: str) -> PriceMap:
    """Nearest zones at the last recorded session up to the explicit date, without lookahead."""
    if not is_canonical_iso_date(as_of):
        raise ValueError('تاریخ محاسبه باید YYYY-MM-DD باشد')
    if history.empty:
        return PriceMap('no_history')
    dates = history.date.to_numpy()
    end = int(np.searchsorted(dates, as_of, side='right'))
    if end == 0:
        return PriceMap('no_history')
    last = history.iloc[end-1]
    if not last.valid:
        return PriceMap('invalid_latest', date=last.date)
    # The newest invalid row or basis break limits the usable suffix. Older prices are never rescaled.
    boundaries = np.flatnonzero(history.boundary.iloc[:end].to_numpy() != '')
    epoch = int(boundaries[-1]) if len(boundaries) else 0
    start = max(epoch, end - LOOKBACK)
    window = history.iloc[start:end]
    inputs = dict(date=last.date, anchor_rial=int(last.close_raw), window_start=dates[start],
                  bars=len(window), boundary=history.boundary.iloc[start] or None,
                  skipped_sessions=int(window.skipped.iloc[1:].sum()))
    if len(window) < MIN_HISTORY:
        return PriceMap('short_history', **inputs)
    recent = window.tail(RANGE_WINDOW)
    typical = float(recent.true_range.median())
    if typical <= 0:
        return PriceMap('flat_history', **inputs)
    swings = _confirmed_swings(window, start)
    zones = _make_zones(swings, dates, inputs['anchor_rial'], typical)
    nearest = tuple(
        zone
        for side in ('support', 'inside', 'resistance')
        for zone in sorted((zone for zone in zones if zone.side == side), key=lambda item: item.distance_pct)[:MAX_PER_SIDE]
    )
    return PriceMap('ready', typical_range_rial=typical, recent_low_rial=int(recent.low_raw.min()),
                    recent_high_rial=int(recent.high_raw.max()), zones=nearest, **inputs)
