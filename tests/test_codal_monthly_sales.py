"""Synthetic monthly cells; no network, live account or production data writes."""

import copy
import hashlib
import json

import pytest

from core.codal_monthly_sales import VERSION, extract_monthly_sales
from core.codal_revenue import extract_monthly_revenue
from tests.test_codal_revenue import TIMES, document, find


def source(start=6):
    data = document()
    for cell in data['tables'][0]['cells']:
        if cell['column'] == 15 and cell.get('raw_value') == '2':
            cell['data_type'] = 'Integer'
        if cell['row'] >= 3:
            cell['row'] += start-3
            cell['address'] = ''.join(c for c in cell['address'] if c.isalpha())+str(cell['row'])
    return data


def run(data, **times):
    body = json.dumps(data, ensure_ascii=False).encode('utf-8')
    return extract_monthly_sales(body, expected_sha256=hashlib.sha256(body).hexdigest(), **(TIMES | times))


@pytest.mark.parametrize('start', [3, 6])
def test_supported_layouts_keep_exact_source_coordinates_and_signed_arithmetic(start):
    data = source(start)
    before = copy.deepcopy(data)
    result = run(data)
    assert result['status'] == 'reconciled', result['reasons']
    assert result['version'] == VERSION and result['total']['amount_rial'] == '163000000'
    assert result['body_first_row'] == start and result['body_last_row'] == start+14
    assert result['undeclared_leading_rows'] == list(range(3, start))
    assert result['rows'][0]['revenue_address'] == f'Q{start+1}'
    assert result['rows'][0]['implied_rate_rial_per_base_unit'] == dict(numerator='50000000', denominator='1')
    assert result['rows'][1]['base_quantity'] == dict(numerator='2000', denominator='1')
    assert result['rows'][1]['implied_rate_rial_per_base_unit'] == dict(numerator='25000', denominator='1')
    assert result['rows'][3]['status'] == result['rows'][4]['status'] == 'not_applicable'
    assert result['source_coordinates_preserved'] and data == before
    assert result['aggregate_quantity_computed'] is result['aggregate_rate_computed'] is False
    assert result['accepted_for_features'] is result['accepted_for_signals'] is result['profitability_proven'] is False


def test_new_contract_does_not_reinterpret_legacy_v1_archives_or_invent_gap_cells():
    data = source()
    body = json.dumps(data).encode()
    legacy = extract_monthly_revenue(body, expected_sha256=hashlib.sha256(body).hexdigest(), **TIMES)
    assert legacy['status'] == 'invalid'
    result = run(data)
    assert result['status'] == 'reconciled'
    assert result['structural_placeholders'] == [dict(row=8, revenue_address='Q8', raw_value='')]
    assert not any(cell['row'] in (3, 4, 5) for cell in data['tables'][0]['cells'])


@pytest.mark.parametrize('field,value', [('raw_value', ''), ('formula_present', None),
    ('formula_present', True), ('data_type', None), ('value_present', False)])
def test_missing_or_nonprimitive_quantity_never_becomes_rendered_zero(field, value):
    data = source()
    find(data, 'O7').update(data_type='Float')
    find(data, 'O7')[field] = value
    row = run(data)['rows'][0]
    assert row['status'] == 'quantity_unavailable'
    assert row['reported_quantity'] is row['base_quantity'] is row['implied_rate_rial_per_base_unit'] is None


def test_positive_sales_with_explicit_zero_quantity_is_not_an_infinite_selling_price():
    data = source()
    find(data, 'O7').update(raw_value='0', data_type='Integer')
    row = run(data)['rows'][0]
    assert row['status'] == 'undefined_zero_quantity'
    assert row['reported_quantity'] == dict(numerator='0', denominator='1')
    assert row['implied_rate_rial_per_base_unit'] is None
    assert row['reasons'] == ['zero_quantity_with_positive_revenue']


@pytest.mark.parametrize('address,field,value', [
    ('Q7', 'raw_value', '101'), ('Q9', 'raw_value', '101'), ('Q20', 'raw_value', '164'),
    ('Q7', 'raw_value', '-100'), ('Q17', 'raw_value', '5'), ('Q19', 'raw_value', '2'),
    ('Q7', 'raw_value', 'NaN'), ('Q7', 'raw_value', '1e9999'), ('Q7', 'formula_present', True),
    ('Q7', 'formula_present', None), ('Q7', 'data_type', None), ('Q7', 'value_present', False),
    ('Q7', 'visible', False), ('Q7', 'column_span', 2), ('A7', 'formula_present', None),
    ('Q8', 'raw_value', '1'), ('O8', 'raw_value', '1'), ('B8', 'raw_value', 'عدد'),
    ('Q2', 'raw_value', 'مبلغ فروش (هزار ریال)'), ('Q7', 'period_end_jalali_raw', '1403/05/31'),
    ('N1', 'raw_value', 'از ابتدای سال مالی تا تاریخ 1403/06/31'),
])
def test_invalid_ledger_fails_without_partial_numbers(address, field, value):
    data = source()
    find(data, address)[field] = value
    result = run(data)
    assert result['status'] == 'invalid' and result['reasons']
    assert result['rows'] is result['sections'] is result['total'] is None


@pytest.mark.parametrize('mutation', ['declared_gap_cell', 'hidden_gap_cell', 'gap_overlap', 'header_overlap',
    'duplicate', 'interior_gap', 'wrong_address', 'unsupported_start', 'body_overlap'])
def test_an_undeclared_leading_gap_cannot_hide_observed_or_overlapping_cells(mutation):
    data = source()
    cells = data['tables'][0]['cells']
    if mutation in ('declared_gap_cell', 'hidden_gap_cell', 'gap_overlap', 'header_overlap'):
        other = copy.deepcopy(find(data, 'F7'))
        other.update(row=3, address='F3', group='Other', visible=mutation != 'hidden_gap_cell')
        if mutation == 'gap_overlap':
            other['column_span'] = 12
        elif mutation == 'header_overlap':
            other.update(row=1, address='F1', column_span=12)
        cells.append(other)
    elif mutation == 'duplicate':
        cells.append(copy.deepcopy(find(data, 'Q7')))
    elif mutation == 'interior_gap':
        data['tables'][0]['cells'] = [c for c in cells if c['row'] != 8]
    elif mutation == 'wrong_address':
        find(data, 'Q7')['column'] = 18
    elif mutation == 'body_overlap':
        find(data, 'F7')['column_span'] = 12
    else:
        data = source(4)
    result = run(data)
    assert result['status'] == 'invalid' and result['rows'] is result['total'] is None


@pytest.mark.parametrize('times', [dict(available_at='2024-09-23T09:59:59+03:30'),
    dict(observed_at='2024-09-23T10:00:00'), dict(observed_at='2024-09-20T10:00:00+03:30')])
def test_receipt_and_period_times_cannot_be_backdated(times):
    assert run(source(), **times)['status'] == 'invalid'


def test_duplicate_json_nonfinite_tokens_and_rebound_hash_cannot_invent_valid_sales():
    body = json.dumps(source()).encode()
    assert extract_monthly_sales(body, expected_sha256='0'*64, **TIMES)['status'] == 'invalid'
    for changed in (body.replace(b'"tracing_no": 123', b'"tracing_no": 123, "tracing_no": 456'),
            body.replace(b'"tracing_no": 123', b'"tracing_no": Infinity')):
        result = extract_monthly_sales(changed, expected_sha256=hashlib.sha256(changed).hexdigest(), **TIMES)
        assert result['status'] == 'invalid' and result['total'] is None


def test_cumulative_columns_remain_outside_monthly_sales_and_average_rate_claims():
    data = source()
    before = run(data)
    find(data, 'F7')['raw_value'] = '123456789'
    after = run(data)
    assert before['source_sha256'] != after['source_sha256']
    assert before['rows'] == after['rows'] and before['total'] == after['total']
    assert after['tariff_change_verified'] is after['product_mix_adjusted'] is False
