"""Explicit legacy CSV semantics; test bytes only, no actual owner input."""
import numpy as np
import pytest

from core.legacy_price_csv import inspect_legacy_price_csv

HEADER = '<TICKER>,<DTYYYYMMDD>,<FIRST>,<HIGH>,<LOW>,<CLOSE>,<VALUE>,<VOL>,<OPEN>,<LAST>\n'


def row(day='20240102', **changes):
    fields = dict(ticker='TEST', date=day, first=101, high=103, low=100, final=102,
                  value=1020, volume=10, source_open=98, last=103) | changes
    return ','.join(str(x) for x in fields.values())+'\n'


def inspect(rows=None, **changes):
    return inspect_legacy_price_csv((HEADER+(''.join(rows) if rows is not None else row())).encode(),
                                   **(dict(symbol='آزمايش', unit='Rial') | changes))


def test_first_last_and_official_close_are_separate_and_reference_is_unknown():
    out, audit = inspect()
    assert (out.open_raw.iloc[0], out.close_raw.iloc[0], out.close_final_raw.iloc[0]) == (101,103,102)
    assert out.source_open_raw.iloc[0] == 98 and out.ref_price_raw.isna().all()
    assert out.symbol.iloc[0] == 'آزمایش'
    assert not audit['verified_native_reference'] and not audit['safe_for_feature_store']
    assert not audit['accepted_for_signals']


def test_single_traded_price_ignores_official_base_volume_close():
    out, audit = inspect([row(first=100, high=100, low=100, last=100, final=101, value=1000)])
    assert out.eligible_for_ohlc_description.iloc[0] and out.one_traded_price.iloc[0]
    assert audit['official_close_outside_trade_range_rows'] == 1


def test_zero_volume_before_between_after_is_not_listing_history():
    zero = dict(first=0, high=0, low=0, last=100, final=100, value=0, volume=0)
    out, audit = inspect([row('20240101', **zero), row('20240102'), row('20240103', **zero),
                          row('20240104'), row('20240105', **zero)])
    assert audit['positive_volume_rows'] == 2 and audit['zero_volume_rows'] == 3
    assert audit['zero_volume_before_first'] == audit['zero_volume_during_span'] == audit['zero_volume_after_last'] == 1
    assert out.observed_traded_session.dropna().tolist() == [1,2]
    assert audit['actual_listing_date'] is None


def test_all_nontraded_rows_do_not_crash_or_invent_listing_date():
    out, audit = inspect([row(volume=0,value=0)])
    assert audit['positive_volume_rows'] == 0 and audit['first_observed_trade'] is None
    assert audit['zero_volume_before_first'] is None and not out.eligible_for_ohlc_description.any()


@pytest.mark.parametrize('volume', ['', 'nan', 'inf', -1, 'True', '1.5', '9007199254740992'])
def test_unknown_invalid_volume_is_not_a_placeholder(volume):
    out, audit = inspect([row(volume=volume)])
    assert audit['invalid_volume_rows'] == 1 and audit['zero_volume_rows'] == 0
    assert not out.eligible_for_ohlc_description.iloc[0] and audit['invalid_rows'] == 1


@pytest.mark.parametrize('changes,reason', [
    ({'first':0}, 'missing_or_nonpositive_price'),
    ({'last':float('inf')}, 'missing_or_nonpositive_price'),
    ({'last':104}, 'traded_price_outside_range'),
    ({'low':105}, 'invalid_range'),
    ({'value':9000}, 'invalid_or_out_of_range_vwap'),
    ({'value':0}, 'invalid_traded_value'),
    ({'volume':0}, 'inconsistent_nontraded_value'),
])
def test_bad_rows_are_retained_with_reasons(changes, reason):
    out, audit = inspect([row(**changes)])
    assert len(out) == 1 and reason in out.issues.iloc[0]
    assert audit['invalid_rows'] == 1 and not out.eligible_for_ohlc_description.iloc[0]


def test_source_open_matching_prior_close_is_diagnostic_only():
    out, audit = inspect([row(), row('20240103', source_open=102)])
    assert audit['source_open_matches_previous_official_close'] == dict(matches=1,comparable=1)
    assert np.isnan(out.ref_price_raw).all() and 'return_pct' not in out


@pytest.mark.parametrize('rows', [[row(),row()], [row(day='20240230')], [row(),row('20240103', ticker='OTHER')]])
def test_dates_and_identity_fail_closed(rows):
    with pytest.raises(ValueError):
        inspect(rows)


@pytest.mark.parametrize('raw', [b'', b'<DTYYYYMMDD>,<FIRST>\n20240102,100', HEADER.replace('<FIRST>','<LAST>').encode(),
    (HEADER+row().strip()+',extra\n').encode(), b'\xff'])
def test_bad_schema_encoding_or_column_counts_fail(raw):
    with pytest.raises(ValueError):
        inspect_legacy_price_csv(raw, symbol='آزمون', unit='Rial')


def test_units_are_not_guessed_or_converted():
    with pytest.raises(ValueError):
        inspect(unit='Toman')


def test_prefix_invariance_without_backward_adjustments():
    prefix, _ = inspect([row(),row('20240103')])
    full, _ = inspect([row(),row('20240103'),row('20240104',first=50,high=50,low=50,last=50,final=50,value=500)])
    # Per-row native prices and observed-session counts cannot be repainted by later data.
    assert prefix.to_json() == full.iloc[:2].to_json()
