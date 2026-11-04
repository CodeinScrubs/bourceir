"""All notices and archives in this module are synthetic and temporary."""

import hashlib
import json
import sqlite3
import zlib
from pathlib import Path

import pytest

from ops.codal_compact import _decode, build_catalog, catalog_path, read_compacted_file, search_catalog, verify_catalog
from ops.codal_documents import capture_codal_document
from tests.test_rights_registration import TEXT, URL, notice


class Provider:
    def __init__(self, body):
        self.body = body

    def get_document(self, url):
        return dict(status='captured', format='text', content=self.body, links=[], source_url=url, effective_url=url,
                    requested_at='2024-01-01T08:00:00+03:30', observed_at='2024-01-01T08:01:00+03:30',
                    response_sha256=hashlib.sha256(self.body).hexdigest(), response_bytes=len(self.body))


def make(root, url=URL, text=TEXT):
    return capture_codal_document(url, provider=Provider(text.encode('utf-8')), root=root)


def test_catalog_deduplicates_and_round_trips_exact_archives_without_deleting(tmp_path):
    root = tmp_path/'codal-documents'
    first = make(root)
    second = make(root, URL+'x')
    result = build_catalog(root)
    assert result['added'] == 2 and result['errors'] == []
    assert result['original_files_removed'] is False and result['verification']['status'] == 'verified'
    assert result['verification']['snapshots'] == 2
    assert result['verification']['unique_blobs'] < 2*9
    for captured in (first, second):
        folder = Path(captured['folder'])
        assert (folder/'report.txt').is_file()
        assert read_compacted_file(root, folder.name, 'report.txt') == (folder/'report.txt').read_bytes()
        for name, digest in captured['source_files_sha256'].items():
            assert read_compacted_file(root, folder.name, name, expected=digest) == (folder/name).read_bytes()
    assert build_catalog(root)['already_present'] == 2


def test_indexed_summary_is_searchable_and_remains_unverified(tmp_path):
    root = tmp_path/'codal-documents'
    first = make(root)
    row = notice()
    row['source_published_at'] = row.pop('published_at')
    index = tmp_path/'index.json'
    index.write_text(json.dumps({'index': {'notices': [row]}}, ensure_ascii=False), encoding='utf-8')
    result = build_catalog(root, notice_index=index)
    assert result['errors'] == []
    hits = search_catalog(root, 'آزمون')
    assert len(hits) == 1 and hits[0]['snapshot_id'] == Path(first['folder']).name
    summary = hits[0]['summary']
    assert summary['rights_registration']['claims_and_cash_rial'] == 400_000_000
    assert summary['source_bytes_recoverable'] is True
    assert summary['historical_identity_verified'] is False and summary['accepted_for_signals'] is False
    assert search_catalog(root, '۳۲۱')[0]['snapshot_id'] == hits[0]['snapshot_id']


def test_incorrect_hash_corrupt_blob_and_manifest_tampering_fail_closed(tmp_path):
    root = tmp_path/'codal-documents'
    captured = make(root)
    folder = Path(captured['folder'])
    build_catalog(root)
    with pytest.raises(ValueError, match='matching'):
        read_compacted_file(root, folder.name, 'report.txt', expected='0'*64)
    with pytest.raises(ValueError, match='not in the original'):
        read_compacted_file(root, folder.name, 'other.txt')
    with pytest.raises(ValueError):
        read_compacted_file(root, folder.name, '../secret')
    connection = sqlite3.connect(catalog_path(root))
    connection.execute('UPDATE blobs SET payload=? WHERE digest=?', (b'corrupt', captured['content_sha256']))
    connection.commit()
    connection.close()
    with pytest.raises(ValueError):
        verify_catalog(root)
    with pytest.raises(ValueError):
        read_compacted_file(root, folder.name, 'report.txt')
    assert (folder/'report.txt').read_bytes() == TEXT.encode('utf-8')


def test_unrelated_sqlite_database_is_never_overwritten(tmp_path):
    root = tmp_path/'codal-documents'
    make(root)
    path = catalog_path(root)
    path.parent.mkdir()
    connection = sqlite3.connect(path)
    connection.execute('CREATE TABLE personal(value TEXT)')
    connection.execute("INSERT INTO personal VALUES('untouched')")
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match='application ID'):
        build_catalog(root)
    connection = sqlite3.connect(path)
    assert connection.execute('SELECT value FROM personal').fetchall() == [('untouched',)]
    connection.close()


def test_decompression_requires_exact_length_hash_and_no_trailing_data():
    body = b'public notice'
    digest = hashlib.sha256(body).hexdigest()
    assert _decode('zlib', zlib.compress(body), len(body), digest) == body
    with pytest.raises(ValueError):
        _decode('zlib', zlib.compress(body)+b'junk', len(body), digest)
    with pytest.raises(ValueError):
        _decode('zlib', zlib.compress(body*1000), len(body), digest)
    with pytest.raises(ValueError):
        _decode('raw', body, len(body), '0'*64)


def test_ambiguous_index_metadata_is_not_guessed(tmp_path):
    root = tmp_path/'codal-documents'
    make(root)
    row = notice()
    row['source_published_at'] = row.pop('published_at')
    index = tmp_path/'index.json'
    index.write_text(json.dumps({'index': {'notices': [row, row]}}, ensure_ascii=False), encoding='utf-8')
    build_catalog(root, notice_index=index)
    summary = search_catalog(root, 'سرمایه')[0]['summary']
    assert summary['tracing_no'] is None and summary['review_reason'] == 'notice_metadata_missing'


def test_summary_tampering_is_detected_before_search_or_removal(tmp_path):
    root = tmp_path/'codal-documents'
    captured = make(root)
    build_catalog(root)
    connection = sqlite3.connect(catalog_path(root))
    connection.execute("UPDATE snapshots SET summary_json = '{}' WHERE snapshot_id=?", (Path(captured['folder']).name,))
    connection.commit()
    connection.close()
    with pytest.raises(ValueError):
        verify_catalog(root)
    with pytest.raises(ValueError):
        search_catalog(root, 'سرمایه')
    assert (Path(captured['folder'])/'report.txt').is_file()


def test_search_index_corruption_is_detected_by_catalog_verification(tmp_path):
    root = tmp_path/'codal-documents'
    make(root)
    build_catalog(root)
    connection = sqlite3.connect(catalog_path(root))
    connection.execute("UPDATE snapshots SET search_text='incorrect index'")
    connection.commit()
    connection.close()
    with pytest.raises(ValueError):
        verify_catalog(root)
