"""آزمون داده ساختاریافته فقط با داده آزمون و پوشه موقت؛ بدون شبکه یا حساب."""

import copy
import json
from datetime import datetime

import pytest

import brs
from config import settings
from core.codal_tables import extract_monthly_tables, numeric_candidate
from ops.codal_tables import capture_codal_tables, load_codal_tables
from tests.test_codal_documents import Response, provider

URL = 'https://codal.ir/Reports/Decision.aspx?LetterSerial=test-monthly'


def raw_report():
    def cell(address, row, column, value, dtype=None, **kw):
        return dict(address=address, rowSequence=row, columnSequence=column, rowSpan=1, colSpan=1,
                    value=value, dataTypeName=dtype, isVisible=True, cellGroupName='Body', **kw)
    return dict(type=58, period=1, tracingNo=123, title_Fa='گزارش تولیدی آزمون',
                periodEndToDate='1403/09/30', yearEndToDate='1403/12/30', publishDateTime='1403/10/01 12:00:00',
                isConsolidated=False, isAudited=False,
                sheets=[dict(code=1, title_Fa='برگه آزمون', tables=[dict(code=1, title_Fa='فروش', cells=[
                    cell('A1', 1, 1, 'مبلغ فروش (میلیون ریال)'), cell('B1', 1, 2, 'نرخ فروش (ریال)'),
                    cell('A2', 2, 1, '۰', 'Integer'), cell('B2', 2, 2, '', 'Float'),
                    cell('A3', 3, 1, '۱٬۲۳۴٫۵', 'Float'), cell('B3', 3, 2, '1,23', 'Integer'),
                ])])])


def html(raw=None, *, prefix='', suffix=''):
    data = json.dumps(raw_report() if raw is None else raw, ensure_ascii=False)
    return '<html><body><p>واحدها در جدول ذکر شده‌اند</p><script>' + prefix + 'var datasource = ' + data + ';' + suffix + '</script></body></html>'


@pytest.mark.parametrize('value,expected', [('۰', '0'), ('۱۲۳', '123'), ('۱٬۲۳۴٫۵', '1234.5'),
    ('(1,234)', '-1234'), ('+12.50', '12.50'), ('', None), ('-', None), ('1,23', None),
    ('1e3', None), ('12%', None), ('NaN', None), (None, None), (True, None), ('1 234', None)])
def test_numbers_remain_unitless_decimal_candidates_with_explicit_unknowns(value, expected):
    assert numeric_candidate(value) == expected


def test_extract_keeps_headers_scales_periods_zero_and_unknown_separate():
    output = extract_monthly_tables(html())
    cells = output['tables'][0]['cells']
    assert cells[0]['raw_value'] == 'مبلغ فروش (میلیون ریال)'
    assert cells[1]['raw_value'] == 'نرخ فروش (ریال)'
    assert cells[2]['numeric_candidate'] == '0' and cells[3]['numeric_candidate'] is None
    assert cells[4]['numeric_candidate'] == '1234.5' and cells[5]['numeric_candidate'] is None
    assert output['numeric_candidate_count'] == 2 and output['cell_count'] == 6
    assert output['report']['period_end_jalali'] == '1403/09/30'
    assert output['report']['is_consolidated'] is False
    assert not output['units_and_period_columns_verified'] and not output['accepted_for_features']
    assert not output['accepted_for_signals'] and not output['formulas_executed']


def test_javascript_comments_and_strings_do_not_become_datasource_and_formulas_never_execute():
    prefix = '// var datasource = evil();\n/* var datasource = 0; */\nconst note = "var datasource = false;";\n'
    raw = raw_report()
    raw['sheets'][0]['tables'][0]['cells'][3]['formula'] = 'fetch("https://evil.test")'
    output = extract_monthly_tables(html(raw, prefix=prefix, suffix='throw new Error("never execute");'))
    assert output['tables'][0]['cells'][3]['formula_present']
    assert output['tables'][0]['cells'][3]['numeric_candidate'] is None
    assert 'evil.test' not in json.dumps(output)


@pytest.mark.parametrize('mutation', ['type', 'period', 'duplicate_cell', 'duplicate_table', 'coordinate',
                                     'visibility', 'bad_date', 'no_sheets', 'nonobject_cell'])
def test_unsupported_or_inconsistent_structure_fails_closed(mutation):
    raw = raw_report()
    table = raw['sheets'][0]['tables'][0]
    if mutation == 'type':
        raw['type'] = 59
    elif mutation == 'period':
        raw['period'] = True
    elif mutation == 'duplicate_cell':
        table['cells'].append(copy.deepcopy(table['cells'][0]))
    elif mutation == 'duplicate_table':
        raw['sheets'][0]['tables'].append(copy.deepcopy(table))
    elif mutation == 'coordinate':
        table['cells'][0]['rowSequence'] = 1.5
    elif mutation == 'visibility':
        table['cells'][0]['isVisible'] = 'true'
    elif mutation == 'bad_date':
        raw['periodEndToDate'] = '1403/13/01'
    elif mutation == 'no_sheets':
        raw['sheets'] = []
    elif mutation == 'nonobject_cell':
        table['cells'][0] = None
    with pytest.raises(ValueError):
        extract_monthly_tables(html(raw))


@pytest.mark.parametrize('body', [
    '<html><body>var datasource = {}</body></html>',
    html(suffix='var datasource = {};'),
    html().replace('"tracingNo": 123', '"tracingNo": 123, "tracingNo": 999'),
    html().replace('var datasource = ', 'var datasource = JSON.parse('),
    html().replace(';</script>', ' || evil();</script>'),
    html().replace('"tracingNo": 123', '"tracingNo": NaN'),
    html(prefix='const pattern = /', suffix='/;'),
])
def test_non_json_duplicate_or_executable_initializer_rejected(body):
    with pytest.raises(ValueError):
        extract_monthly_tables(body)


def test_hidden_unknown_and_missing_values_are_preserved_without_fabricated_numbers():
    raw = raw_report()
    cells = raw['sheets'][0]['tables'][0]['cells']
    cells[2]['isVisible'] = False
    del cells[4]['value']
    del cells[5]['isVisible']
    output = extract_monthly_tables(html(raw))
    assert output['numeric_candidate_count'] == 0
    actual = output['tables'][0]['cells']
    assert actual[2]['raw_value'] == '۰' and actual[2]['visible'] is False
    assert not actual[4]['value_present'] and actual[4]['raw_value'] is None
    assert actual[5]['visible'] is None


def test_capture_archives_only_projected_cells_visible_text_and_code_without_backdating(tmp_path):
    api, session = provider(Response(html().replace('</body>', '<input name="__VIEWSTATE" value="SENSITIVE_SESSION_MARKER">'
        '<script>const password="SENSITIVE_SESSION_MARKER";</script></body>').encode()))
    result = capture_codal_tables(URL, 123, provider=api, root=tmp_path)
    loaded = load_codal_tables(result['folder'])
    assert result['status'] == 'captured' and result['cell_count'] == 6
    assert loaded['data']['numeric_candidate_count'] == 2
    assert datetime.fromisoformat(result['available_at']) >= datetime.fromisoformat(result['observed_at'])
    assert datetime.fromisoformat(result['available_at']).year > 2024
    assert not result['publication_time_verified'] and not result['numeric_facts_reviewed']
    assert len(session.calls) == 1
    for name in ('tables.json', 'report.txt'):
        assert 'SENSITIVE_SESSION_MARKER' not in (tmp_path / result['snapshot_id'] / name).read_text(encoding='utf-8')
    assert not list(tmp_path.rglob('*.html'))


@pytest.mark.parametrize('body,status', [('<html><body>CAPTCHA</body></html>', 'verification_required'),
                                       ('<html><body>بدون جدول</body></html>', 'error')])
def test_verification_or_missing_data_never_creates_success_archive(tmp_path, body, status):
    api, _ = provider(Response(body.encode()))
    result = capture_codal_tables(URL, 123, provider=api, root=tmp_path)
    assert result['status'] == status and not list(tmp_path.iterdir())


def test_wrong_tracing_number_rejected_before_publication(tmp_path):
    api, _ = provider(Response(html().encode()))
    with pytest.raises(ValueError, match='شماره پیگیری'):
        capture_codal_tables(URL, 999, provider=api, root=tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('path', ['tables.json', 'report.txt', 'code/core/codal_tables.py'])
def test_modified_data_or_archived_code_is_detected(tmp_path, path):
    from pathlib import Path
    api, _ = provider(Response(html().encode()))
    record = capture_codal_tables(URL, 123, provider=api, root=tmp_path)
    target = Path(record['folder']) / path
    target.write_bytes(target.read_bytes() + b'changed')
    with pytest.raises(ValueError, match='اثر انگشت'):
        load_codal_tables(record['folder'])


def test_cli_uses_explicit_tracing_private_archive_and_does_not_create_database(tmp_path, monkeypatch, capsys):
    api, _ = provider(Response(html().encode()))
    monkeypatch.setattr('data_collector.codal.CodalProvider', lambda: api)
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(settings, 'DB_PATH', tmp_path / 'must-not-exist.db')
    assert brs.main(['codal-tables', '--url', URL, '--expected-tracing-no', '123']) == 0
    assert len(list((tmp_path / 'private/codal-tables').glob('CODALTABLE-*/manifest.json'))) == 1
    assert not settings.DB_PATH.exists()
    output = json.loads(capsys.readouterr().out)
    assert output['accepted_for_features'] is False and output['numeric_facts_reviewed'] is False


def test_non_boolean_opt_in_is_rejected_before_network():
    api, session = provider()
    with pytest.raises(ValueError):
        api.get_document(URL, structured_tables='true')
    assert session.calls == []


@pytest.mark.parametrize('change', ['backdate', 'promotion', 'rehashed-number', 'traversal'])
def test_archive_cannot_backdate_promote_or_override_raw_values(tmp_path, change):
    import hashlib
    from pathlib import Path

    api, _ = provider(Response(html().encode()))
    record = capture_codal_tables(URL, 123, provider=api, root=tmp_path)
    folder = Path(record['folder'])
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    if change == 'backdate':
        manifest['available_at'] = '2024-01-01T12:00:00+03:30'
    elif change == 'traversal':
        manifest['files_sha256']['../outside'] = 'a' * 64
    else:
        data = json.loads((folder / 'tables.json').read_text(encoding='utf-8'))
        if change == 'promotion':
            data['accepted_for_features'] = True
        else:
            data['tables'][0]['cells'][2]['numeric_candidate'] = '999'
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        (folder / 'tables.json').write_bytes(body)
        manifest['files_sha256']['tables.json'] = hashlib.sha256(body).hexdigest()
    (folder / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(ValueError):
        load_codal_tables(folder)
