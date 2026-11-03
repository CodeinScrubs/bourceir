"""Synthetic interrupted batches live only in pytest's temporary directory."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

HELPER = (Path(__file__).resolve().parents[1] / 'data/private/rights-open-study-2026-10-31'
          / 'capture-all-registration-notices-v4.py')
pytestmark = pytest.mark.skipif(not HELPER.is_file(), reason='Private census helper is not installed')


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('private_rights_census_v4', HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.settings, 'DATA_DIR', tmp_path/'data')
    root = module.settings.DATA_DIR/'private/rights-open-study-2026-10-31'
    health = module.settings.DATA_DIR/'private/source-health/codal-documents'
    docs = module.settings.DATA_DIR/'private/codal-documents'
    specs = [
        ('REG-CENSUS-BATCH-1e17ad5cde0247bf930b60d5c53004bf',
         'REG-CENSUS-TIMEOUT-REVIEW-dc2ebc6fb10d4310a397d79ebe6c1e01',
         'resolved-stop-430693971ad492c3.json', 72, 'ReadTimeout',
         '430693971ad492c3f513a47428c0bc69265f748a9d5f9332a18df73aed1b37a8',
         'registration-timeout-resolution-v1', '02'),
        ('REG-CENSUS-BATCH-c65f35ec274d4a739d94ddedb0a8a679',
         'REG-CENSUS-CONNECTION-REVIEW-d820c859c53f4a3b96818b46e973aaf3',
         'resolved-stop-81a021a8670c0770.json', 16, 'ConnectionError',
         '81a021a8670c0770913fc555ac2453e814b5d4a69997500d2450dd87d58a428d',
         'registration-connection-resolution-v1', '03'),
    ]
    originals, manifests, paths = {}, {}, []
    for batch_name, review_name, stop_name, number, error, digest, version, hour in specs:
        batch, review, stop = root/batch_name, root/review_name, health/stop_name
        doc = docs/('DOC-'+hour)
        selected = [dict(tracing_no=n, url=f'https://codal.ir/{hour}/{n}') for n in range(1, 101)]
        _write(batch/'plan.json', dict(selected=selected))
        for n in range(1, number):
            _write(batch/f'intent-{n:02d}.json', {})
            _write(batch/f'receipt-{n:02d}.json', dict(status='captured', tracing_no=n,
                                                       stopped_at='2026-01-01T01:00:00+03:30'))
        intent, original = batch/f'intent-{number:02d}.json', batch/f'receipt-{number:02d}.json'
        _write(intent, {'index': number})
        _write(original, dict(status='error', error_type=error, http_status=None))
        originals[original] = digest
        _write(review/'plan.json', dict(original_receipt_sha256=digest))
        _write(review/'intent.json', {'index': number})
        _write(review/'receipt.json', dict(status='captured', tracing_no=number,
            source_url=selected[number-1]['url'], captured_folder=str(doc), content_sha256='public-'+hour,
            stopped_at=f'2026-01-01T{hour}:00:00+03:30'))
        _write(stop, {'blocked': True})
        _write(doc/'manifest.json', {'manifest': hour})
        manifests[doc] = {'source_url': selected[number-1]['url'], 'content_sha256': 'public-'+hour}
        plan = dict(version=version, original_stop_sha256=_sha(stop), batch_plan_sha256=_sha(batch/'plan.json'),
            original_intent_sha256=_sha(intent), original_receipt_sha256=digest,
            retry_plan_sha256=_sha(review/'plan.json'), retry_intent_sha256=_sha(review/'intent.json'),
            retry_receipt_sha256=_sha(review/'receipt.json'), document_manifest_sha256=_sha(doc/'manifest.json'))
        _write(review/'resolution-plan.json', plan)
        _write(review/'resolution-result.json', dict(version=version, status='released',
            plan_sha256=_sha(review/'resolution-plan.json'), archived_stop_sha256=_sha(stop)))
        paths.append(dict(batch=batch, review=review, stop=stop, document=doc, original=original, number=number))
    real_sha = module.sha
    monkeypatch.setattr(module, 'sha', lambda path: originals.get(Path(path), real_sha(path)))
    monkeypatch.setattr(module, 'load_codal_document', lambda path: manifests[Path(path)])
    return module, root, paths


def test_two_resolutions_preserve_both_incomplete_tails(evidence):
    module, root, paths = evidence
    assert module.reviewed_timeout(root)['batch'] == paths[0]['batch'].resolve()
    assert module.reviewed_connection(root)['batch'] == paths[1]['batch'].resolve()
    assert module.prior_batch_state(root).isoformat() == '2026-01-01T03:00:00+03:30'


@pytest.mark.parametrize('which', [0, 1])
@pytest.mark.parametrize('target', ['retry', 'manifest', 'stop', 'original'])
def test_tampering_either_resolution_is_rejected(evidence, which, target):
    module, root, paths = evidence
    item = paths[which]
    selected = {'retry': item['review']/'receipt.json', 'manifest': item['document']/'manifest.json',
                'stop': item['stop'], 'original': item['original']}[target]
    _write(selected, {'changed': True})
    with pytest.raises((ValueError, KeyError)):
        module.prior_batch_state(root)


@pytest.mark.parametrize('which', [0, 1])
def test_unexpected_tail_attempt_in_either_batch_is_rejected(evidence, which):
    module, root, paths = evidence
    item = paths[which]
    _write(item['batch']/f"intent-{item['number']+1:02d}.json", {'unexpected': True})
    with pytest.raises(ValueError, match='interrupted batch tail'):
        module.prior_batch_state(root)


@pytest.mark.parametrize('which', [0, 1])
def test_any_other_failure_remains_blocking(evidence, which):
    module, root, paths = evidence
    _write(paths[which]['batch']/'receipt-03.json', {'status': 'error', 'tracing_no': 3})
    with pytest.raises(ValueError, match='prior document failure'):
        module.prior_batch_state(root)


def test_same_batch_cannot_resolve_two_failures(evidence, monkeypatch):
    module, root, paths = evidence
    monkeypatch.setattr(module, 'reviewed_connection', lambda _: dict(batch=paths[0]['batch'].resolve()))
    with pytest.raises(ValueError, match='same batch'):
        module.prior_batch_state(root)
