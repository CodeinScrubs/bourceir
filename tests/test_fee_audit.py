"""Synthetic cost comparisons only; no private account rows or live tariff claims."""

from copy import deepcopy
from decimal import Decimal

import pytest

from core.fee_audit import compare_export_fees

RATES = dict(buy=0.003712, sell=0.0088)


def row(**changes):
    return dict(date='2021-03-22', side='buy', raw_symbol=None, quantity=2,
                price_rial=1000, fee_rial=7.424, fee_source='broker_export') | changes


def test_decimal_comparison_retains_duplicates_missing_symbols_and_originals():
    records = [row(), row(), row(side='sell', quantity=1, price_rial=1000, fee_rial=8.8)]
    before = deepcopy(records)
    result = compare_export_fees(records, RATES)
    assert records == before
    assert result['row_count'] == 3 and result['different_row_count'] == 0
    buy = result['sides']['buy']
    assert buy['row_count'] == 2 and buy['missing_symbol_rows'] == 2
    assert buy['reported_fee_rial'] == '14.848'
    assert buy['status_counts'] == dict(exact_scenario_match=2)
    assert buy['observed_weighted_fee_fraction'] == pytest.approx(.003712)
    for flag in ('historical_fee_schedule_verified', 'fee_tax_breakdown_verified',
                 'actual_history_complete', 'historical_instrument_scope_verified', 'accepted_for_signals'):
        assert result[flag] is False


def test_offsetting_differences_do_not_disappear_in_a_zero_signed_total():
    records = [row(fee_rial=9.424), row(fee_rial=5.424)]
    result = compare_export_fees(records, RATES, detail_limit=1)
    buy = result['sides']['buy']
    assert Decimal(buy['reported_minus_scenario_rial']) == 0
    assert Decimal(buy['absolute_difference_rial']) == 4
    assert Decimal(buy['max_absolute_difference_rial']) == 2
    assert buy['status_counts']['different_from_scenario'] == 2
    assert result['different_row_count'] == 2 and result['details_truncated']
    assert [r['source_row_index'] for r in result['differences']] == [0]
    all_details = compare_export_fees(records, RATES, detail_limit=50)
    assert result['sides'] == all_details['sides']
    assert all_details['differences'][1]['reported_minus_scenario_rial'] == '-2.000000'


def test_declared_tolerance_is_symmetric_and_not_inferred_from_matches():
    result = compare_export_fees([row(fee_rial=8.424), row(fee_rial=6.424), row(fee_rial=8.4240001)], RATES)
    assert result['sides']['buy']['status_counts'] == {
        'within_declared_tolerance': 2, 'different_from_scenario': 1}
    strict = compare_export_fees([row(fee_rial=8.424)], RATES, tolerance_rial=0)
    assert strict['different_row_count'] == 1


def test_empty_export_keeps_observed_rate_and_dates_unknown():
    result = compare_export_fees([], RATES)
    assert result['row_count'] == 0 and result['date_start'] is None and result['date_end'] is None
    assert all(x['observed_weighted_fee_fraction'] is None for x in result['sides'].values())
    assert not result['historical_fee_schedule_verified']


def test_weighted_rate_uses_notional_denominator_and_does_not_fit_scenario():
    result = compare_export_fees([row(quantity=1, price_rial=100, fee_rial=1),
                                 row(quantity=1, price_rial=200, fee_rial=4)], RATES, detail_limit=0)
    assert result['sides']['buy']['observed_weighted_fee_fraction'] == pytest.approx(5 / 300)
    assert result['scenario']['rates'] == {'buy': '0.003712', 'sell': '0.0088'}
    assert result['different_row_count'] == 1 and result['details_truncated']


@pytest.mark.parametrize('changes', [dict(quantity=True), dict(quantity=0), dict(quantity=1.5),
    dict(price_rial=-1), dict(price_rial=float('nan')), dict(fee_rial=float('inf')), dict(fee_rial=-1),
    dict(fee_rial='7.424'), dict(date='2021-3-22'), dict(date='2021-02-30'),
    dict(side='hold'), dict(side=[]), dict(fee_source='config_estimate')])
def test_invalid_export_values_fail_closed(changes):
    with pytest.raises(ValueError):
        compare_export_fees([row(**changes)], RATES)


@pytest.mark.parametrize('rates', [dict(buy=-1, sell=.01), dict(buy=float('nan'), sell=.01),
    dict(buy=True, sell=.01), dict(buy=1, sell=.01), dict(buy='.003712', sell=.01),
    dict(buy=.003712), {'buy': .003712, 'sell': .0088, 'tax': .005}, None])
def test_invalid_reference_rates_are_not_zero_or_defaults(rates):
    with pytest.raises(ValueError):
        compare_export_fees([row()], rates)


@pytest.mark.parametrize('kwargs', [dict(tolerance_rial=-1), dict(tolerance_rial=float('nan')),
    dict(tolerance_rial=True), dict(detail_limit=-1), dict(detail_limit=1001), dict(detail_limit=1.5),
    dict(detail_limit=True)])
def test_invalid_comparison_policy_rejected(kwargs):
    with pytest.raises(ValueError):
        compare_export_fees([row()], RATES, **kwargs)


def test_nonexact_monetary_arithmetic_is_not_reported_as_exact():
    with pytest.raises(ValueError, match='دقت'):
        compare_export_fees([row(price_rial=Decimal('1.1' + '1' * 90))], RATES)


def test_malformed_or_missing_required_record_fields_rejected():
    missing = row()
    del missing['fee_rial']
    for record in (None, [], {}, missing):
        with pytest.raises(ValueError):
            compare_export_fees([record], RATES)
    for records in (None, {}, iter([])):
        with pytest.raises(ValueError):
            compare_export_fees(records, RATES)
