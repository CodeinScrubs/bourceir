"""Synthetic native rows only. Future prices never become a past entitlement mark."""

from copy import deepcopy

import pytest

from research.rights_valuation import dated_rights_quote


def row(**changes):
    result = dict(insCode='123', dEven=20240110, qTotTran5J=0, qTotCap=0, zTotTran=0,
                  priceFirst=0, priceMax=0, priceMin=0, pDrCotVal=500, pClosing=500)
    result.update(changes)
    return result


def observation(rows=None, **changes):
    result = dict(source='tsetmc', ins_code='123', price_unit='Rial',
        start_date='2024-01-01', end_date_exclusive='2024-02-01',
        payload=dict(closingPriceDaily=[row()] if rows is None else rows))
    result.update(changes)
    return result


def assess(obs=None):
    return dated_rights_quote(observation() if obs is None else obs,
        ins_code='123', day='2024-01-10', issue_start='2024-01-10')


def test_carried_close_is_not_a_trade_mark_or_a_zero_entitlement_value():
    result = assess()
    assert result['status'] == 'observed_zero_activity_not_a_quote'
    assert result['excluded_nontrade_close_rial'] == 500
    assert result['market_mark_rial'] is None and result['execution_price_rial'] is None


def test_future_trade_or_invalid_future_numbers_never_fill_the_terminal_price():
    future = row(dEven=20240111, qTotTran5J=100, qTotCap=10000, zTotTran=1,
                 priceFirst=100, priceMax=100, priceMin=100, pDrCotVal=100, pClosing=100)
    assert assess(observation([row(), future])) == assess()
    future['pClosing'] = 'unreadable future field'
    assert assess(observation([row(), future])) == assess()


def test_observed_trade_is_a_mark_without_claiming_execution_or_source_completeness():
    result = assess(observation([row(qTotTran5J=100, qTotCap=10000, zTotTran=2,
        priceFirst=100, priceMax=101, priceMin=99, pDrCotVal=101, pClosing=100)]))
    assert result['market_mark_rial'] == 100 and result['last_trade_rial'] == 101
    assert result['execution_price_rial'] is None
    assert not result['actual_execution_verified'] and not result['historical_coverage_verified']


@pytest.mark.parametrize('changes', [dict(qTotCap=100000), dict(qTotCap=100),
                                   dict(zTotTran=101), dict(qTotTran5J=100.5)])
def test_impossible_turnover_or_unit_counts_cannot_establish_a_trade_mark(changes):
    traded = row(qTotTran5J=100, qTotCap=10000, zTotTran=2,
                 priceFirst=100, priceMax=101, priceMin=99, pDrCotVal=100, pClosing=100)
    traded.update(changes)
    result = assess(observation([traded]))
    assert result['market_mark_rial'] is None
    assert result['status'] != 'observed_dated_trade_mark'


def test_missing_or_duplicate_day_is_unknown_without_imputing_a_halt():
    assert assess(observation([]))['status'] == 'no_observed_row'
    assert assess(observation([row(), row()]))['status'] == 'ambiguous_duplicate_day'


@pytest.mark.parametrize('changes', [dict(qTotTran5J=1), dict(priceFirst=100), dict(zTotTran=2),
                                   dict(qTotCap=100), dict(qTotTran5J=True), dict(pClosing=float('nan')),
                                   dict(zTotTran=1.5), dict(insCode='124')])
def test_contradictory_or_invalid_source_never_becomes_a_known_mark(changes):
    result = assess(observation([row(**changes)]))
    assert result['market_mark_rial'] is None and result['execution_price_rial'] is None
    assert result['status'] != 'observed_dated_trade_mark'


@pytest.mark.parametrize('changes', [dict(source='other'), dict(ins_code='124'), dict(price_unit='Toman'),
                                   dict(end_date_exclusive='2024-01-10'), dict(start_date='2024-01-11')])
def test_source_identity_units_and_window_must_match(changes):
    with pytest.raises(ValueError):
        assess(observation(**changes))


def test_previous_issue_date_is_rejected_and_inputs_are_not_mutated():
    with pytest.raises(ValueError):
        dated_rights_quote(observation(), ins_code='123', day='2024-01-09', issue_start='2024-01-10')
    source = observation()
    before = deepcopy(source)
    assess(source)
    assert source == before


@pytest.mark.parametrize('native_day', [True, '20240110', 20241301, 20240132])
def test_unknown_dates_cannot_be_silently_excluded(native_day):
    with pytest.raises(ValueError):
        assess(observation([row(), row(dEven=native_day)]))
