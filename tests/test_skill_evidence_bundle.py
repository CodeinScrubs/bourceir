"""Synthetic review ZIPs stay in temporary test folders."""

import hashlib
import importlib.util
import json
import stat
from pathlib import Path
from zipfile import ZIP_STORED, BadZipFile, ZipFile, ZipInfo

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / '.agents/skills/brs-evidence-check/scripts/verify_bundle.py'


@pytest.fixture
def checker():
    spec = importlib.util.spec_from_file_location('brs_skill_bundle_check', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bundle(tmp_path, payload=None, *, extra=None, manifest=None):
    payload = {'data.txt': b'archived text'} if payload is None else payload
    hashes = {name: hashlib.sha256(body).hexdigest() for name, body in payload.items()}
    path = tmp_path / 'review.zip'
    with ZipFile(path, 'w', compression=ZIP_STORED) as archive:
        for name, body in {**payload, **(extra or {})}.items():
            archive.writestr(name, body)
        archive.writestr('manifest.json', json.dumps(hashes) if manifest is None else manifest)
    return path


def test_complete_bundle_is_verified_without_extraction_or_code_execution(tmp_path, checker):
    marker = tmp_path / 'executed.txt'
    code = f'from pathlib import Path\nPath({str(marker)!r}).write_text("executed")'.encode()
    path = bundle(tmp_path, {'payload.py': code, 'START-HERE-FA.md': b'cover note'})
    original = path.read_bytes()
    result = checker.verify_bundle(path)
    assert result['status'] == 'verified_complete' and result['listed_hashes_verified'] == 2
    assert result['archive_sha256'] == hashlib.sha256(original).hexdigest()
    assert not result['source_authenticity_verified'] and not result['profitability_verified']
    assert path.read_bytes() == original and not marker.exists()
    assert list(tmp_path.iterdir()) == [path]


def test_unhashed_cover_note_is_explicit_and_strict_mode_is_not_success(tmp_path, checker, capsys):
    path = bundle(tmp_path, extra={'START-HERE-FA.md': b'not inventoried'})
    result = checker.verify_bundle(path)
    assert result['status'] == 'verified_partial_coverage'
    assert result['unhashed_payload_files'] == ['START-HERE-FA.md']
    assert result['crc_verified'] and not result['manifest_coverage_complete']
    assert checker.main([str(path), '--require-complete']) == 2
    assert json.loads(capsys.readouterr().out)['status'] == 'verified_partial_coverage'


def test_rehashed_zip_crc_does_not_hide_manifest_payload_tampering(tmp_path, checker):
    path = bundle(tmp_path, manifest=json.dumps({'data.txt': '0' * 64}))
    with pytest.raises(ValueError, match='SHA-256 mismatch'):
        checker.verify_bundle(path)


@pytest.mark.parametrize('manifest', [
    '{}', '{"version":"bundle-v2","files":{}}', '{"data.txt":"invalid"}',
    '{"missing.txt":"' + '0' * 64 + '"}',
    '{"manifest.json":"' + '0' * 64 + '"}',
])
def test_unsupported_incomplete_or_self_referential_manifest_fails(tmp_path, checker, manifest):
    path = bundle(tmp_path, manifest=manifest)
    with pytest.raises(ValueError):
        checker.verify_bundle(path)


def test_duplicate_manifest_key_fails_even_if_hashes_agree(tmp_path, checker):
    digest = hashlib.sha256(b'archived text').hexdigest()
    manifest = '{"data.txt":"' + digest + '","data.txt":"' + digest + '"}'
    path = bundle(tmp_path, manifest=manifest)
    with pytest.raises(ValueError, match='Duplicate manifest'):
        checker.verify_bundle(path)


def test_duplicate_zip_entry_is_not_resolved_by_choosing_one_copy(tmp_path, checker):
    path = bundle(tmp_path)
    with ZipFile(path, 'a') as archive, pytest.warns(UserWarning):
        archive.writestr('data.txt', b'archived text')
    with pytest.raises(ValueError, match='Duplicate archive'):
        checker.verify_bundle(path)


@pytest.mark.parametrize('name', ['../escape.txt', '/absolute.txt', 'C:/drive.txt', 'a\\b.txt', 'a//b.txt'])
def test_noncanonical_or_escaping_names_are_rejected(tmp_path, checker, name):
    path = bundle(tmp_path, {name: b'payload'})
    with pytest.raises(ValueError, match='entry name'):
        checker.verify_bundle(path)


def test_symlink_payload_is_not_followed(tmp_path, checker):
    path = bundle(tmp_path)
    with ZipFile(path, 'a') as archive:
        link = ZipInfo('link')
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, '../data.txt')
    with pytest.raises(ValueError, match='Symlink'):
        checker.verify_bundle(path)


def test_crc_is_checked_for_unhashed_files_too(tmp_path, checker):
    marker = b'COVER-UNHASHED-12345'
    path = bundle(tmp_path, extra={'cover.txt': marker})
    raw = path.read_bytes()
    assert raw.count(marker) == 1
    path.write_bytes(raw.replace(marker, b'X' + marker[1:]))
    with pytest.raises(BadZipFile):
        checker.verify_bundle(path)


def test_declared_member_bound_is_checked_before_read(tmp_path, checker, monkeypatch):
    path = bundle(tmp_path, {'large.txt': b'x' * 200})
    monkeypatch.setattr(checker, 'MAX_MEMBER_BYTES', 100)
    with pytest.raises(ValueError, match='member limit'):
        checker.verify_bundle(path)


def test_output_is_exclusive_and_outside_project_reports_are_rejected(tmp_path, checker, monkeypatch, capsys):
    path = bundle(tmp_path)
    fake_root = tmp_path / 'repo'
    monkeypatch.setattr(checker, '__file__', str(fake_root / '.agents/skills/skill/scripts/check.py'))
    report = fake_root / 'data/private/check.json'
    report.parent.mkdir(parents=True)
    assert checker.main([str(path), '--output', str(report)]) == 0
    original = report.read_bytes()
    with pytest.raises(FileExistsError):
        checker.main([str(path), '--output', str(report)])
    assert report.read_bytes() == original
    with pytest.raises(SystemExit) as error:
        checker.main([str(path), '--output', str(tmp_path / 'outside.json')])
    assert error.value.code == 2 and not (tmp_path / 'outside.json').exists()
    assert json.loads(capsys.readouterr().out.splitlines()[0])['status'] == 'verified_complete'


def test_directory_payload_cannot_hide_from_crc_verification(tmp_path, checker):
    path = bundle(tmp_path)
    with ZipFile(path, 'a') as archive:
        archive.writestr('directory/', b'hidden payload')
    with pytest.raises(ValueError, match='Directory entries'):
        checker.verify_bundle(path)
