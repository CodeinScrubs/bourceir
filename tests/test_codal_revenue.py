"""Synthetic documents stay in tests; no network or financial account is used."""

import copy
import hashlib
import json

import pytest

from core.codal_revenue import VERSION, extract_monthly_revenue
from core.codal_tables import TABLE_VERSION

PERIOD = '1403/06/31'
TIMES = dict(observed_at='2024-09-23T10:00:00+03:30', archived_at='2024-09-23T10:00:01+03:30',
             available_at='2024-09-23T10:00:02+03:30')
COLS = {1: 'A', 2: 'B', 14: 'N', 15: 'O', 16: 'P', 17: 'Q'}


def document():
    cells = []

    def cell(row, col, raw, *, group='Body', formula=False, dtype=None, span=1):
        item = dict(address=f'{COLS[col]}{row}', row=row, column=col, row_span=1,
                    column_span=span, group=group, data_type=dtype, raw_value=raw,
                    value_present=True, visible=True, formula_present=formula,
                    period_end_jalali_raw=PERIOD if col >= 14 else '')
        cells.append(item)

    for row, col, raw, span in [(1, 1, 'شرح', 2), (2, 1, 'نام محصول', 1), (2, 2, 'واحد', 1),
        (1, 14, f'دوره یک ماهه منتهی به {PERIOD}', 4), (2, 14, 'تعداد تولید', 1),
        (2, 15, 'تعداد فروش', 1), (2, 16, 'نرخ فروش (ریال)', 1),
        (2, 17, 'مبلغ فروش (میلیون ریال)', 1)]:
        cell(row, col, raw, group='Header', span=span)
    body = [
        ('فروش داخلی:', '', '', False), ('محصول الف', 'عدد', '100', False),
        ('', '', '', False), ('جمع فروش داخلی', '', '100', True),
        ('فروش صادراتی:', '', '', False), ('محصول الف', 'هزار عدد', '50', False),
        ('جمع فروش صادراتی', '', '50', True), ('درآمد ارائه خدمات:', '', '', False),
        ('خدمت الف', 'ساعت', '20', False), ('جمع درآمد ارائه خدمات', '', '20', True),
        ('برگشت از فروش:', '', '', False), ('محصول الف', 'عدد', '-5', False),
        ('جمع برگشت از فروش', '', '-5', True), ('تخفیفات', '', '-2', False),
        ('جمع', '', '163', True),
    ]
    for row, (label, unit, amount, formula) in enumerate(body, 3):
        for col, raw in [(1, label), (2, unit), (14, ''), (15, '2' if unit else ''),
                         (16, 'source-derived-rate' if unit else ''), (17, amount)]:
            cell(row, col, raw, formula=formula if col == 17 else col == 16 and bool(unit),
                 dtype='Float' if col == 17 and not formula and amount else None)
    # A formula in an unrelated cumulative column is preserved but not consumed.
    cells.append(dict(address='F4', row=4, column=6, row_span=1, column_span=1, group='Body',
        visible=True, raw_value='999999', value_present=True, formula_present=True, data_type=None))
    return dict(version=TABLE_VERSION, report=dict(type=58, period_months=1, tracing_no=123,
        is_consolidated=False, period_end_jalali=PERIOD),
        tables=[dict(table_code=1197, title='تولید و فروش', cells=cells)])


def find(data, address):
    return next(c for c in data['tables'][0]['cells'] if c['address'] == address)


def run(data, **kwargs):
    body = json.dumps(data, ensure_ascii=False).encode()
    return extract_monthly_revenue(body, expected_sha256=hashlib.sha256(body).hexdigest(), **(TIMES | kwargs))


def test_signed_primitive_ledger_reconciles_cached_totals_without_formula_execution():
    data = document()
    original = copy.deepcopy(data)
    result = run(data)
    assert result['status'] == 'reconciled', result['reasons']
    assert result['version'] == VERSION
    assert result['total']['amount_rial'] == '163000000'
    assert [x['amount_rial'] for x in result['sections']] == ['100000000', '50000000', '20000000', '-5000000']
    assert [x['section'] for x in result['rows']] == ['domestic', 'export', 'services', 'returns', 'discounts']
    assert result['rows'][1]['quantity_unit'] == 'هزار عدد'
    assert result['rows'][0]['rate_raw_value'] == 'source-derived-rate'
    assert result['source_bytes_verified'] and result['cached_totals_reconciled']
    for key in ('formulas_executed', 'document_identity_verified', 'historical_publication_verified',
                'quantity_and_rate_verified', 'accepted_for_features', 'accepted_for_signals', 'profitability_proven'):
        assert result[key] is False
    assert result['structural_placeholders'] == [dict(row=5, revenue_address='Q5', raw_value='')]
    assert data == original


@pytest.mark.parametrize('address,field,value', [
    ('Q4', 'formula_present', True), ('Q4', 'formula_present', None),
    ('Q4', 'data_type', None), ('Q4', 'raw_value', ''), ('Q4', 'raw_value', 'NaN'),
    ('Q4', 'raw_value', '101'), ('Q6', 'raw_value', '101'), ('Q17', 'raw_value', '164'),
    ('Q14', 'raw_value', '5'), ('Q16', 'raw_value', '2'), ('Q4', 'raw_value', '-100'),
    ('Q2', 'raw_value', 'مبلغ فروش (هزار ریال)'), ('N1', 'raw_value', 'از ابتدای سال مالی تا تاریخ 1403/06/31'),
    ('Q2', 'period_end_jalali_raw', '1403/05/31'), ('Q4', 'period_end_jalali_raw', '1403/05/31'),
    ('Q4', 'visible', False), ('Q4', 'column_span', 2), ('Q4', 'address', 'Q5'),
    ('A4', 'formula_present', True), ('B4', 'raw_value', ''), ('Q5', 'raw_value', '1'),
    ('O5', 'raw_value', '1'), ('Q5', 'formula_present', None), ('B5', 'raw_value', 'عدد'),
    ('Q6', 'formula_present', False), ('Q4', 'value_present', False),
])
def test_invalid_values_structure_totals_and_signs_fail_without_partial_ledger(address, field, value):
    data = document()
    find(data, address)[field] = value
    result = run(data)
    assert result['status'] == 'invalid'
    assert result['rows'] is None and result['total'] is None and result['sections'] is None
    assert result['reasons'] and not result['cached_totals_reconciled']


@pytest.mark.parametrize('mutation', ['duplicate_product', 'duplicate_product_changed_unit', 'duplicate_table', 'duplicate_cell',
                                    'missing_cell', 'wrong_type', 'consolidated', 'non_monthly', 'huge_row'])
def test_ambiguous_or_unsupported_documents_are_rejected(mutation):
    data = document()
    if mutation in ('duplicate_product', 'duplicate_product_changed_unit'):
        find(data, 'A5')['raw_value'] = 'محصول الف'
        find(data, 'B5')['raw_value'] = 'عدد' if mutation == 'duplicate_product' else 'هزار عدد'
        find(data, 'Q5').update(raw_value='0', data_type='Integer')
    elif mutation == 'duplicate_table':
        data['tables'].append(copy.deepcopy(data['tables'][0]))
    elif mutation == 'duplicate_cell':
        data['tables'][0]['cells'].append(copy.deepcopy(find(data, 'Q4')))
    elif mutation == 'missing_cell':
        data['tables'][0]['cells'].remove(find(data, 'Q4'))
    elif mutation == 'wrong_type':
        data['report']['type'] = 59
    elif mutation == 'consolidated':
        data['report']['is_consolidated'] = True
    elif mutation == 'non_monthly':
        data['report']['period_months'] = 3
    else:
        find(data, 'F4')['row'] = 10**12
    assert run(data)['status'] == 'invalid'


def test_cumulative_value_does_not_become_previous_month_or_affect_current_revenue():
    data = document()
    before = run(data)
    find(data, 'F4')['raw_value'] = '123'
    after = run(data)
    assert before['source_sha256'] != after['source_sha256']
    assert before['rows'] == after['rows'] and before['total'] == after['total']
    assert not any('mom' in key or 'previous' in key for key in after)


@pytest.mark.parametrize('times', [
    dict(available_at='2024-09-23T09:00:00+03:30'), dict(archived_at='2024-09-23T09:00:00+03:30'),
    dict(observed_at='2024-09-23T10:00:00'), dict(observed_at='2024-09-20T10:00:00+03:30'),
])
def test_availability_never_backdates_receipt_archive_or_report_period(times):
    assert run(document(), **times)['status'] == 'invalid'


def test_hash_mismatch_duplicate_json_and_nonfinite_json_fail_closed():
    body = json.dumps(document()).encode()
    bad_hash = extract_monthly_revenue(body, expected_sha256='0' * 64, **TIMES)
    assert bad_hash['status'] == 'invalid' and not bad_hash['source_bytes_verified']
    for altered in (body.replace(b'"tracing_no": 123', b'"tracing_no": 123, "tracing_no": 456'),
                    body.replace(b'"tracing_no": 123', b'"tracing_no": NaN')):
        result = extract_monthly_revenue(altered, expected_sha256=hashlib.sha256(altered).hexdigest(), **TIMES)
        assert result['status'] == 'invalid' and result['rows'] is None


def test_placeholder_zero_formats_are_explicit_structural_padding():
    data = document()
    find(data, 'Q5').update(raw_value='0.00', data_type='Float')
    result = run(data)
    assert result['status'] == 'reconciled'
    assert result['structural_placeholders'][0]['raw_value'] == '0.00'
    assert len(result['rows']) == 5


def test_fractional_revenue_keeps_exact_decimal_arithmetic_in_rial():
    data = document()
    find(data, 'Q4')['raw_value'] = '100.125'
    find(data, 'Q6')['raw_value'] = '100.125'
    find(data, 'Q17')['raw_value'] = '163.125'
    result = run(data)
    assert result['status'] == 'reconciled'
    assert result['total']['amount_rial'] == '163125000.000'
    find(data, 'Q4')['data_type'] = 'Integer'
    assert run(data)['status'] == 'invalid'


@pytest.mark.parametrize('address,field,value', [
    ('Q4', 'column', 18), ('A4', 'column', 3), ('N1', 'column', 20), ('F4', 'row', 5),
])
def test_every_cell_address_must_match_its_actual_coordinates(address, field, value):
    data = document()
    find(data, address)[field] = value
    result = run(data)
    assert result['status'] == 'invalid'
    assert result['rows'] is None and result['total'] is None
    assert 'cell address disagrees with coordinates' in result['reasons']


@pytest.mark.parametrize('mutation', ['body_span', 'header_span', 'unknown_group_span', 'vertical_span'])
def test_ignored_cells_cannot_overlap_consumed_header_or_body_regions(mutation):
    data = document()
    cell = find(data, 'F4')
    if mutation == 'body_span':
        cell['column_span'] = 12  # F..Q overlaps the consumed month revenue.
    elif mutation == 'header_span':
        cell.update(address='F1', row=1, column_span=12, group='Other')
    elif mutation == 'unknown_group_span':
        cell.update(column_span=12, group='Other')
    else:
        data['tables'][0]['cells'].remove(find(data, 'N3'))
        cell.update(address='N3', row=3, column=14, row_span=2, group='Other')
    result = run(data)
    assert result['status'] == 'invalid'
    assert result['rows'] is None and result['total'] is None


def test_deep_json_and_timestamp_overflow_return_invalid_without_raising():
    body = b'[' * 2000 + b'0' + b']' * 2000
    result = extract_monthly_revenue(body, expected_sha256=hashlib.sha256(body).hexdigest(), **TIMES)
    assert result['status'] == 'invalid' and result['rows'] is None
    result = run(document(), observed_at='9999-12-31T23:59:59-23:59')
    assert result['status'] == 'invalid' and result['rows'] is None


def test_nonzero_primitive_cannot_underflow_to_zero_and_reconcile_zero_total():
    data = document()
    find(data, 'Q4')['raw_value'] = '0.' + '0' * 1000199 + '1'
    find(data, 'Q6')['raw_value'] = '0'
    find(data, 'Q17')['raw_value'] = '63'
    result = run(data)
    assert result['status'] == 'invalid' and result['rows'] is None and result['total'] is None


def test_precision_limit_applies_before_decimal_arithmetic():
    data = document()
    find(data, 'Q4')['raw_value'] = '0.' + '0' * 40 + '1'
    result = run(data)
    assert result['status'] == 'invalid' and 'unsupported revenue precision' in result['reasons']
