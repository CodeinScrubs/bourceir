"""Financial cells are source claims, never automatically verified trading facts."""

import json
from pathlib import Path

import pytest

from core.codal_financial_cells import MAX_HTML_BYTES, VERSION, extract_financial_cells


def cell(address, row, column, value=None, **changes):
    return {'address': address, 'rowSequence': row, 'columnSequence': column,
            'rowSpan': 1, 'colSpan': 1, 'value': value, 'isVisible': True} | changes


def page(cells=None, **changes):
    data = {'type': 6, 'tracingNo': 123, 'period': 6, 'periodEndToDate': '1402/06/31',
            'yearEndToDate': '1402/12/29', 'title_Fa': 'صورت مالی', 'isConsolidated': False,
            'isAudited': True, 'sheets': [{'code': 0, 'title_Fa': 'ترازنامه',
            'tables': [{'code': 4, 'title_Fa': 'دارایی', 'description': 'میلیون ریال',
                        'cells': cells if cells is not None else [cell('A1', 1, 1, 'شرح')]}]}]} | changes
    return '<html><script>\nvar datasource = ' + json.dumps(data, ensure_ascii=False) + ';\n</script></html>'


def test_preserves_zero_absence_hidden_formulas_and_table_description():
    items = [cell('A1', 1, 1, 0), cell('B1', 1, 2, '0', isVisible=False),
             cell('C1', 1, 3, None, formula='1+1'), cell('D1', 1, 4, 1.25)]
    del items[2]['value']
    result = extract_financial_cells(page(items))
    cells = result['tables'][0]['cells']
    assert result['version'] == VERSION and result['report']['tracing_no'] == 123
    assert result['report']['is_audited'] is True and result['report']['is_consolidated'] is False
    assert result['tables'][0]['sheet_code'] == 0 and result['tables'][0]['description'] == 'میلیون ریال'
    assert [(v['raw_value'], v['raw_value_kind']) for v in cells] == [
        (0, 'integer'), ('0', 'string'), (None, 'absent'), ('1.25', 'decimal')]
    assert cells[1]['visible'] is False and cells[2]['formula_present'] is True
    assert result['publication_time_verified'] is False and result['accepted_for_signals'] is False
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize('bad_cells', [
    [cell('A1', 1, 1, 1), cell('A1', 2, 1, 2)],
    [cell('A1', 1, 1, 1), cell('B2', 1, 1, 2)],
    [cell('A1', 1, 1, 1, isVisible='false')],
    [cell('A1', 1, 1, {'nested': 1})],
    [cell('bad', 1, 1, 0)],
    [cell('A1', True, 1, 0)],
])
def test_invalid_or_conflicting_cells_are_not_silently_replaced(bad_cells):
    with pytest.raises(ValueError):
        extract_financial_cells(page(bad_cells))


@pytest.mark.parametrize('changes', [
    {'type': 58}, {'tracingNo': 0}, {'period': 13}, {'isAudited': 1},
    {'yearEndToDate': '1402/13/01'}, {'sheets': []},
])
def test_bad_financial_scope_is_rejected(changes):
    with pytest.raises(ValueError):
        extract_financial_cells(page(**changes))


def test_multiple_or_executable_datasources_cannot_pass():
    html = page()
    with pytest.raises(ValueError):
        extract_financial_cells(html + html)
    with pytest.raises(ValueError):
        extract_financial_cells(html.replace(';\n</script>', '.evil();\n</script>'))
    with pytest.raises(ValueError):
        extract_financial_cells('x' * (MAX_HTML_BYTES + 1))


def test_multiple_sheets_are_preserved_not_conflated():
    data = json.loads(page().split('var datasource = ', 1)[1].split(';\n</script>', 1)[0])
    data['sheets'].append({'code': 1, 'title_Fa': 'سود و زیان',
                           'tables': [{'code': 4, 'title_Fa': None, 'cells': [cell('A1', 1, 1, 3)]}]})
    html = '<script>\nvar datasource = ' + json.dumps(data, ensure_ascii=False) + ';\n</script>'
    result = extract_financial_cells(html)
    assert result['cell_count'] == 2 and [t['sheet_code'] for t in result['tables']] == [0, 1]
    data['sheets'].append(data['sheets'][0])
    with pytest.raises(ValueError):
        extract_financial_cells('<script>\nvar datasource = ' + json.dumps(data, ensure_ascii=False) + ';\n</script>')


def test_address_sequence_mismatch_is_observed_not_rejected():
    result = extract_financial_cells(page([
        cell('A1', 99, 99, 0), cell('Z20', 20, 26, 1),
        cell('AA20', 20, 27, 2), cell('AZ21', 21, 52, 3),
        cell('BA22', 22, 53, 4), cell('AAA23', 23, 703, 5),
        cell('AB24', 25, 28, 6), cell('AC26', 26, 29, 7),
    ]))
    cells = result['tables'][0]['cells']
    assert (cells[0]['address'], cells[0]['row'], cells[0]['column'], cells[0]['raw_value']) == ('A1', 99, 99, 0)
    assert [item['address_matches_sequence'] for item in cells] == [False, True, True, True, True, True, False, True]
    assert result['address_sequence_mismatch_count'] == 2


def test_recorded_external_fixture_when_available_is_not_a_live_or_pit_test():
    root = Path(__file__).resolve().parents[1] / 'data/private/codal-mcp-evaluation-2026-11-01/repo/tests/fixtures'
    if not root.exists():
        pytest.skip('Optional external recorded fixture not available')
    income = extract_financial_cells((root / 'decision_1612323.html').read_text(encoding='utf-8'))
    balance = extract_financial_cells((root / 'decision_1612323_sheet0.html').read_text(encoding='utf-8'))
    assert income['report']['tracing_no'] == balance['report']['tracing_no'] == 1612323
    assert {t['sheet_code'] for t in income['tables']} == {1}
    assert {t['sheet_code'] for t in balance['tables']} == {0}
    assert income['cell_count'] == 183 and balance['cell_count'] == 321
