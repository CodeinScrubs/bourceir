"""Compact public CODAL snapshots into one lossless, searchable local catalog.

This module never reads broker accounts and does not publish features or signals.
The ordinary project's trading database is not modified.
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import uuid
import zlib
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from config import settings
from core.codal_documents import public_document_url
from core.codal_notice_summary import summarize_codal_notice
from core.text import normalize_fa, to_en_digits
from core.timeutils import now_tehran

VERSION = 'codal-compact-catalog-v1'
APP_ID = 0x42525343
SCHEMA_VERSION = 1
MAX_FILE = 25*1024*1024
SNAPSHOT = re.compile(r'CODAL-[a-f0-9]{32}\Z')


def sha(body):
    return hashlib.sha256(body).hexdigest()


def _reject_reparse(path):
    """Never follow a symlink or Windows junction while moving user data."""
    path = Path(path)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise ValueError('Symlinks and Windows reparse points are not accepted')
    return info


def _safe_path(root, relative, *, must_exist=True):
    root = Path(root)
    _reject_reparse(root)
    relative = Path(relative)
    if (relative.is_absolute() or relative.drive or not relative.parts
            or any(part in ('', '.', '..') for part in relative.parts)):
        raise ValueError('A relative archive path is required')
    current = root
    for part in relative.parts:
        current = current/part
        if current.exists() or current.is_symlink():
            _reject_reparse(current)
        elif must_exist:
            raise FileNotFoundError(current)
    if not current.resolve(strict=False).is_relative_to(root.resolve()):
        raise ValueError('Archive paths must remain within the intended root')
    return current


def _read_regular(root, relative):
    path = _safe_path(root, relative)
    info = _reject_reparse(path)
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
        raise ValueError('Archive content must be a bounded regular file')
    body = path.read_bytes()
    if len(body) != info.st_size or len(body) > MAX_FILE:
        raise ValueError('Archive file changed while reading')
    return body


def catalog_path(root):
    return Path(root)/'_compact'/'notices.sqlite'


@contextmanager
def _catalog_lock(root):
    """Serialize catalog writers and removals; a stale lock requires inspection."""
    root = Path(root)
    _reject_reparse(root)
    directory = root/'_compact'
    directory.mkdir(exist_ok=True)
    _reject_reparse(directory)
    lock = directory/'.catalog.lock'
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, json.dumps({'pid': os.getpid(), 'started_at': now_tehran().isoformat()}).encode())
        os.fsync(descriptor)
        yield
    finally:
        os.close(descriptor)
        lock.unlink()


def _schema(connection):
    connection.executescript('''
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS catalog_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS blobs(
            digest TEXT PRIMARY KEY, raw_size INTEGER NOT NULL,
            codec TEXT NOT NULL CHECK(codec IN ('raw','zlib')), payload BLOB NOT NULL
        );
        CREATE TABLE IF NOT EXISTS snapshots(
            snapshot_id TEXT PRIMARY KEY, manifest_sha256 TEXT NOT NULL, source_url TEXT NOT NULL,
            observed_at TEXT NOT NULL, document_format TEXT NOT NULL,
            symbol TEXT, tracing_no INTEGER, title TEXT, summary_json TEXT NOT NULL,
            search_text TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS snapshot_files(
            snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id), name TEXT NOT NULL,
            digest TEXT NOT NULL REFERENCES blobs(digest), PRIMARY KEY(snapshot_id,name)
        );
        CREATE INDEX IF NOT EXISTS idx_codal_compact_symbol ON snapshots(symbol);
        CREATE INDEX IF NOT EXISTS idx_codal_compact_trace ON snapshots(tracing_no);
        CREATE INDEX IF NOT EXISTS idx_codal_compact_observed ON snapshots(observed_at);
    ''')
    connection.execute(f'PRAGMA application_id={APP_ID}')
    connection.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
    connection.execute('INSERT OR IGNORE INTO catalog_meta(key,value) VALUES(?,?)', ('version', VERSION))


def _open_catalog(path, *, writable):
    path = Path(path)
    existed = path.exists()
    if not writable and not path.is_file():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(str(path) if writable else path.resolve().as_uri()+'?mode=ro',
                                 uri=not writable, timeout=30)
    connection.execute('PRAGMA foreign_keys=ON')
    if writable:
        connection.execute('PRAGMA journal_mode=DELETE')
        connection.execute('PRAGMA synchronous=FULL')
        app_id = connection.execute('PRAGMA application_id').fetchone()[0]
        if (existed and app_id != APP_ID) or (not existed and app_id != 0):
            connection.close()
            raise ValueError('Unexpected SQLite application ID')
        if not existed:
            _schema(connection)
            connection.commit()
    if (connection.execute('PRAGMA application_id').fetchone()[0] != APP_ID
            or connection.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION
            or connection.execute("SELECT value FROM catalog_meta WHERE key='version'").fetchone() != (VERSION,)):
        connection.close()
        raise ValueError('Unexpected compact catalog version')
    return connection


def _encode(body):
    if not isinstance(body, bytes) or len(body) > MAX_FILE:
        raise ValueError('Bounded bytes are required')
    compressed = zlib.compress(body, level=9)
    return ('zlib', compressed) if len(compressed) < len(body) else ('raw', body)


def _decode(codec, payload, size, expected):
    if (type(size) is not int or not 0 <= size <= MAX_FILE or not isinstance(payload, bytes)
            or not isinstance(expected, str) or not re.fullmatch(r'[a-f0-9]{64}', expected)):
        raise ValueError('Invalid compressed blob metadata')
    if codec == 'raw':
        body = payload
    elif codec == 'zlib':
        try:
            inflater = zlib.decompressobj()
            body = inflater.decompress(payload, size+1)
            if not inflater.eof or inflater.unused_data or inflater.unconsumed_tail:
                raise ValueError('Compressed blob is incomplete or has trailing data')
        except zlib.error as error:
            raise ValueError('Invalid compressed blob') from error
    else:
        raise ValueError('Unsupported compression')
    if len(body) != size or sha(body) != expected:
        raise ValueError('Archived bytes do not match their original hash')
    return body


def _notice_index(path):
    if path is None:
        return {}, None, None
    body = Path(path).read_bytes()
    source = json.loads(body.decode('utf-8'))
    notices = source.get('index', {}).get('notices')
    if not isinstance(notices, list):
        raise ValueError('A frozen public notice index is required')
    grouped = {}
    for row in notices:
        if not isinstance(row, dict) or not isinstance(row.get('url'), str):
            raise ValueError('Malformed public notice index')
        url = public_document_url(row['url'])
        grouped.setdefault(url, []).append(row)
    return grouped, sha(body), body


def _metadata(rows, url):
    candidates = rows.get(url, [])
    if len(candidates) != 1:
        return None
    row = candidates[0]
    keys = ('tracing_no', 'symbol_fa', 'company_name', 'title', 'url')
    if any(key not in row for key in keys) or not isinstance(row.get('source_published_at'), str):
        return None
    return {**{key: row[key] for key in keys}, 'published_at': row['source_published_at']}


def _search_value(summary):
    excerpt = '\n'.join(summary['excerpts'])
    text = ' '.join(str(summary.get(key) or '') for key in ('symbol', 'company_name', 'title', 'tracing_no'))
    return to_en_digits(normalize_fa(text+' '+excerpt)).replace('\u200c', ' ')


def _insert_blob(connection, body):
    digest = sha(body)
    codec, payload = _encode(body)
    connection.execute('INSERT OR IGNORE INTO blobs(digest,raw_size,codec,payload) VALUES(?,?,?,?)',
                       (digest, len(body), codec, payload))
    saved = connection.execute('SELECT raw_size,codec,payload FROM blobs WHERE digest=?', (digest,)).fetchone()
    if saved is None or _decode(saved[1], saved[2], saved[0], digest) != body:
        raise ValueError('The deduplicated blob does not round-trip exactly')
    return digest


def _expected_files(manifest):
    code = manifest.get('source_files_sha256')
    if not isinstance(code, dict) or not isinstance(manifest.get('content_file'), str):
        raise ValueError('Document manifest has an incomplete file inventory')
    return {manifest['content_file']: manifest['content_sha256'], **code}


def build_catalog(root, *, notice_index=None):
    """Create/update one catalog; no source file is removed by this function."""
    with _catalog_lock(root):
        return _build_catalog(root, notice_index=notice_index)


def _build_catalog(root, *, notice_index=None):
    from ops.codal_documents import load_codal_document

    _reject_reparse(Path(root))
    root = Path(root).resolve()
    _reject_reparse(root)
    compact_dir = root/'_compact'
    compact_dir.mkdir(exist_ok=True)
    _reject_reparse(compact_dir)
    target = catalog_path(root)
    if target.exists():
        _reject_reparse(target)
    rows, index_sha, index_body = _notice_index(notice_index)
    builder_bytes = Path(__file__).read_bytes()
    parser_path = settings.BASE_DIR/'core/codal_notice_summary.py'
    parser_bytes = parser_path.read_bytes().replace(b'\r\n', b'\n')
    summary_code, parser_code = sha(builder_bytes), sha(parser_bytes)
    cutoff = (now_tehran().date()+timedelta(days=1)).isoformat()
    errors, added, already = [], 0, 0
    connection = _open_catalog(target, writable=True)
    try:
        connection.execute('BEGIN IMMEDIATE')
        _insert_blob(connection, builder_bytes)
        _insert_blob(connection, parser_bytes)
        if index_body is not None:
            _insert_blob(connection, index_body)
        for manifest_path in sorted(root.glob('CODAL-*/manifest.json')):
            folder = manifest_path.parent
            if not SNAPSHOT.fullmatch(folder.name):
                errors.append(dict(snapshot=folder.name, error='invalid_snapshot_name'))
                continue
            connection.execute('SAVEPOINT one_snapshot')
            try:
                manifest_bytes = _read_regular(root, f'{folder.name}/manifest.json')
                old = connection.execute('SELECT manifest_sha256 FROM snapshots WHERE snapshot_id=?',
                                         (folder.name,)).fetchone()
                if old is not None:
                    if old[0] != sha(manifest_bytes):
                        raise ValueError('Existing catalog identity conflicts with the manifest')
                    already += 1
                    connection.execute('RELEASE one_snapshot')
                    continue
                manifest = load_codal_document(folder)
                if manifest['snapshot_id'] != folder.name:
                    raise ValueError('Document identity mismatch')
                contents = {'manifest.json': manifest_bytes}
                for name, expected in _expected_files(manifest).items():
                    body = _read_regular(root, f'{folder.name}/{name}')
                    if sha(body) != expected:
                        raise ValueError('Original file does not match its manifest')
                    contents[name] = body
                notice = _metadata(rows, manifest['source_url'])
                summary = summarize_codal_notice(manifest, contents[manifest['content_file']],
                                                 notice=notice, end_exclusive=cutoff)
                summary.update(source_bytes_recoverable=True, metadata_index_sha256=index_sha,
                               summary_builder_sha256=summary_code, summary_parser_sha256=parser_code)
                search = _search_value(summary)
                encoded_summary = json.dumps(summary, ensure_ascii=False, sort_keys=True, allow_nan=False)
                connection.execute('INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (folder.name, sha(manifest_bytes), manifest['source_url'], manifest['observed_at'],
                     manifest['format'], summary['symbol'], summary['tracing_no'], summary['title'],
                     encoded_summary, search))
                connection.execute('INSERT INTO catalog_meta(key,value) VALUES(?,?)',
                                   ('summary_sha256:'+folder.name, sha(encoded_summary.encode('utf-8'))))
                for name, body in contents.items():
                    digest = _insert_blob(connection, body)
                    connection.execute('INSERT INTO snapshot_files VALUES(?,?,?)', (folder.name, name, digest))
                if _read_regular(root, f'{folder.name}/manifest.json') != manifest_bytes:
                    raise ValueError('Manifest changed during compacting')
                connection.execute('RELEASE one_snapshot')
                added += 1
            except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as error:
                connection.execute('ROLLBACK TO one_snapshot')
                connection.execute('RELEASE one_snapshot')
                errors.append(dict(snapshot=folder.name, error=type(error).__name__, message=str(error)))
        if (Path(__file__).read_bytes() != builder_bytes
                or parser_path.read_bytes().replace(b'\r\n', b'\n') != parser_bytes):
            raise ValueError('Summary code changed during catalog creation')
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    checked = verify_catalog(root)
    return dict(version=VERSION, catalog=str(target), added=added, already_present=already, errors=errors,
                original_files_removed=False, catalog_bytes=target.stat().st_size, verification=checked)


def read_compacted_snapshot(root, snapshot_id):
    """Return a complete verified snapshot with one catalog connection."""
    if not isinstance(snapshot_id, str) or not SNAPSHOT.fullmatch(snapshot_id):
        raise ValueError('Invalid snapshot identity')
    _reject_reparse(Path(root))
    root = Path(root).resolve()
    _reject_reparse(root)
    _safe_path(root, '_compact/notices.sqlite')
    connection = _open_catalog(catalog_path(root), writable=False)
    try:
        metadata = connection.execute('''SELECT s.manifest_sha256,b.raw_size,b.codec,b.payload
            FROM snapshots s JOIN blobs b ON b.digest=s.manifest_sha256 WHERE s.snapshot_id=?''',
            (snapshot_id,)).fetchone()
        if metadata is None:
            raise ValueError('The compact snapshot is unavailable')
        manifest_bytes = _decode(metadata[2], metadata[3], metadata[1], metadata[0])
        manifest = json.loads(manifest_bytes.decode('utf-8'))
        inventory = {'manifest.json': metadata[0], **_expected_files(manifest)}
        if manifest.get('snapshot_id') != snapshot_id:
            raise ValueError('Snapshot does not match the original manifest')
        on_disk = root/snapshot_id/'manifest.json'
        if on_disk.exists() and sha(_read_regular(root, f'{snapshot_id}/manifest.json')) != metadata[0]:
            raise ValueError('The local manifest differs from the compact copy')
        rows = connection.execute('''SELECT f.name,f.digest,b.raw_size,b.codec,b.payload
            FROM snapshot_files f JOIN blobs b ON b.digest=f.digest
            WHERE f.snapshot_id=?''', (snapshot_id,)).fetchall()
        if {row[0]: row[1] for row in rows} != inventory:
            raise ValueError('The compacted file inventory differs from the original manifest')
        return {name: _decode(codec, payload, size, digest) for name, digest, size, codec, payload in rows}
    finally:
        connection.close()


def read_compacted_file(root, snapshot_id, name, *, expected=None):
    """Return verified exact bytes from the compact catalog; never fabricate content."""
    if not isinstance(name, str) or not name or '\\' in name or name.startswith('/') or '..' in Path(name).parts:
        raise ValueError('Invalid archive-relative file name')
    files = read_compacted_snapshot(root, snapshot_id)
    if name not in files:
        raise ValueError('Requested file is not in the original manifest')
    if expected is not None and sha(files[name]) != expected:
        raise ValueError('No matching compacted source file exists')
    return files[name]


def verify_catalog(root):
    """Recheck database structure, every blob, and each manifest's exact inventory."""
    _reject_reparse(Path(root))
    root = Path(root).resolve()
    _reject_reparse(root)
    _safe_path(root, '_compact/notices.sqlite')
    connection = _open_catalog(catalog_path(root), writable=False)
    try:
        if connection.execute('PRAGMA integrity_check').fetchone() != ('ok',):
            raise ValueError('SQLite integrity check failed')
        if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise ValueError('SQLite foreign-key check failed')
        documents = connection.execute('''SELECT snapshot_id,manifest_sha256,source_url,summary_json,
            symbol,tracing_no,title,search_text FROM snapshots''').fetchall()
        manifest_ids = {row[1] for row in documents}
        manifests, blob_ids, blob_count = {}, set(), 0
        for digest, size, codec, payload in connection.execute('SELECT digest,raw_size,codec,payload FROM blobs'):
            body = _decode(codec, payload, size, digest)
            if digest in manifest_ids:
                manifests[digest] = body
            blob_ids.add(digest)
            blob_count += 1
        files = {}
        for snapshot, name, digest in connection.execute('SELECT snapshot_id,name,digest FROM snapshot_files'):
            files.setdefault(snapshot, {})[name] = digest
        for snapshot, manifest_digest, source_url, summary_json, symbol, tracing_no, title, search_text in documents:
            if not SNAPSHOT.fullmatch(snapshot) or files.get(snapshot, {}).get('manifest.json') != manifest_digest:
                raise ValueError('Snapshot or manifest identity differs')
            manifest = json.loads(manifests[manifest_digest].decode('utf-8'))
            summary = json.loads(summary_json)
            summary_digest = connection.execute('SELECT value FROM catalog_meta WHERE key=?',
                                                ('summary_sha256:'+snapshot,)).fetchone()
            expected = {'manifest.json':manifest_digest, **_expected_files(manifest)}
            if (manifest.get('snapshot_id') != snapshot or manifest.get('source_url') != source_url
                    or public_document_url(source_url) != source_url or files[snapshot] != expected
                    or summary_digest != (sha(summary_json.encode('utf-8')),)
                    or summary.get('source_url') != source_url
                    or summary.get('symbol') != symbol or summary.get('tracing_no') != tracing_no
                    or summary.get('title') != title or _search_value(summary) != search_text
                    or summary.get('source_content_sha256') != manifest.get('content_sha256')
                    or summary.get('source_available_at') != manifest.get('available_at')
                    or summary.get('observed_at') != manifest.get('observed_at')
                    or summary.get('source_bytes_recoverable') is not True
                    or summary.get('summary_builder_sha256') not in blob_ids
                    or summary.get('summary_parser_sha256') not in blob_ids
                    or (summary.get('metadata_index_sha256') is not None
                        and summary['metadata_index_sha256'] not in blob_ids)
                    or summary.get('accepted_for_signals') is not False):
                raise ValueError('The compact snapshot or summary does not match its original manifest')
        if set(files) != {row[0] for row in documents}:
            raise ValueError('Unexpected catalog file mappings')
        return dict(status='verified', snapshots=len(documents), unique_blobs=blob_count,
                    exact_content_and_code_recoverable=True, independent_source_authenticity_verified=False)
    finally:
        connection.close()


def _make_parent(root, relative):
    current = Path(root)
    for part in Path(relative).parts[:-1]:
        current = current/part
        if not current.exists():
            current.mkdir()
        _reject_reparse(current)
    return current


def _restore_snapshot(root, snapshot_id, files):
    restored = 0
    for name, body in files.items():
        relative = Path(snapshot_id)/name
        _safe_path(root, relative, must_exist=False)
        _make_parent(root, relative)
        target = _safe_path(root, relative, must_exist=False)
        if target.exists():
            if _read_regular(root, relative) != body:
                raise ValueError('Existing file conflicts with the compact archive')
            continue
        temporary = target.with_name(target.name+'.restore-'+uuid.uuid4().hex+'.tmp')
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_BINARY', 0), 0o600)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            # A hard link creates the destination atomically without overwriting it.
            os.link(temporary, target)
            temporary.unlink()
        except BaseException:
            if temporary.exists():
                temporary.unlink()
            raise
        if _read_regular(root, relative) != body:
            raise ValueError('Restored bytes do not match the catalog')
        restored += 1
    return restored


def restore_snapshot(root, snapshot_id):
    """Restore exact original files; conflicting existing files are never overwritten."""
    with _catalog_lock(root):
        files = read_compacted_snapshot(root, snapshot_id)
        return dict(snapshot_id=snapshot_id, restored_files=_restore_snapshot(root, snapshot_id, files),
                    source_bytes_verified=True)


def prune_catalog(root, *, allow_remove=False):
    """Remove loose files only after a complete, exact, recoverable catalog check."""
    if allow_remove is not True:
        raise ValueError('Explicit removal mode is required')
    from ops.codal_documents import load_codal_document
    from ops.report_files import write_json_report

    with _catalog_lock(root):
        root = Path(root).resolve()
        checked = verify_catalog(root)
        connection = _open_catalog(catalog_path(root), writable=False)
        try:
            snapshots = [row[0] for row in connection.execute('SELECT snapshot_id FROM snapshots ORDER BY snapshot_id')]
        finally:
            connection.close()
        # Full preflight before removing even one file. The manifest stays in place.
        for snapshot in snapshots:
            files = read_compacted_snapshot(root, snapshot)
            for name, body in files.items():
                relative = Path(snapshot)/name
                path = _safe_path(root, relative, must_exist=False)
                if path.exists() and _read_regular(root, relative) != body:
                    raise ValueError('Loose source differs from its recoverable catalog copy')
        receipt_path = root/'_compact'/('prune-receipt-'+uuid.uuid4().hex+'.json')
        write_json_report(receipt_path, dict(version=VERSION, status='planned', snapshots=len(snapshots),
            started_at=now_tehran().isoformat(), preflight=checked), forbidden_paths=(settings.DB_PATH,))
        removed = 0
        try:
            for snapshot in snapshots:
                files = read_compacted_snapshot(root, snapshot)
                for name, body in files.items():
                    if name == 'manifest.json':
                        continue
                    relative = Path(snapshot)/name
                    path = _safe_path(root, relative, must_exist=False)
                    if not path.exists():
                        continue
                    if _read_regular(root, relative) != body:
                        raise ValueError('Loose source changed before removal')
                    path.unlink()
                    removed += 1
                # Exercise the real project loader after every migrated snapshot.
                load_codal_document(root/snapshot)
            verify_catalog(root)
            receipt = dict(version=VERSION, status='complete', completed_at=now_tehran().isoformat(),
                snapshots=len(snapshots), removed_loose_files=removed, catalog_bytes=catalog_path(root).stat().st_size,
                original_content_recoverable=True, source_independently_verified=False,
                accepted_for_signals=False, preflight=checked)
            write_json_report(receipt_path, receipt, forbidden_paths=(settings.DB_PATH,))
        except BaseException:
            # Partial interruption is recoverable from the verified immutable blobs.
            for snapshot in snapshots:
                _restore_snapshot(root, snapshot, read_compacted_snapshot(root, snapshot))
            raise
        return {**receipt, 'receipt':str(receipt_path)}


def search_catalog(root, query, *, limit=30):
    if not isinstance(query, str) or not query.strip() or len(query) > 200 or type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('A bounded search query and result limit are required')
    needle = to_en_digits(normalize_fa(query.strip())).replace('\u200c', ' ')
    needle = needle.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    _reject_reparse(Path(root))
    root = Path(root).resolve()
    _safe_path(root, '_compact/notices.sqlite')
    connection = _open_catalog(catalog_path(root), writable=False)
    try:
        rows = connection.execute("SELECT snapshot_id,summary_json,source_url FROM snapshots WHERE search_text LIKE ? ESCAPE '\\' "
            'ORDER BY observed_at DESC LIMIT ?', ('%'+needle+'%', limit)).fetchall()
        results = []
        for snapshot, raw_summary, source_url in rows:
            digest = connection.execute('SELECT value FROM catalog_meta WHERE key=?',
                                        ('summary_sha256:'+snapshot,)).fetchone()
            summary = json.loads(raw_summary)
            if (digest != (sha(raw_summary.encode('utf-8')),)
                    or summary.get('source_url') != source_url or public_document_url(source_url) != source_url
                    or summary.get('accepted_for_signals') is not False):
                raise ValueError('Compact summary verification failed')
            files = read_compacted_snapshot(root, snapshot)
            manifest = json.loads(files['manifest.json'].decode('utf-8'))
            if (summary.get('source_content_sha256') != manifest.get('content_sha256')
                    or summary.get('source_available_at') != manifest.get('available_at')
                    or summary.get('source_bytes_recoverable') is not True):
                raise ValueError('Summary source evidence does not match the original snapshot')
            results.append(dict(snapshot_id=snapshot, summary=summary))
        return results
    finally:
        connection.close()


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    build = sub.add_parser('build')
    build.add_argument('--notice-index', type=Path)
    sync = sub.add_parser('sync')
    sync.add_argument('--notice-index', type=Path)
    sync.add_argument('--apply', action='store_true', required=True)
    sub.add_parser('verify')
    prune = sub.add_parser('prune')
    prune.add_argument('--apply', action='store_true', required=True)
    restore = sub.add_parser('restore')
    restore.add_argument('snapshot_id')
    search = sub.add_parser('search')
    search.add_argument('query')
    search.add_argument('--limit', type=int, default=30)
    args = parser.parse_args(argv)
    root = settings.DATA_DIR/'private/codal-documents'
    try:
        if args.command in ('build', 'sync'):
            built = build_catalog(root, notice_index=args.notice_index)
            if args.command == 'sync' and not built['errors']:
                result = dict(build=built, removal=prune_catalog(root, allow_remove=args.apply))
            else:
                result = built
        elif args.command == 'verify':
            result = verify_catalog(root)
        elif args.command == 'prune':
            result = prune_catalog(root, allow_remove=args.apply)
        elif args.command == 'restore':
            result = restore_snapshot(root, args.snapshot_id)
        else:
            result = search_catalog(root, args.query, limit=args.limit)
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 1 if args.command in ('build', 'sync') and built['errors'] else 0


if __name__ == '__main__':
    sys.exit(main())
