"""Finite owner-cohort research rules; never a live signal interface.

Scores use raw, dated reference returns on the supplied market calendar. A
missing session stays missing instead of compressing a 60-session window.
The fixed family tests turnover/holding time, not a fitted probability model.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from core.market_rules import lock_flags, pit_daily_returns
from core.timeutils import is_canonical_iso_date
from research.baselines import pick_top
from research.dip import DipRule

VERSION = 'owner-opportunity-fixed-v1'


@dataclass(frozen=True)
class OpportunityRule:
    name: str
    selector: str
    horizon: int
    dip_rule: DipRule | None = None


def fixed_rules():
    """Twelve fixed trading rules, two active controls and two capital controls."""
    rules = [OpportunityRule(f'{selector}:h{h}', selector, h)
             for selector in ('positive_momentum20', 'positive_momentum60', 'pullback_in_uptrend')
             for h in (10, 20, 40)]
    rules += [OpportunityRule(f'queue_dip2_tp4:h{h}', 'buy_queue', h,
                             DipRule(dip=.02, take_profit=.04, hold=h, conservative=True))
              for h in (1, 5, 10)]
    return rules + [OpportunityRule('liquidity:h20', 'liquidity', 20),
                    OpportunityRule('random97:h20', 'random97', 20),
                    OpportunityRule('cash', 'cash', 1), OpportunityRule('buy_hold', 'buy_hold', 1)]


def causal_scores(prices, calendar):
    """Return only decision-time fields; reject ambiguous identities/calendar."""
    days = list(calendar)
    if (not days or days != sorted(set(days))
            or any(not is_canonical_iso_date(day) for day in days)):
        raise ValueError('A canonical sorted unique session calendar is required')
    required = {'symbol', 'date', 'close_final_raw', 'ref_price_raw',
                'high_raw', 'low_raw', 'volume', 'value'}
    if not required <= set(prices) or prices.duplicated(['symbol', 'date']).any():
        raise ValueError('Unique raw daily decision rows are required')
    if not set(prices.date) <= set(days):
        raise ValueError('Observed price dates cannot be dropped from the calendar')
    if prices.symbol.isna().any() or prices.symbol.map(lambda x: not isinstance(x, str) or not x).any():
        raise ValueError('Stable nonempty instrument keys are required')
    output = []
    for symbol, original in prices.groupby('symbol', sort=True):
        frame = original.set_index('date').reindex(days)
        # This API uses official close / reference, never adjusted close or
        # vendor backward adjustment factors, even if such columns are supplied.
        ret = pit_daily_returns(frame)
        ret = ret.where(np.isfinite(ret) & ret.gt(-1))
        log = np.log1p(ret)
        momentum = {h: np.expm1(log.rolling(h, min_periods=h).sum()) for h in (5, 20, 60)}
        locked = lock_flags(frame).locked_up
        result = pd.DataFrame({'symbol': symbol, 'date': days,
            'value': frame.value.to_numpy(),
            'positive_momentum20': momentum[20].where(momentum[20].gt(0)).to_numpy(),
            'positive_momentum60': momentum[60].where(momentum[60].gt(0)).to_numpy(),
            'pullback_in_uptrend': (-momentum[5]).where(
                momentum[20].gt(0) & momentum[5].lt(0)).to_numpy(),
            'buy_queue': frame.value.where(locked).to_numpy()})
        output.append(result)
    columns = ['symbol', 'date', 'value', 'positive_momentum20', 'positive_momentum60',
               'pullback_in_uptrend', 'buy_queue']
    return pd.concat(output, ignore_index=True)[columns] if output else pd.DataFrame(columns=columns)


def decision_tape(scores, rule, days, *, min_value=20_000_000_000., positions=5):
    """Same tape in normal/stress; reserve time to exit before the terminal day."""
    import hashlib

    days = list(days)
    if days != sorted(set(days)) or len(days) < 3:
        raise ValueError('At least three sorted unique replay sessions are required')
    if (isinstance(min_value, bool) or not np.isfinite(min_value) or min_value < 0
            or type(positions) is not int or positions < 1):
        raise ValueError('Finite liquidity and a positive slot count are required')
    empty = pd.DataFrame(columns=['date', 'symbol', 'rank'])
    if rule.selector == 'cash':
        return empty, 1, positions
    if rule.selector == 'buy_hold':
        # Fixed cohort, not filtered using future liquidity or later survivors.
        symbols = sorted(scores.symbol.unique())
        tape = pd.DataFrame(dict(date=days[0], symbol=symbols, rank=range(len(symbols))))
        return tape, len(days)-2, len(symbols)
    panel = scores.loc[scores.date.isin(days[:max(0, len(days)-rule.horizon-1)])
                       & scores.value.ge(min_value)].reset_index(drop=True)
    if rule.selector == 'liquidity':
        score = panel.value
    elif rule.selector == 'random97':
        score = pd.Series([int.from_bytes(hashlib.sha256(f'97|{d}|{s}'.encode()).digest()[:8], 'big')
                           / 2**64 for d, s in panel[['date', 'symbol']].itertuples(index=False, name=None)],
                          index=panel.index, dtype=float)
    elif rule.selector in {'positive_momentum20', 'positive_momentum60', 'pullback_in_uptrend', 'buy_queue'}:
        score = panel[rule.selector]
    else:
        raise ValueError('Unsupported fixed selector')
    tape = pick_top(panel, score, positions)[['date', 'symbol']].copy()
    tape['rank'] = tape.groupby('date').cumcount()
    return tape.reset_index(drop=True), rule.horizon, positions
