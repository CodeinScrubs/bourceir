"""No real network, account, database or live report is used by these tests."""

import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest

from ops.codal_financial_archive import capture_financial_statement, load_codal_financial_archive

URL = 'https://codal.ir/Reports/Decision.aspx?LetterSerial=example'


def response():
    return dict(status='captured', format='text', content='گزارش آزمایشی'.encode(),
                source_url=URL, effective_url=URL, requested_at='2025-01-01T10:00:00+03:30',
                observed_at='2025-01-01T10:00:01+03:30', response_sha256='a' * 64, response_bytes=123,
                financial_data=dict(version='codal-financial-cells-v1', report=dict(tracing_no=123, type=6),
                    tables=[dict(sheet_code=1, table_code=1, cells=[dict(raw_value=0, raw_value_kind='integer', value_present=True, address='A1', row=1,
                                            column=1, address_matches_sequence=True)])],
                    table_count=1, cell_count=1, address_sequence_mismatch_count=0,
                    numeric_facts_reviewed=False, units_and_period_columns_verified=False,
                    document_identity_verified=False, publication_time_verified=False,
                    formulas_executed=False, accepted_for_features=False, accepted_for_signals=False),
                financial_sheets=dict(version='codal-financial-sheets-v1', status='not_present', sheets=[],
                    selected_sheet_id=None, selection_status='unmarked'))


class Provider:
    def __init__(self, result):
        self.result, self.calls = result, []

    def get_document(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.result


def test_archive_preserves_zero_and_observation_time_without_promoting_facts(tmp_path):
    api = Provider(response())
    capture = capture_financial_statement(URL, 123, provider=api, root=tmp_path)
    loaded = load_codal_financial_archive(capture['folder'])
    assert api.calls == [(URL, {'financial_statement': True})]
    assert loaded['data']['tables'][0]['cells'][0]['raw_value'] == 0
    assert datetime.fromisoformat(capture['available_at']) >= datetime.fromisoformat(capture['observed_at'])
    assert datetime.fromisoformat(capture['available_at']) >= datetime.fromisoformat(capture['archived_at'])
    assert capture['publication_time_verified'] is False
    assert capture['historical_availability_verified'] is False
    assert capture['accepted_for_features'] is False
    assert not list(tmp_path.rglob('*.html'))


@pytest.mark.parametrize('result', [dict(status='verification_required'), dict(status='error')])
def test_failed_source_is_not_published(tmp_path, result):
    assert capture_financial_statement(URL, 123, provider=Provider(result), root=tmp_path) == result
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('change', ['trace', 'url', 'time', 'hash', 'promotion', 'count', 'sheet'])
def test_bad_evidence_is_rejected_before_archive(tmp_path, change):
    result = response()
    if change == 'trace':
        result['financial_data']['report']['tracing_no'] = 124
    elif change == 'url':
        result['effective_url'] = 'https://example.com/Reports/Decision.aspx'
    elif change == 'time':
        result['observed_at'] = '2024-01-01T10:00:01+03:30'
    elif change == 'hash':
        result['response_sha256'] = 'bad'
    elif change == 'promotion':
        result['financial_data']['accepted_for_features'] = True
    elif change == 'count':
        result['financial_data']['cell_count'] = 2
    else:
        result['financial_sheets']['selected_sheet_id'] = 12
    with pytest.raises(ValueError):
        capture_financial_statement(URL, 123, provider=Provider(result), root=tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('change', ['file', 'backdate', 'rehash_promotion', 'traversal'])
def test_archive_detects_modification_or_promotion(tmp_path, change):
    capture = capture_financial_statement(URL, 123, provider=Provider(response()), root=tmp_path)
    folder = Path(capture['folder'])
    manifest_path = folder / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if change == 'file':
        (folder / 'financial.json').write_text('{}', encoding='utf-8')
    elif change == 'backdate':
        manifest['available_at'] = '2024-01-01T00:00:00+03:30'
    elif change == 'traversal':
        manifest['files_sha256']['../file'] = 'a' * 64
    else:
        path = folder / 'financial.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        data['accepted_for_signals'] = True
        body = json.dumps(data).encode()
        path.write_bytes(body)
        manifest['files_sha256']['financial.json'] = hashlib.sha256(body).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError):
        load_codal_financial_archive(folder)


@pytest.mark.parametrize('change', ['count', 'cell_flag', 'missing_count', 'boolean_row', 'missing_cell'])
def test_rehashed_coordinate_quality_claims_are_recomputed(tmp_path, change):
    capture = capture_financial_statement(URL, 123, provider=Provider(response()), root=tmp_path)
    folder = Path(capture['folder'])
    data_path, manifest_path = folder / 'financial.json', folder / 'manifest.json'
    data = json.loads(data_path.read_text(encoding='utf-8'))
    cell = data['tables'][0]['cells'][0]
    if change == 'count':
        data['address_sequence_mismatch_count'] = 99
    elif change == 'cell_flag':
        cell['address_matches_sequence'] = False
    elif change == 'missing_count':
        del data['address_sequence_mismatch_count']
    elif change == 'boolean_row':
        cell['row'] = True
    else:
        del cell['address']
    body = json.dumps(data, ensure_ascii=False).encode('utf-8')
    data_path.write_bytes(body)
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['files_sha256']['financial.json'] = hashlib.sha256(body).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError, match='coordinate'):
        load_codal_financial_archive(folder)


@pytest.mark.parametrize('change', ['absent_with_value', 'false_present', 'wrong_kind',
                                     'boolean_as_integer', 'nan_decimal', 'missing_raw'])
def test_rehashed_value_type_or_presence_conflict_is_rejected(tmp_path, change):
    capture = capture_financial_statement(URL, 123, provider=Provider(response()), root=tmp_path)
    folder = Path(capture['folder'])
    data_path, manifest_path = folder / 'financial.json', folder / 'manifest.json'
    data = json.loads(data_path.read_text(encoding='utf-8'))
    cell = data['tables'][0]['cells'][0]
    if change == 'absent_with_value':
        cell.update(value_present=False, raw_value_kind='absent')
    elif change == 'false_present':
        cell['value_present'] = False
    elif change == 'wrong_kind':
        cell['raw_value_kind'] = 'string'
    elif change == 'boolean_as_integer':
        cell['raw_value'] = False
    elif change == 'nan_decimal':
        cell.update(raw_value='NaN', raw_value_kind='decimal')
    else:
        del cell['raw_value']
    body = json.dumps(data, ensure_ascii=False).encode('utf-8')
    data_path.write_bytes(body)
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['files_sha256']['financial.json'] = hashlib.sha256(body).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError, match='value metadata'):
        load_codal_financial_archive(folder)


@pytest.mark.parametrize('change', ['duplicate_table', 'duplicate_address', 'duplicate_coordinates'])
def test_rehashed_duplicate_table_or_cell_identity_is_rejected(tmp_path, change):
    capture = capture_financial_statement(URL, 123, provider=Provider(response()), root=tmp_path)
    folder = Path(capture['folder'])
    data_path, manifest_path = folder / 'financial.json', folder / 'manifest.json'
    data = json.loads(data_path.read_text(encoding='utf-8'))
    table = data['tables'][0]
    if change == 'duplicate_table':
        data['tables'].append(copy.deepcopy(table))
        data['table_count'] += 1
    else:
        cell = copy.deepcopy(table['cells'][0])
        if change == 'duplicate_address':
            cell.update(row=2, column=2, address_matches_sequence=False)
            data['address_sequence_mismatch_count'] += 1
        else:
            cell.update(address='B2', address_matches_sequence=False)
            data['address_sequence_mismatch_count'] += 1
        table['cells'].append(cell)
    data['cell_count'] += 1
    body = json.dumps(data, ensure_ascii=False).encode('utf-8')
    data_path.write_bytes(body)
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['files_sha256']['financial.json'] = hashlib.sha256(body).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError, match='Duplicate financial'):
        load_codal_financial_archive(folder)


@pytest.mark.parametrize('present,kind,value', [
    (False, 'absent', None), (True, 'null', None), (True, 'integer', 0),
    (True, 'string', '0'), (True, 'boolean', False), (True, 'decimal', '0.00'),
])
def test_distinct_valid_raw_values_are_retained(tmp_path, present, kind, value):
    result = response()
    result['financial_data']['tables'][0]['cells'][0].update(
        value_present=present, raw_value_kind=kind, raw_value=value)
    capture = capture_financial_statement(URL, 123, provider=Provider(result), root=tmp_path)
    cell = load_codal_financial_archive(capture['folder'])['data']['tables'][0]['cells'][0]
    assert (cell['value_present'], cell['raw_value_kind'], cell['raw_value']) == (present, kind, value)


def test_invalid_trace_or_url_is_rejected_before_provider(tmp_path):
    api = Provider(response())
    with pytest.raises(ValueError):
        capture_financial_statement(URL, True, provider=api, root=tmp_path)
    with pytest.raises(ValueError):
        capture_financial_statement('https://example.com/Reports/Decision.aspx', 123, provider=api, root=tmp_path)
    assert not api.calls


def test_provider_projects_static_financial_json_and_omits_hidden_state(tmp_path):
    from tests.test_codal_documents import Response, provider
    from tests.test_codal_tables import html, raw_report

    raw = raw_report()
    raw['type'] = 6
    body = html(raw).replace('</body>', '<input name="__VIEWSTATE" value="private-marker"></body>')
    api, session = provider(Response(body.encode('utf-8')))
    result = capture_financial_statement(URL, 123, provider=api, root=tmp_path)
    loaded = load_codal_financial_archive(result['folder'])
    assert loaded['data']['cell_count'] == 6
    assert loaded['data']['tables'][0]['cells'][2]['raw_value'] == '۰'
    assert loaded['sheets']['status'] == 'not_present'
    assert len(session.calls) == 1
    assert 'private-marker' not in (Path(result['folder']) / 'report.txt').read_text(encoding='utf-8')


def test_provider_challenge_never_creates_archive(tmp_path):
    from tests.test_codal_documents import Response, provider

    api, session = provider(Response('<html><body>CAPTCHA</body></html>'.encode()))
    result = capture_financial_statement(URL, 123, provider=api, root=tmp_path)
    assert result['status'] == 'verification_required'
    assert len(session.calls) == 1
    assert not list(tmp_path.iterdir())


def test_provider_preserves_signed_selector_ids_as_unverified_observations(tmp_path):
    from tests.test_codal_documents import Response, provider
    from tests.test_codal_tables import html, raw_report

    raw = raw_report()
    raw['type'] = 6
    options = ('<select id="ddlTable"><option value="-1" selected>نامشخص</option>'
               '<option value="1">صورت مالی</option></select>')
    body = html(raw).replace('</body>', options + '</body>')
    api, _ = provider(Response(body.encode('utf-8')))
    result = capture_financial_statement(URL, 123, provider=api, root=tmp_path)
    loaded = load_codal_financial_archive(result['folder'])
    assert loaded['sheets']['selected_sheet_id'] == -1
    assert loaded['sheets']['sheets'][0]['sheet_id'] == -1
    assert loaded['manifest']['document_identity_verified'] is False


@pytest.mark.parametrize('selected_id', [True, False])
def test_boolean_selected_sheet_id_cannot_alias_numeric_id(tmp_path, selected_id):
    result = response()
    result['financial_sheets'].update(status='observed', selection_status='selected',
        selected_sheet_id=selected_id,
        sheets=[dict(sheet_id=int(selected_id), selected=True, title='گزارش')])
    with pytest.raises(ValueError, match='selected sheet id'):
        capture_financial_statement(URL, 123, provider=Provider(result), root=tmp_path)
    assert not list(tmp_path.iterdir())


def sheet_response(requested, selected, payload_code, *, effective=None):
    result = response()
    result['source_url'] = requested
    result['effective_url'] = effective or requested
    result['financial_data']['tables'][0]['sheet_code'] = payload_code
    if selected is not None:
        result['financial_sheets'].update(status='observed', selection_status='selected',
            selected_sheet_id=selected, sheets=[dict(sheet_id=selected, selected=True, title='گزارش')])
    return result


@pytest.mark.parametrize('selected,payload', [(1, 1), (0, 1)])
def test_explicit_sheet_contradiction_is_rejected(tmp_path, selected, payload):
    requested = URL + '&sheetId=0'
    with pytest.raises(ValueError, match='conflict'):
        capture_financial_statement(requested, 123,
            provider=Provider(sheet_response(requested, selected, payload)), root=tmp_path)
    assert not list(tmp_path.iterdir())


def test_sheet_id_consistency_is_recorded_without_verifying_financial_identity(tmp_path):
    requested = URL + '&sheetId=0'
    result = capture_financial_statement(requested, 123,
        provider=Provider(sheet_response(requested, 0, 0)), root=tmp_path)
    assert result['source_sheet_id'] == 0 and result['effective_sheet_id'] == 0
    assert result['sheet_consistency'] == 'consistent_unverified'
    assert result['document_identity_verified'] is False
    assert load_codal_financial_archive(result['folder'])['manifest']['sheet_consistency'] == 'consistent_unverified'


def test_sheet_with_unmarked_selector_remains_unknown(tmp_path):
    requested = URL + '&sheetId=0'
    result = capture_financial_statement(requested, 123,
        provider=Provider(sheet_response(requested, None, 0)), root=tmp_path)
    assert result['sheet_consistency'] == 'unknown'
    assert result['document_identity_verified'] is False


def test_unmarked_selector_does_not_hide_wrong_payload_sheet(tmp_path):
    requested = URL + '&sheetId=0'
    with pytest.raises(ValueError, match='conflicts with payload'):
        capture_financial_statement(requested, 123,
            provider=Provider(sheet_response(requested, None, 1)), root=tmp_path)
    assert not list(tmp_path.iterdir())


def test_implicit_request_does_not_hide_selected_payload_contradiction(tmp_path):
    with pytest.raises(ValueError, match='Selected sheet ID conflicts'):
        capture_financial_statement(URL, 123,
            provider=Provider(sheet_response(URL, 1, 0)), root=tmp_path)
    assert not list(tmp_path.iterdir())


def test_selected_sheet_with_missing_payload_codes_remains_unknown(tmp_path):
    requested = URL + '&sheetId=0'
    result = sheet_response(requested, 0, 0)
    del result['financial_data']['tables'][0]['sheet_code']
    capture = capture_financial_statement(requested, 123, provider=Provider(result), root=tmp_path)
    assert capture['sheet_consistency'] == 'unknown'


@pytest.mark.parametrize('query', ['sheetId=0&SHEETID=1', '%73heetId=0&sheetid=0',
                                  'sheetId=', 'sheetId=+1', 'sheetId=01', 'sheetId=-0',
                                  'sheetId=1.0', 'sheetId=10000000000'])
def test_ambiguous_sheet_request_fails_before_network(tmp_path, query):
    api = Provider(response())
    with pytest.raises(ValueError, match='sheetId'):
        capture_financial_statement(URL + '&' + query, 123, provider=api, root=tmp_path)
    assert not api.calls and not list(tmp_path.iterdir())


def test_redirect_cannot_change_explicit_sheet_request(tmp_path):
    requested, effective = URL + '&sheetId=0', URL + '&sheetId=1'
    with pytest.raises(ValueError, match='conflict'):
        capture_financial_statement(requested, 123,
            provider=Provider(sheet_response(requested, 0, 0, effective=effective)), root=tmp_path)
    assert not list(tmp_path.iterdir())


def test_rehashed_sheet_evidence_cannot_falsely_promote_consistency(tmp_path):
    requested = URL + '&sheetId=0'
    capture = capture_financial_statement(requested, 123,
        provider=Provider(sheet_response(requested, None, 0)), root=tmp_path)
    path = Path(capture['folder']) / 'manifest.json'
    manifest = json.loads(path.read_text(encoding='utf-8'))
    manifest['sheet_consistency'] = 'consistent_unverified'
    path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError, match='evidence'):
        load_codal_financial_archive(capture['folder'])
