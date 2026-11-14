from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from research.aban_rotation import rank_scores, score_inputs, targets


def _market():
    start = date(2024, 1, 1)
    days = [(start+timedelta(days=i)).isoformat() for i in range(80)]
    codes = ('3', '9')
    records = [dict(date=day, ins_code=code, close_final_raw=1000+i+(int(code)-3)*(i%3),
                    ref_price_raw=1000, volume=1, trade_count=1)
               for i, day in enumerate(days) for code in codes]
    return pd.DataFrame(records), days, codes


def test_scores_use_only_observations_on_or_before_each_session():
    market, days, codes = _market()
    original = score_inputs(market, days, codes)
    market.loc[market.date.eq(days[63]), 'close_final_raw'] = 2000
    changed = score_inputs(market, days, codes)
    for a, b in zip(original, changed, strict=True):
        pd.testing.assert_frame_equal(a.iloc[:63], b.iloc[:63])
    assert original[0].iloc[15].isna().all()
    assert original[0].iloc[16].notna().all()
    assert original[1].iloc[48].isna().all()
    assert original[1].iloc[49].notna().all()
    assert not np.array_equal(rank_scores(original)[63:], rank_scores(changed)[63:])


def test_no_trade_or_missing_momentum_stays_ineligible():
    market, days, codes = _market()
    market.loc[market.date.eq(days[70]) & market.ins_code.eq('3'), 'trade_count'] = 0
    out = rank_scores(score_inputs(market, days, codes))
    assert np.isnan(out[70, 0])
    assert np.isfinite(out[70, 1])


def test_cadence_first_target_last_close_and_stable_ties():
    scores = np.full((43, 2), .5)
    scores[21, 0] = np.nan
    first = targets(scores, exposure=.25)
    final = targets(scores, include_latest=True)
    np.testing.assert_array_equal(first[0], [.25, 0.])
    np.testing.assert_array_equal(first[21], [0., .25])
    assert np.isnan(first[42]).all()
    np.testing.assert_array_equal(final[42], [1., 0.])
    assert np.isnan(final[20]).all()


def test_duplicate_market_key_fails_without_creating_a_score():
    market, days, codes = _market()
    with pytest.raises(ValueError, match='Duplicate'):
        score_inputs(pd.concat([market, market.head(1)]), days, codes)
