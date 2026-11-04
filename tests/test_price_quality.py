"""Unknown boolean values cannot acquire a plausible price in shared data checks."""

import numpy as np
import pandas as pd
import pytest

from core.price_quality import json_price_row, numeric_prices, price_quality


@pytest.mark.parametrize('value',[True,np.bool_(True),False])
def test_boolean_raw_value_is_unknown_in_quality_flags_and_serialization(value):
    row=dict(date='2024-01-01',open_raw=100.,high_raw=101.,low_raw=99.,close_raw=100.,
             close_final_raw=100.,ref_price_raw=value,volume=1000.,value=100000.)
    numeric,flags=price_quality(pd.DataFrame([row]))
    assert pd.isna(numeric.ref_price_raw.iloc[0])
    assert flags.missing_or_nonfinite_ref_price_raw.iloc[0]
    assert json_price_row(row)['ref_price_raw'] is None


def test_nullable_boolean_and_unknown_values_remain_float_nan():
    frame=pd.DataFrame({'ref_price_raw':pd.Series([True,pd.NA],dtype='boolean')})
    numeric,flags=price_quality(frame)
    assert numeric.ref_price_raw.isna().all()
    assert flags.missing_or_nonfinite_ref_price_raw.all()


def test_ordinary_numbers_and_numeric_strings_are_preserved():
    row=dict(date='2024-01-01',open_raw=100.,high_raw=101.,low_raw=99.,close_raw=100.,
             close_final_raw='100',ref_price_raw=100,volume=1000.,value=100000.)
    numeric,flags=price_quality(pd.DataFrame([row]))
    assert not flags.iloc[0].any()
    assert numeric.close_final_raw.iloc[0]==100.
    assert json_price_row(row)['close_final_raw']==100.


def test_sanitizing_unknown_values_does_not_mutate_the_input_snapshot():
    frame=pd.DataFrame({'ref_price_raw':[100.,np.inf,np.nan]})
    before=frame.copy(deep=True)
    clean=numeric_prices(frame)
    assert clean.ref_price_raw.iloc[0]==100.
    assert clean.ref_price_raw.iloc[1:].isna().all()
    pd.testing.assert_frame_equal(frame,before)
