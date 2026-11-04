"""A migration must round-trip exact synthetic source files without breaking readers."""

import json
import sqlite3
from pathlib import Path

import pytest

from config import settings
from ops.capital_actions import _source
from ops.codal_compact import _restore_snapshot, build_catalog, catalog_path, prune_catalog, restore_snapshot
from ops.codal_documents import load_codal_document, read_codal_document_file
from ops.codal_storage_compat import COMPACT_SHA256, CURRENT, LEGACY, semantic_source_compatible
from ops.rights_registration import create_rights_study
from tests.test_codal_compact import Provider, make
from tests.test_rights_registration import TEXT, URL, notice


def test_lossless_pruning_preserves_loader_study_and_restore(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path/'data')
    root = settings.DATA_DIR/'private/codal-documents'
    captured = make(root)
    folder = Path(captured['folder'])
    before = {name: (folder/name).read_bytes() for name in
              ('manifest.json', captured['content_file'], *captured['source_files_sha256'])}
    build_catalog(root)
    result = prune_catalog(root, allow_remove=True)
    assert result['status'] == 'complete' and result['original_content_recoverable'] is True
    assert (folder/'manifest.json').is_file() and not (folder/'report.txt').exists()
    assert all(not (folder/name).exists() for name in captured['source_files_sha256'])
    assert load_codal_document(folder) == {key: value for key, value in captured.items()
                                            if key not in ('status', 'folder')}
    assert read_codal_document_file(folder, 'report.txt') == before['report.txt']
    assert _source(folder, notice())[1] == TEXT
    assert build_catalog(root)['already_present'] == 1
    notice_path = settings.DATA_DIR/'private/notice.json'
    notice_path.write_text(json.dumps(notice(), ensure_ascii=False), encoding='utf-8')
    study = create_rights_study(notice_path=notice_path, document=folder)
    assert study['facts']['status'] == 'supported_primary_fact'
    assert prune_catalog(root, allow_remove=True)['removed_loose_files'] == 0
    restored = restore_snapshot(root, folder.name)
    assert restored['restored_files'] == len(before)-1
    assert {name:(folder/name).read_bytes() for name in before} == before
    assert restore_snapshot(root, folder.name)['restored_files'] == 0


def test_corrupt_catalog_and_unapproved_removal_never_delete_loose_files(tmp_path):
    root = tmp_path/'codal-documents'
    captured = make(root)
    folder = Path(captured['folder'])
    build_catalog(root)
    with pytest.raises(ValueError, match='Explicit'):
        prune_catalog(root)
    connection = sqlite3.connect(catalog_path(root))
    connection.execute('UPDATE blobs SET payload=? WHERE digest=?', (b'bad', captured['content_sha256']))
    connection.commit()
    connection.close()
    with pytest.raises(ValueError):
        prune_catalog(root, allow_remove=True)
    assert (folder/'report.txt').read_bytes() == TEXT.encode('utf-8')


def test_interrupted_removal_restores_previously_deleted_files(tmp_path, monkeypatch):
    root = tmp_path/'codal-documents'
    captured = make(root)
    folder = Path(captured['folder'])
    before = {name:(folder/name).read_bytes() for name in
              (captured['content_file'], *captured['source_files_sha256'])}
    build_catalog(root)
    original_unlink = Path.unlink

    def fail_on_report(path, *args, **kwargs):
        if path.name == 'report.txt':
            raise OSError('synthetic sharing violation')
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'unlink', fail_on_report)
    with pytest.raises(OSError, match='sharing violation'):
        prune_catalog(root, allow_remove=True)
    assert {name:(folder/name).read_bytes() for name in before} == before


def test_missing_catalog_after_pruning_fails_closed(tmp_path):
    root = tmp_path/'codal-documents'
    captured = make(root, URL+'other')
    folder = Path(captured['folder'])
    build_catalog(root)
    prune_catalog(root, allow_remove=True)
    catalog_path(root).rename(catalog_path(root).with_suffix('.disabled'))
    with pytest.raises((OSError, ValueError)):
        load_codal_document(folder)


def test_changed_loose_source_is_not_removed_or_overwritten(tmp_path):
    root = tmp_path/'codal-documents'
    captured = make(root)
    folder = Path(captured['folder'])
    build_catalog(root)
    (folder/'report.txt').write_text('changed after archive', encoding='utf-8')
    with pytest.raises(ValueError, match='differs'):
        prune_catalog(root, allow_remove=True)
    assert (folder/'report.txt').read_text(encoding='utf-8') == 'changed after archive'
    with pytest.raises(ValueError, match='conflicts'):
        restore_snapshot(root, folder.name)
    assert (folder/'report.txt').read_text(encoding='utf-8') == 'changed after archive'


def test_pdf_bytes_remain_recoverable_without_invented_summary(tmp_path):
    from ops.codal_documents import capture_codal_document

    class PdfProvider(Provider):
        def get_document(self, url):
            return super().get_document(url) | {'format':'pdf'}

    root = tmp_path/'codal-documents'
    body = b'%PDF-1.4\n%%EOF'
    captured = capture_codal_document(URL, provider=PdfProvider(body), root=root)
    folder = Path(captured['folder'])
    build_catalog(root)
    prune_catalog(root, allow_remove=True)
    assert not (folder/'report.pdf').exists()
    assert read_codal_document_file(folder, 'report.pdf') == body
    assert load_codal_document(folder)['content_sha256'] == captured['content_sha256']


def test_restore_rejects_parent_traversal_before_creating_directories(tmp_path):
    root = tmp_path/'catalog-root'
    root.mkdir()
    with pytest.raises(ValueError):
        _restore_snapshot(root, 'CODAL-'+'a'*32, {'../../escaped/file.txt':b'bad'})
    assert not (tmp_path/'escaped').exists()


def test_legacy_compatibility_is_pinned_and_rejects_future_code_changes(tmp_path, monkeypatch):
    name = 'ops/codal_documents.py'
    assert semantic_source_compatible(name, LEGACY[name], CURRENT[name])
    assert not semantic_source_compatible(name, '0'*64, CURRENT[name])
    assert not semantic_source_compatible(name, LEGACY[name], '0'*64)
    assert not semantic_source_compatible('core/rights_evidence.py', '1'*64, '2'*64)
    assert semantic_source_compatible('any/exact.py', '1'*64, '1'*64)
    monkeypatch.setattr(settings, 'BASE_DIR', tmp_path)
    (tmp_path/'ops').mkdir()
    (tmp_path/'ops/codal_compact.py').write_text('modified', encoding='utf-8')
    assert COMPACT_SHA256 != '0'*64
    assert not semantic_source_compatible(name, LEGACY[name], CURRENT[name])
