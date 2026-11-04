"""Financial unit arithmetic and unknowns; synthetic reports remain in tests."""

import hashlib
import json
from fractions import Fraction

import pytest

from core.codal_operating import FALSE_FLAGS, extract_operating_metrics, unit_scale
from tests.test_codal_revenue import TIMES, document, find


def source():
    data = document()
    for address, raw in [('O4', '2'), ('O8', '2'), ('O11', '2')]:
        find(data, address).update(data_type='Float', raw_value=raw, formula_present=False)
    find(data, 'P4')['raw_value'] = '50000000'
    find(data, 'P8')['raw_value'] = '25000000'
    find(data, 'P11')['raw_value'] = '10000000'
    return data


def run(data=None, **kwargs):
    body = json.dumps(source() if data is None else data, ensure_ascii=False).encode()
    return extract_operating_metrics(body, expected_sha256=hashlib.sha256(body).hexdigest(), **(TIMES|kwargs))


def value(frac):
    return None if frac is None else Fraction(int(frac['numerator']), int(frac['denominator']))


def test_thousand_units_are_not_mistaken_for_individual_units():
    result = run()
    domestic, export, service, returned, discounted = result['rows']
    assert result['status'] == 'observed'
    assert value(domestic['implied_rate_rial_per_base_unit']) == 50000000
    assert value(export['implied_rate_rial_per_reported_unit']) == 25000000
    assert value(export['implied_rate_rial_per_base_unit']) == 25000
    assert export['quantity_unit'] == 'هزار عدد' and export['base_unit'] == 'عدد'
    assert export['quantity_scale'] == 1000 and value(export['base_quantity']) == 2000
    assert value(service['implied_rate_rial_per_base_unit']) == 10000000
    assert returned['status'] == discounted['status'] == 'not_applicable'
    assert result['total_revenue_rial'] == '163000000'
    assert result['total_quantity'] is result['aggregate_unit_price'] is None
    for record in [result, *result['rows']]:
        assert all(record[flag] is False for flag in FALSE_FLAGS)
    assert not domestic['publisher_rounding_rule_verified']


def test_nonterminating_unit_price_keeps_exact_fraction_and_does_not_execute_cached_formula():
    data = source(); find(data, 'O4')['raw_value'] = '3'; find(data, 'P4')['raw_value'] = '33333333'
    result = run(data)['rows'][0]
    assert value(result['implied_rate_rial_per_reported_unit']) == Fraction(100000000, 3)
    assert value(result['cached_minus_implied_rial_per_reported_unit']) == Fraction(-1, 3)
    assert result['nearest_rounding_interval_contains_implied'] is True
    assert result['cached_rate_equals_implied'] is False
    assert result['cached_rate_formula_present'] is True
    assert not result['formulas_executed'] and not result['source_rounding_uncertainty_quantified']


@pytest.mark.parametrize('formula,dtype,raw', [(True, 'Float', '2'), (None, 'Float', '2'),
    (False, None, '2'), (False, 'Integer', '2.5'), (False, 'Float', ''),
    (False, 'Float', 'NaN'), (False, 'Float', '1e400'), (False, 'Float', '9'*129)],
    ids=['formula', 'unknown-formula', 'unknown-type', 'fractional-integer', 'missing', 'nan', 'exponent', 'oversize'])
def test_unusable_quantity_does_not_fall_back_to_cached_rate(formula, dtype, raw):
    data = source(); find(data, 'O4').update(formula_present=formula, data_type=dtype, raw_value=raw)
    result = run(data)
    row = result['rows'][0]
    assert result['status'] == 'observed' and len(result['rows']) == 5
    assert row['status'] == 'quantity_unavailable'
    assert row['implied_rate_rial_per_reported_unit'] is None
    assert row['implied_rate_rial_per_base_unit'] is None
    assert result['total_revenue_rial'] == '163000000'


def test_zero_quantity_with_revenue_is_anomaly_without_invented_unit_price():
    data = source(); find(data, 'O4')['raw_value'] = '0'
    row = run(data)['rows'][0]
    assert value(row['reported_quantity']) == 0 and value(row['base_quantity']) == 0
    assert row['status'] == 'undefined_zero_quantity'
    assert row['reasons'] == ['zero_quantity_with_positive_revenue']
    assert row['implied_rate_rial_per_reported_unit'] is None


def test_zero_over_zero_is_unknown_and_is_not_zero_unit_price():
    data = source(); find(data, 'O4')['raw_value'] = '0'
    for address, raw in [('Q4', '0'), ('Q6', '0'), ('Q17', '63')]:
        find(data, address)['raw_value'] = raw
    row = run(data)['rows'][0]
    assert row['status'] == 'undefined_zero_quantity'
    assert row['reasons'] == ['zero_quantity_and_zero_revenue']
    assert row['implied_rate_rial_per_base_unit'] is None


def test_unknown_unit_keeps_reported_ratio_but_does_not_guess_a_base_scale():
    data = source(); find(data, 'B4')['raw_value'] = 'بسته با وزن نامعلوم'
    row = run(data)['rows'][0]
    assert row['status'] == 'reported_unit_only' and row['quantity_scale'] is None
    assert value(row['implied_rate_rial_per_reported_unit']) == 50000000
    assert row['base_quantity'] is row['implied_rate_rial_per_base_unit'] is None


@pytest.mark.parametrize('label,base,scale', [('عدد', 'عدد', 1), ('هزار تيوپ', 'تیوپ', 1000),
    ('میلیون عدد', 'عدد', 1000000), ('  هزار   شیشه ', 'شیشه', 1000),
    ('تن', 'تن', 1), ('کیلوگرم', 'کیلوگرم', 1)])
def test_only_explicit_unit_prefixes_are_converted(label, base, scale):
    assert unit_scale(label) == dict(base_unit=base, multiplier_to_base=scale)


@pytest.mark.parametrize('label', ['هزارعدد', 'میلیارد عدد', 'میلیون ریال', None, '', 'واحد'])
def test_ambiguous_or_unsupported_units_stay_unknown(label):
    assert unit_scale(label) is None


def test_negative_sales_quantity_is_retained_without_negative_sell_price():
    data = source(); find(data, 'O4')['raw_value'] = '-2'
    row = run(data)['rows'][0]
    assert value(row['reported_quantity']) == -2
    assert row['status'] == 'quantity_unavailable' and row['implied_rate_rial_per_base_unit'] is None


def test_cached_rate_mismatch_is_exposed_without_replacing_the_primitive_derived_rate():
    data = source(); find(data, 'P4')['raw_value'] = '0'
    row = run(data)['rows'][0]
    assert value(row['implied_rate_rial_per_base_unit']) == 50000000
    assert row['cached_rate_equals_implied'] is False
    assert row['nearest_rounding_interval_contains_implied'] is False
    assert not row['publisher_rounding_rule_verified']


def test_cached_nonnumeric_formula_value_is_not_executed_or_used_as_a_quantity():
    data = source(); find(data, 'P4')['raw_value'] = '=Q4/O4*1000000'
    row = run(data)['rows'][0]
    assert row['status'] == 'computed' and value(row['implied_rate_rial_per_base_unit']) == 50000000
    assert row['cached_rate_rial_per_reported_unit'] is None and not row['formulas_executed']


@pytest.mark.parametrize('address,field,value_', [('Q4', 'raw_value', '101'),
    ('O4', 'period_end_jalali_raw', '1403/05/31'), ('Q2', 'raw_value', 'مبلغ فروش (هزار ریال)'),
    ('O4', 'visible', False)])
def test_broken_revenue_geometry_units_or_period_cannot_make_operating_metrics(address, field, value_):
    data = source(); find(data, address)[field] = value_
    result = run(data)
    assert result['status'] == 'unavailable' and result['rows'] is None


def test_hash_and_availability_are_required_before_financial_arithmetic():
    body = json.dumps(source()).encode()
    result = extract_operating_metrics(body, expected_sha256='0'*64, **TIMES)
    assert result['status'] == 'unavailable' and result['source_bytes_verified'] is False
    assert run(available_at='2020-01-01T00:00:00+03:30')['status'] == 'unavailable'
