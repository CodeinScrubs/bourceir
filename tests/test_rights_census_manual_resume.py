"""Offline checks for an optional private, append-only census recovery helper."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

HELPER = (Path(__file__).resolve().parents[1] / 'data/private/rights-open-study-2026-10-31'
          / 'capture-all-registration-notices-v3.py')
RELEASE = HELPER.parent/'release-reviewed-codal-timeout-v1.py'
pytestmark = pytest.mark.skipif(not HELPER.is_file(), reason='Private census helper is not installed')
ORIGINAL = '430693971ad492c3f513a47428c0bc69265f748a9d5f9332a18df73aed1b37a8'


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('private_rights_census_v3', HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.settings, 'DATA_DIR', tmp_path/'data')
    root = module.settings.DATA_DIR/'private/rights-open-study-2026-10-31'
    batch = root/'REG-CENSUS-BATCH-1e17ad5cde0247bf930b60d5c53004bf'
    review = root/'REG-CENSUS-TIMEOUT-REVIEW-dc2ebc6fb10d4310a397d79ebe6c1e01'
    stop = module.settings.DATA_DIR/'private/source-health/codal-documents/resolved-stop-430693971ad492c3.json'
    doc = module.settings.DATA_DIR/'private/codal-documents/CODAL-test'
    selected = [dict(tracing_no=n, url=f'https://codal.ir/{n}') for n in range(1, 101)]
    _write(batch/'plan.json', dict(selected=selected))
    for n in range(1, 72):
        _write(batch/f'intent-{n:02d}.json', {})
        _write(batch/f'receipt-{n:02d}.json', dict(status='captured', tracing_no=n,
                                                   stopped_at='2026-01-01T01:00:00+03:30'))
    _write(batch/'intent-72.json', {'index': 72})
    _write(batch/'receipt-72.json', dict(status='error', error_type='ReadTimeout', http_status=None))
    _write(review/'plan.json', dict(original_receipt_sha256=ORIGINAL))
    _write(review/'intent.json', {'index': 72})
    _write(review/'receipt.json', dict(status='captured', tracing_no=72, source_url=selected[71]['url'],
        captured_folder=str(doc), content_sha256='public-content', stopped_at='2026-01-01T02:00:00+03:30'))
    _write(stop, {'blocked': True})
    _write(doc/'manifest.json', {'manifest': 'test'})
    monkeypatch.setattr(module, 'load_codal_document', lambda path: {
        'source_url': selected[71]['url'], 'content_sha256': 'public-content'})
    real_sha = module.sha
    monkeypatch.setattr(module, 'sha', lambda path: ORIGINAL if Path(path) == batch/'receipt-72.json'
                        else real_sha(path))
    plan = dict(version='registration-timeout-resolution-v1', original_stop_sha256=_digest(stop),
        batch_plan_sha256=_digest(batch/'plan.json'), original_intent_sha256=_digest(batch/'intent-72.json'),
        original_receipt_sha256=ORIGINAL, retry_plan_sha256=_digest(review/'plan.json'),
        retry_intent_sha256=_digest(review/'intent.json'), retry_receipt_sha256=_digest(review/'receipt.json'),
        document_manifest_sha256=_digest(doc/'manifest.json'))
    _write(review/'resolution-plan.json', plan)
    _write(review/'resolution-result.json', dict(version=plan['version'], status='released',
        plan_sha256=_digest(review/'resolution-plan.json'), archived_stop_sha256=_digest(stop)))
    return module, root, batch, review, stop, doc


def test_audited_timeout_keeps_unattempted_tail_pending(evidence):
    module, root, *_ = evidence
    assert module.reviewed_timeout(root)['stopped_at'] == '2026-01-01T02:00:00+03:30'
    assert module.prior_batch_state(root).isoformat() == '2026-01-01T02:00:00+03:30'


@pytest.mark.parametrize('target', ['retry', 'manifest', 'stop', 'original'])
def test_tampered_resolution_evidence_is_rejected(evidence, target):
    module, root, batch, review, stop, doc = evidence
    targets = {'retry': review/'receipt.json', 'manifest': doc/'manifest.json',
               'stop': stop, 'original': batch/'receipt-72.json'}
    _write(targets[target], {'changed': True})
    with pytest.raises((ValueError, KeyError)):
        module.reviewed_timeout(root)


def test_any_unrecorded_tail_attempt_stops_recovery(evidence):
    module, root, batch, *_ = evidence
    _write(batch/'intent-73.json', {'unexpected': True})
    with pytest.raises(ValueError, match='interrupted batch tail'):
        module.prior_batch_state(root)


def test_unresolved_prior_receipt_stops_recovery(evidence):
    module, root, batch, *_ = evidence
    _write(batch/'receipt-03.json', {'status': 'error', 'tracing_no': 3})
    with pytest.raises(ValueError, match='prior document failure'):
        module.prior_batch_state(root)


@pytest.fixture
def release_evidence(tmp_path, monkeypatch):
    if not RELEASE.is_file():
        pytest.skip('Private timeout release helper is not installed')
    spec = importlib.util.spec_from_file_location('private_rights_census_release', RELEASE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.settings, 'DATA_DIR', tmp_path/'data')
    root = module.settings.DATA_DIR/'private/rights-open-study-2026-10-31'
    batch = root/'REG-CENSUS-BATCH-1e17ad5cde0247bf930b60d5c53004bf'
    review = root/'REG-CENSUS-TIMEOUT-REVIEW-dc2ebc6fb10d4310a397d79ebe6c1e01'
    health = module.settings.DATA_DIR/'private/source-health/codal-documents'
    doc = module.settings.DATA_DIR/'private/codal-documents/CODAL-test'
    monkeypatch.setattr(module, 'ROOT', root)
    monkeypatch.setattr(module, 'BATCH', batch)
    monkeypatch.setattr(module, 'REVIEW', review)
    monkeypatch.setattr(module, 'HEALTH', health)
    selected = [dict(tracing_no=n, url=f'https://codal.ir/{n}') for n in range(1, 101)]
    _write(batch/'plan.json', dict(selected=selected, capture_code_sha256={'source': 'hash'}))
    _write(batch/'intent-72.json', {'index': 72})
    _write(batch/'receipt-72.json', dict(status='error', error_type='ReadTimeout', http_status=None))
    original = _digest(batch/'receipt-72.json')
    monkeypatch.setattr(module, 'EXPECTED_RECEIPT', original)
    _write(health/'manual-stop.json', dict(receipt_sha256=original, source_review_required=True,
        automatic_release=False, blocked=True, receipt=str(batch/'receipt-72.json')))
    _write(review/'plan.json', dict(original_receipt_sha256=original, maximum_document_attempts=1))
    _write(review/'intent.json', dict(plan_sha256=_digest(review/'plan.json')))
    _write(review/'receipt.json', dict(status='captured', tracing_no=72, source_url=selected[71]['url'],
        captured_folder=str(doc), content_sha256='public-content', plan_sha256=_digest(review/'plan.json'),
        intent_sha256=_digest(review/'intent.json')))
    _write(doc/'manifest.json', {'manifest': 'test'})
    monkeypatch.setattr(module, 'load_codal_document', lambda path: {
        'source_url': selected[71]['url'], 'content_sha256': 'public-content'})
    monkeypatch.setattr(module, '_code_fingerprints', lambda paths: {'source': 'hash'})
    monkeypatch.setattr(module, 'source_status', lambda: {'blocked': False})
    monkeypatch.setattr(module, 'no_collector', lambda: None)
    return module, batch, review, health


def test_release_archives_stop_and_keeps_original_failure(release_evidence):
    module, batch, review, health = release_evidence
    stop_hash = _digest(health/'manual-stop.json')
    failure_hash = _digest(batch/'receipt-72.json')
    module.main()
    assert not (health/'manual-stop.json').exists()
    assert _digest(health/'resolved-stop-430693971ad492c3.json') == stop_hash
    assert _digest(batch/'receipt-72.json') == failure_hash
    result = json.loads((review/'resolution-result.json').read_text(encoding='utf-8'))
    assert result['status'] == 'released'
    assert result['plan_sha256'] == _digest(review/'resolution-plan.json')


def test_failed_release_result_restores_stop(release_evidence, monkeypatch):
    module, _, review, health = release_evidence
    real_write = module.write_private_report

    def broken_write(path, payload, **kwargs):
        if Path(path) == review/'resolution-result.json':
            raise OSError('simulated interruption')
        return real_write(path, payload, **kwargs)

    monkeypatch.setattr(module, 'write_private_report', broken_write)
    with pytest.raises(OSError, match='simulated interruption'):
        module.main()
    assert (health/'manual-stop.json').exists()
    assert not (health/'resolved-stop-430693971ad492c3.json').exists()


def test_blocked_source_cannot_be_released(release_evidence, monkeypatch):
    module, _, review, health = release_evidence
    monkeypatch.setattr(module, 'source_status', lambda: {'blocked': True})
    with pytest.raises(ValueError, match='reviewed successful retry'):
        module.main()
    assert (health/'manual-stop.json').exists()
    assert not (review/'resolution-plan.json').exists()
