"""Synthetic header mutations only; no company or account data."""

import pytest

from tests.test_codal_tables import normalize_supported, supported_month


@pytest.mark.parametrize('extra', ['ماه قبل', 'دوره مشابه سال قبل'])
def test_current_period_with_conflicting_role_is_not_promoted(extra):
    raw = supported_month()
    raw['sheets'][0]['tables'][0]['cells'][1]['value'] += ' ' + extra
    result = normalize_supported(raw)
    assert result['verified'] is False
    assert result['accepted_for_features'] is False
    assert result['rows'] is None


def test_previous_period_with_conflicting_year_ago_role_is_not_promoted():
    raw = supported_month()
    raw['sheets'][0]['tables'][0]['cells'][2]['value'] += ' دوره مشابه سال قبل'
    result = normalize_supported(raw)
    assert result['verified'] is False
    assert result['accepted_for_features'] is False


def test_unambiguous_existing_monthly_contract_stays_valid():
    result = normalize_supported(supported_month())
    assert result['verified'] and result['accepted_for_features']
    assert result['totals']['current']['revenue_rial'] == '120000000'
