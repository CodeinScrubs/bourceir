"""Replay synthetic source receipts offline, retaining vintage and revision limits."""

import hashlib
import json
from pathlib import Path

import pytest

from config import settings
from core.codal_tables import numeric_candidate
from ops import codal_monthly_vintage as vintage
from ops import codal_search as search
from ops.codal_tables import SOURCE_FILES, UNVERIFIED
from ops.report_files import read_private_report, write_private_report
from tests.test_codal_monthly_vintage import bound
from tests.test_codal_search import row
from tests.test_codal_search_archive import INSTRUMENT, Provider

DECISIONS = ['2024-09-24T12:00:00+03:30', '2024-09-26T12:00:00+03:30']


def sha(body):
    return hashlib.sha256(body).hexdigest()


def rebind(folder, name):
    manifest = read_private_report(folder/'manifest.json')
    manifest['files_sha256'][name] = sha((folder/name).read_bytes())
    write_private_report(folder/'manifest.json', manifest)


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path/'data')
    monkeypatch.setattr(settings, 'DB_PATH', tmp_path/'financial-account.db')
    monkeypatch.setattr(search.time, 'sleep', lambda _s: None)
    tables, native = [], []
    for correction in (False, True):
        loaded, letter, _body, text = bound(correction=correction)
        data, manifest = loaded['data'], loaded['manifest']
        folder = settings.DATA_DIR/'private/codal-tables'/('CODALTABLE-'+('b' if correction else 'a')*32)
        folder.mkdir(parents=True)
        data.update(dict.fromkeys(UNVERIFIED, False))
        cells = data['tables'][0]['cells']
        for cell in cells:
            cell['numeric_candidate'] = numeric_candidate(cell['raw_value']) if cell['visible'] is True and cell['data_type'] in ('Integer', 'Float') else None
        data.update(cell_count=len(cells), table_count=1,
            numeric_candidate_count=sum(c['numeric_candidate'] is not None for c in cells))
        files = {'tables.json': json.dumps(data, ensure_ascii=False).encode(), 'report.txt': text.encode()}
        files.update({'code/'+n: b'# synthetic preserved decoder; never executed\n' for n in SOURCE_FILES})
        for name, body in files.items():
            path = folder/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        manifest.update(version='codal-monthly-archive-v1', snapshot_id=folder.name,
            files_sha256={n: sha(b) for n, b in files.items()}, cell_count=len(cells), table_count=1,
            numeric_candidate_count=data['numeric_candidate_count'], **dict.fromkeys(UNVERIFIED, False))
        write_private_report(folder/'manifest.json', manifest)
        tables.append(folder)
        native.append(row(TracingNo=letter['tracing_no'], Symbol=letter['symbol_fa'], CompanyName=letter['company_name'],
            Title=letter['title'], LetterCode=letter['letter_code'], Url=letter['url'],
            PublishDateTime=data['report']['claimed_publish_time_jalali'], SentDateTime=None, PdfUrl=None))
    metadata = search.capture_search(Provider({1: native, 2: []}), INSTRUMENT,
        start='2024-09-01', end_exclusive='2024-10-01', max_pages=3)
    return Path(metadata['folder']), tables


def capture(evidence):
    source, tables = evidence
    return vintage.capture_monthly_vintage(source, tables, decisions=DECISIONS)


def test_offline_capture_and_exact_replay_preserve_all_original_evidence(evidence, monkeypatch):
    monkeypatch.setattr('requests.sessions.Session.request', lambda *_a, **_k: pytest.fail('No network'))
    result = capture(evidence)
    assert result['exact_source_replay'] and result['network_requests'] == 0
    before = result['result']
    for folder in [evidence[0], *evidence[1]]:
        (folder/'manifest.json').write_bytes(b'original later unavailable')
    reviewed = vintage.review_monthly_vintage(result['folder'])
    assert reviewed['result'] == before and not settings.DB_PATH.exists()
    assert len(before['versions']) == 2 and len(before['selections']) == 4
    first, diagnostic, later, _ = before['selections']
    assert first['selected_tracing_no'] == 123 and diagnostic['selected_tracing_no'] == 456
    assert later['selected_tracing_no'] == 456
    assert before['available_at'] == before['calculated_at']
    assert before['source_claim_clock_is_reconstruction_only']
    assert all(before[k] is False for k in vintage.FALSE_FLAGS)
    assert before['known_revision_inventory'] == dict(tracing_numbers=[123, 456],
        all_known_sources_captured=True,
        explicit_revision_links=[dict(older_tracing_no=123, newer_tracing_no=456)],
        bounded_search_is_complete_history=False)


@pytest.mark.parametrize('included', [0, 1])
def test_missing_known_original_or_correction_blocks_before_financial_cells(evidence, monkeypatch, included):
    source, tables = evidence
    monkeypatch.setattr(vintage, '_input', lambda *_a: pytest.fail('Incomplete revision history interpreted'))
    with pytest.raises(ValueError, match='Known monthly revision source missing'):
        vintage.capture_monthly_vintage(source, [tables[included]], decisions=DECISIONS)
    assert not (settings.DATA_DIR/'private/codal-monthly-vintage').exists()


@pytest.mark.parametrize('key', ['corrects_tracing_no', 'later_revision_banner_tracing_no'])
def test_unknown_explicit_revision_link_is_not_ignored(evidence, monkeypatch, key):
    real = vintage.monthly_version

    def linked(*args, **kwargs):
        version = real(*args, **kwargs)
        version[key] = 999
        return version

    monkeypatch.setattr(vintage, 'monthly_version', linked)
    with pytest.raises(ValueError, match='Explicit monthly revision source missing'):
        capture(evidence)
    assert not (settings.DATA_DIR/'private/codal-monthly-vintage').exists()


def test_explicit_revision_link_with_reversed_publication_order_is_rejected(evidence, monkeypatch):
    real = vintage.monthly_version

    def reversed_link(*args, **kwargs):
        version = real(*args, **kwargs)
        if version['tracing_no'] == 123:
            version['later_revision_banner_tracing_no'] = None
            version['corrects_tracing_no'] = 456
        return version

    monkeypatch.setattr(vintage, 'monthly_version', reversed_link)
    with pytest.raises(ValueError, match='chronology contradicts'):
        capture(evidence)
    assert not (settings.DATA_DIR/'private/codal-monthly-vintage').exists()


def test_explicit_reassessment_reports_changed_old_decoder_and_executes_none_of_it(evidence):
    folder, _ = evidence
    name = 'core/codal_documents.py'
    (folder/'code'/name).write_bytes(b"raise RuntimeError('old code must never execute')\n")
    plan = read_private_report(folder/'plan.json')
    plan['code_sha256'][name] = sha((folder/'code'/name).read_bytes())
    write_private_report(folder/'plan.json', plan)
    manifest = read_private_report(folder/'manifest.json')
    manifest['files'] = search._files(folder)
    write_private_report(folder/'manifest.json', manifest)
    receipt = capture(evidence)['result']['search_reassessment']
    assert receipt['changed_code_files'] == [name]
    assert receipt['original_code_matches_current'] is receipt['original_review_rerun'] is False
    assert receipt['archived_code_executed'] is False
    assert receipt['all_page_assessments_and_summary_equal_preserved']


@pytest.mark.parametrize('mutation', ['amount', 'availability', 'eligibility', 'selection', 'changed_code'])
def test_rehashed_outer_manifest_cannot_change_results_eligibility_or_current_decoder(evidence, mutation):
    folder = Path(capture(evidence)['folder'])
    name = 'result.json'
    result = read_private_report(folder/name)
    if mutation == 'amount':
        result['versions'][0]['metrics']['total']['amount_rial'] = '999999999'
    elif mutation == 'availability':
        result['calculated_at'] = result['available_at'] = '2000-01-01T00:00:00+03:30'
    elif mutation == 'eligibility':
        result['accepted_for_signals'] = True
    elif mutation == 'selection':
        result['selections'][0]['selected_tracing_no'] = 456
    else:
        name = 'code/core/codal_monthly_vintage.py'
        (folder/name).write_bytes(b'# altered decoder\n')
    if mutation != 'changed_code':
        write_private_report(folder/name, result)
    rebind(folder, name)
    with pytest.raises(ValueError):
        vintage.review_monthly_vintage(folder)


def test_rebound_native_result_cannot_replace_raw_publication_metadata(evidence):
    source, _ = evidence
    result = read_private_report(source/'result.json')
    result['summary']['letters'][0]['source_published_at'] = '2024-01-01T00:00:00+03:30'
    write_private_report(source/'result.json', result)
    manifest = read_private_report(source/'manifest.json')
    manifest['files'] = search._files(source)
    write_private_report(source/'manifest.json', manifest)
    with pytest.raises(ValueError, match='denominator'):
        capture(evidence)


def test_unmatched_or_sealed_metadata_blocks_before_numeric_table_loading(evidence, monkeypatch):
    _, tables = evidence
    manifest = read_private_report(tables[0]/'manifest.json')
    manifest['expected_tracing_no'] = 999
    write_private_report(tables[0]/'manifest.json', manifest)
    monkeypatch.setattr(vintage, '_input', lambda *_a: pytest.fail('Unscoped financial data interpreted'))
    with pytest.raises(ValueError, match='unsealed native search metadata'):
        capture(evidence)


def test_future_period_in_unsealed_metadata_is_rejected_before_reading_cells(evidence, monkeypatch):
    source, tables = evidence
    raw_file = source/'raw/page-001.json'
    payload = json.loads(raw_file.read_bytes())
    payload['Letters'][0]['Title'] = payload['Letters'][0]['Title'].replace('1403/06/31', '1404/06/31')
    body = json.dumps(payload, ensure_ascii=False).encode()
    raw_file.write_bytes(body)
    saved = read_private_report(source/'page-001.json')
    saved['observation'].update(body_sha256=sha(body), response_bytes=len(body))
    write_private_report(source/'page-001.json', saved)
    plan = read_private_report(source/'plan.json')
    cases = []
    result = read_private_report(source/'result.json')
    for index, case in enumerate(result['cases']):
        saved = read_private_report(source/case['observation_file'])
        raw = (source/case['body_file']).read_bytes() if case['body_file'] else None
        assessed = search._assessment(saved, raw, plan['requests'][index])
        write_private_report(source/case['assessment_file'], assessed)
        cases.append(dict(case, assessment=assessed))
    result['summary'] = search._summary(plan, cases)
    write_private_report(source/'result.json', result)
    manifest = read_private_report(source/'manifest.json')
    manifest['files'] = search._files(source)
    write_private_report(source/'manifest.json', manifest)
    monkeypatch.setattr(vintage, '_input', lambda *_a: pytest.fail('Sealed-period financial data interpreted'))
    with pytest.raises(ValueError, match='Monthly native period'):
        vintage.capture_monthly_vintage(source, tables, decisions=DECISIONS)


def test_extra_files_are_not_silently_added_to_the_evidence_receipt(evidence):
    folder = Path(capture(evidence)['folder'])
    (folder/'unbound.json').write_bytes(b'{}')
    with pytest.raises(ValueError, match='fingerprint'):
        vintage.review_monthly_vintage(folder)


def test_duplicate_tables_invalid_decisions_and_account_paths_fail_before_output(evidence):
    search_folder, tables = evidence
    for decisions in ([], [DECISIONS[0]]*2, ['2024-09-24T12:00:00'], ['2099-01-01T00:00:00+03:30']):
        with pytest.raises(ValueError):
            vintage.capture_monthly_vintage(search_folder, tables, decisions=decisions)
    with pytest.raises(ValueError):
        vintage.capture_monthly_vintage(search_folder, [tables[0]]*2, decisions=DECISIONS)
    with pytest.raises(ValueError, match='designated'):
        vintage.capture_monthly_vintage(settings.DB_PATH, tables, decisions=DECISIONS)
    assert not (settings.DATA_DIR/'private/codal-monthly-vintage').exists()


def test_cli_capture_and_offline_review_do_not_need_account_or_browser(evidence, capsys):
    source, tables = evidence
    args = ['capture', '--search', str(source)]
    for table in tables:
        args += ['--table', str(table)]
    for decision in DECISIONS:
        args += ['--decision-at', decision]
    vintage.main(args)
    result = json.loads(capsys.readouterr().out)
    vintage.main(['review', '--archive', result['folder']])
    assert json.loads(capsys.readouterr().out)['exact_source_replay']
    assert not settings.DB_PATH.exists()
