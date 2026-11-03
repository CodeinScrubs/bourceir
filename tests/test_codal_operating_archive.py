"""A derived financial receipt must remain independent of the mutable original input."""

import hashlib
import json
from pathlib import Path

import pytest

from config import settings
from core.codal_tables import numeric_candidate
from ops import codal_operating as operating
from ops.codal_tables import ARCHIVE_VERSION, SOURCE_FILES, UNVERIFIED
from ops.report_files import read_private_report, write_private_report
from tests.test_codal_operating import source
from tests.test_codal_revenue import TIMES


def sha(body):
    return hashlib.sha256(body).hexdigest()


@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path/'data')
    monkeypatch.setattr(settings, 'DB_PATH', tmp_path/'financial-account.db')
    monkeypatch.setattr(operating, 'now_tehran_iso', lambda: '2024-09-24T10:00:00+03:30')
    folder = settings.DATA_DIR/'private/codal-tables'/('CODALTABLE-'+'a'*32)
    folder.mkdir(parents=True)
    data = source()
    data.update(dict.fromkeys(UNVERIFIED, False))
    cells = data['tables'][0]['cells']
    for cell in cells:
        cell['numeric_candidate'] = numeric_candidate(cell['raw_value']) if cell['visible'] is True and cell['data_type'] in ('Integer','Float') else None
    data.update(cell_count=len(cells), table_count=1,
        numeric_candidate_count=sum(c['numeric_candidate'] is not None for c in cells))
    bodies = {'tables.json': json.dumps(data, ensure_ascii=False).encode(), 'report.txt': b'public test report'}
    bodies.update({'code/'+name: b'# preserved old source; not executable by review\n' for name in SOURCE_FILES})
    for name, body in bodies.items():
        path = folder/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(body)
    url = 'https://codal.ir/Reports/Decision.aspx?LetterSerial=test-public&let=58'
    manifest = dict(version=ARCHIVE_VERSION, snapshot_id=folder.name, source_url=url, effective_url=url,
        requested_at=TIMES['observed_at'], observed_at=TIMES['observed_at'], archived_at=TIMES['archived_at'],
        available_at=TIMES['available_at'], expected_tracing_no=123,
        files_sha256={name:sha(body) for name,body in bodies.items()},
        cell_count=data['cell_count'], table_count=1, numeric_candidate_count=data['numeric_candidate_count'],
        **dict.fromkeys(UNVERIFIED,False))
    write_private_report(folder/'manifest.json', manifest)
    return folder


def rebind(folder, name):
    manifest = read_private_report(folder/'manifest.json')
    manifest['files_sha256'][name] = sha((folder/name).read_bytes())
    write_private_report(folder/'manifest.json', manifest)


def test_capture_and_review_keep_exact_inputs_units_and_new_derived_availability(archive, monkeypatch):
    monkeypatch.setattr('requests.sessions.Session.request', lambda *a, **k: pytest.fail('No network'))
    result = operating.capture_operating_report(archive)
    folder = Path(result['folder']); before = result['result']
    assert result['status'] == 'observed' and result['exact_source_replay']
    assert before['source_available_at'] == TIMES['available_at']
    assert before['available_at'] == before['calculated_at'] == '2024-09-24T10:00:00+03:30'
    (archive/'tables.json').write_bytes(b'original later unavailable')
    reviewed = operating.review_operating_report(folder)
    assert reviewed['result'] == before and reviewed['network_requests'] == 0
    assert not settings.DB_PATH.exists()


@pytest.mark.parametrize('field', ['quantity', 'available_at', 'features'])
def test_rebound_result_cannot_invent_quantity_backdate_availability_or_upgrade_eligibility(archive, field):
    folder = Path(operating.capture_operating_report(archive)['folder'])
    result = read_private_report(folder/'result.json')
    if field == 'quantity':
        result['metrics']['rows'][0]['base_quantity']['numerator'] = '999999'
    elif field == 'available_at':
        result['available_at'] = TIMES['available_at']
    else:
        result['metrics']['accepted_for_features'] = True
    write_private_report(folder/'result.json', result); rebind(folder, 'result.json')
    with pytest.raises(ValueError, match='differs'):
        operating.review_operating_report(folder)


def test_backdated_calculation_cannot_use_an_older_source_timestamp_as_actual_availability(archive):
    folder = Path(operating.capture_operating_report(archive)['folder'])
    result = read_private_report(folder/'result.json')
    result['calculated_at'] = result['available_at'] = '2000-01-01T00:00:00+03:30'
    write_private_report(folder/'result.json', result); rebind(folder, 'result.json')
    with pytest.raises(ValueError, match='backdated'):
        operating.review_operating_report(folder)


def test_extra_file_is_not_part_of_a_verified_public_only_report(archive):
    folder = Path(operating.capture_operating_report(archive)['folder'])
    (folder/'unbound.txt').write_text('not an approved public archive member')
    with pytest.raises(ValueError, match='unbound'):
        operating.review_operating_report(folder)


@pytest.mark.parametrize('name', ['code/core/codal_operating.py', 'result.json',
    'input/CODALTABLE-'+'a'*32+'/tables.json'])
def test_changed_preserved_code_result_or_source_is_rejected(archive, name):
    folder = Path(operating.capture_operating_report(archive)['folder'])
    (folder/name).write_bytes((folder/name).read_bytes()+b' ')
    with pytest.raises(ValueError, match='fingerprint'):
        operating.review_operating_report(folder)


def test_old_future_timestamp_and_output_budget_fail_before_creating_a_valid_report(archive, monkeypatch):
    monkeypatch.setattr(operating, 'now_tehran_iso', lambda: '2000-01-01T00:00:00+03:30')
    with pytest.raises(ValueError, match='precedes'):
        operating.capture_operating_report(archive)
    monkeypatch.setattr(operating, 'now_tehran_iso', lambda: '2024-09-24T10:00:00+03:30')
    monkeypatch.setattr(operating, 'MAX_RESULT_BYTES', 1)
    with pytest.raises(ValueError, match='byte budget'):
        operating.capture_operating_report(archive)
    assert not (settings.DATA_DIR/'private/codal-operating').exists()


def test_cli_exposes_capture_and_offline_review_without_account_or_api(archive, capsys):
    from brs import build_parser

    args = build_parser().parse_args(['codal-operating', '--archive', str(archive)])
    assert args.func(args) == 0
    result = json.loads(capsys.readouterr().out)
    args = build_parser().parse_args(['codal-operating', '--archive', result['folder'], '--review'])
    assert args.func(args) == 0
    assert not json.loads(capsys.readouterr().out)['accepted_for_signals']
    assert not settings.DB_PATH.exists()


def test_input_cannot_point_at_unrelated_or_account_paths(archive):
    with pytest.raises(ValueError, match='designated'):
        operating.capture_operating_report(settings.DB_PATH)
