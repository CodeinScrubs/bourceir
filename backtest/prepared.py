"""Immutable in-memory price preparation shared by deterministic portfolio replays."""

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

import numpy as np
import pandas as pd

from core import market_rules
from core.price_quality import RAW_PRICES, numeric_prices
from core.timeutils import is_canonical_iso_date

PRICE_SNAPSHOT_VERSION = 'portfolio-price-snapshot-v1'
REQUIRED = {'symbol','date','low_raw','high_raw','close_raw','close_final_raw','ref_price_raw','volume','value'}


@dataclass(frozen=True)
class PreparedPortfolioPrices:
    sessions: Mapping
    row_count: int
    has_open: bool
    frame_fingerprint: str
    version: str = PRICE_SNAPSHOT_VERSION


def prepare_portfolio_prices(prices):
    """Copy raw prices once, keep scalar financial cells, and freeze every mapping.

    The fingerprint identifies normalized frame values under this pandas version;
    it is not source authenticity or historical-availability evidence. Callers
    still freeze and verify their original input bytes separately. No global or
    disk cache is used, and changing the caller's frame cannot alter this view.
    """
    if (not isinstance(prices,pd.DataFrame) or prices.columns.duplicated().any()
            or not REQUIRED<=set(prices) or prices.duplicated(['symbol','date']).any()):
        raise ValueError('Unique symbol/date rows and raw price columns are required')
    if any(not is_canonical_iso_date(day) for day in prices.date.unique()):
        raise ValueError('تاریخ قیمت آماده باید میلادی استاندارد و معلوم باشد')
    fields = [f for f in RAW_PRICES if f in prices]
    has_open = 'open_raw' in prices
    frame = prices[['symbol','date',*fields]].sort_values(['symbol','date']).copy()
    # Never let booleans become one-rial prices, references, quantities or value.
    frame[fields] = numeric_prices(frame,fields)
    digest = hashlib.sha256(json.dumps(dict(version=PRICE_SNAPSHOT_VERSION,pandas=pd.__version__,
        fields=['symbol','date',*fields]),sort_keys=True).encode())
    digest.update(pd.util.hash_pandas_object(frame,index=False).to_numpy(dtype='uint64').tobytes())
    frame['vwap'] = market_rules.daily_vwap(frame)
    frame['mark'] = market_rules.official_close(frame)
    frame['previous_official'] = frame.groupby('symbol')['mark'].shift(1)
    frame['previous_date'] = frame.groupby('symbol')['date'].shift(1)
    frame['action'] = market_rules.corporate_action_factors(frame)
    locks = market_rules.lock_flags(frame)
    frame['up'],frame['down'] = locks.locked_up,locks.locked_down
    frame['mark_valid'] = (np.isfinite(frame[['ref_price_raw','mark']]).all(axis=1)
        & frame.ref_price_raw.gt(0) & frame.mark.gt(0))
    # Missing/incoherent traded totals cannot supply an execution VWAP. They do
    # not erase an independently observed official mark on a non-execution day.
    frame['valid'] = frame.mark_valid & np.isfinite(frame.vwap)
    sessions = {}
    for day,group in frame.groupby('date',sort=False):
        rows = group.set_index('symbol').to_dict('index')
        sessions[day] = MappingProxyType({symbol:MappingProxyType(row) for symbol,row in rows.items()})
    return PreparedPortfolioPrices(MappingProxyType(sessions),len(frame),has_open,digest.hexdigest())
