"""Bounded, resumable public-market capture for the queue-fall study.

The separate, versioned research archive contains no account tables. It is not
the application's database and does not migrate or modify it.
"""

import argparse
import gzip
import hashlib
import io
import json
import shutil
import sqlite3
import uuid
from pathlib import Path

from config import settings
from core.queue_fall_study import (
    MAX_BOOK_BYTES,
    MAX_THRESHOLD_BYTES,
    book_precheck,
    book_request,
    study_session,
    summarize_sessions,
    trade_observation,
    trade_request,
)
from core.quote_context import context_requests
from core.timeutils import now_tehran, now_tehran_iso
from data_collector.tsetmc import TsetmcProvider
from ops.report_files import write_private_report

VERSION = 'queue-fall-archive-v1'
BLOCK_SIZE = 1000
MIN_FREE_BYTES = 5 * 1024**3
MAX_ARCHIVE_BYTES = 16 * 1024**3
CODE_FILES = ('ops/queue_fall_archive.py', 'core/queue_fall_study.py', 'core/query_screener.py',
              'core/legacy_quotes.py', 'core/quote_context.py', 'core/source_cooldown.py',
              'core/timeutils.py', 'data_collector/tsetmc.py', 'data_collector/source_cooldown.py',
              'config/settings.py', 'config/default.yaml')
COLUMNS = ('symbol_id', 'symbol', 'ins_code', 'type_code', 'date', 'source', 'price_unit',
           'open_raw', 'high_raw', 'low_raw', 'close_raw', 'close_final_raw', 'ref_price_raw',
           'volume', 'value', 'trade_count', 'captured_at')


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def code_hashes():
    return {name: _digest((settings.BASE_DIR / name).read_bytes()) for name in CODE_FILES}


def _connect(path, *, readonly=False):
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError('An existing study archive is required')
    uri = path.as_uri() + ('?mode=ro' if readonly else '?mode=rw')
    conn = sqlite3.connect(uri, uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def _meta(conn):
    items = {r['key']: json.loads(r['value']) for r in conn.execute('SELECT key,value FROM meta')}
    if items.get('version') != VERSION or items.get('status') != 'ready':
        raise ValueError('Unknown or incomplete archive')
    return items


def create_archive(source, target, *, cutoff, fingerprints):
    """Freeze public daily rows from a read-only source into a new archive."""
    source, target = Path(source).resolve(), Path(target).resolve()
    if not source.is_file() or source == target or target.exists():
        raise ValueError('Existing source and new distinct target required')
    if cutoff != now_tehran().date().isoformat():
        raise ValueError('Cutoff must be today in Tehran; current partial session is excluded')
    if shutil.disk_usage(target.parent).free < MIN_FREE_BYTES:
        raise ValueError('Insufficient free disk space for a recoverable archive')
    target.touch(exist_ok=False)
    src = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=15)
    out = sqlite3.connect(target, timeout=15)
    try:
        out.executescript('''
            PRAGMA foreign_keys=ON;
            CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE sessions(seq INTEGER PRIMARY KEY,symbol_id INTEGER,symbol TEXT,ins_code TEXT,
              type_code INTEGER,date TEXT,source TEXT,price_unit TEXT,open_raw REAL,high_raw REAL,
              low_raw REAL,close_raw REAL,close_final_raw REAL,ref_price_raw REAL,volume INTEGER,
              value REAL,trade_count INTEGER,captured_at TEXT);
            CREATE TABLE blocks(block_no INTEGER PRIMARY KEY,first_seq INTEGER,last_seq INTEGER,sha256 TEXT NOT NULL);
            CREATE TABLE attempts(id INTEGER PRIMARY KEY,session_seq INTEGER NOT NULL REFERENCES sessions(seq),
              attempted_at TEXT NOT NULL,finished_at TEXT NOT NULL,status TEXT NOT NULL,
              metadata TEXT NOT NULL,book_gzip BLOB,threshold_gzip BLOB,trade_gzip BLOB,result TEXT NOT NULL);
            CREATE INDEX attempts_session ON attempts(session_seq,id);
            CREATE TRIGGER no_session_update BEFORE UPDATE ON sessions BEGIN SELECT RAISE(ABORT,'frozen sessions'); END;
            CREATE TRIGGER no_session_delete BEFORE DELETE ON sessions BEGIN SELECT RAISE(ABORT,'frozen sessions'); END;
            CREATE TRIGGER no_attempt_update BEFORE UPDATE ON attempts BEGIN SELECT RAISE(ABORT,'append only'); END;
            CREATE TRIGGER no_attempt_delete BEFORE DELETE ON attempts BEGIN SELECT RAISE(ABORT,'append only'); END;
            CREATE TRIGGER no_block_update BEFORE UPDATE ON blocks BEGIN SELECT RAISE(ABORT,'frozen blocks'); END;
            CREATE TRIGGER no_block_delete BEFORE DELETE ON blocks BEGIN SELECT RAISE(ABORT,'frozen blocks'); END;
            CREATE TRIGGER no_meta_update BEFORE UPDATE ON meta BEGIN SELECT RAISE(ABORT,'frozen metadata'); END;
            CREATE TRIGGER no_meta_delete BEFORE DELETE ON meta BEGIN SELECT RAISE(ABORT,'frozen metadata'); END;
        ''')
        src.execute('BEGIN')
        duplicates = src.execute('''SELECT COUNT(*) FROM (SELECT ins_code FROM symbols
            WHERE instrument_type_code IN (300,303) GROUP BY ins_code HAVING COUNT(*)>1)''').fetchone()[0]
        if duplicates:
            raise ValueError('Current registry has duplicate instrument identifiers')
        registry_count = src.execute('SELECT COUNT(*) FROM symbols WHERE instrument_type_code IN (300,303)').fetchone()[0]
        query = '''SELECT s.id,s.symbol_fa,s.ins_code,s.instrument_type_code,p.date,p.source,p.price_unit,
                    p.open_raw,p.high_raw,p.low_raw,p.close_raw,p.close_final_raw,p.ref_price_raw,
                    p.volume,p.value,p.trade_count,p.captured_at
                 FROM daily_prices p JOIN symbols s ON s.id=p.symbol_id
                 WHERE s.instrument_type_code IN (300,303) AND p.source='tsetmc' AND p.volume>0 AND p.date<?
                 ORDER BY p.date DESC,s.ins_code,s.id'''
        cursor = src.execute(query, (cutoff,))
        total, blocks, first_day, last_day = 0, [], None, None
        out.execute('BEGIN')
        while True:
            rows = cursor.fetchmany(BLOCK_SIZE)
            if not rows:
                break
            first_seq = total + 1
            body = b''.join((_json(list(row)) + '\n').encode('utf-8') for row in rows)
            total += len(rows)
            out.executemany('INSERT INTO sessions VALUES (' + ','.join('?' for _ in range(len(COLUMNS)+1)) + ')',
                            [(first_seq + i, *row) for i, row in enumerate(rows)])
            block = (len(blocks), first_seq, total, _digest(body))
            out.execute('INSERT INTO blocks VALUES (?,?,?,?)', block)
            blocks.append(block)
            first_day = first_day or rows[0][4]
            last_day = rows[-1][4]
        if not total:
            raise ValueError('No eligible public historical market sessions')
        manifest = dict(version=VERSION, status='ready', created_at=now_tehran_iso(), cutoff_exclusive=cutoff,
                        session_count=total, registry_count=registry_count, latest_date=first_day,
                        earliest_date=last_day, block_size=BLOCK_SIZE, blocks_sha256=_digest(_json(blocks).encode()),
                        code_sha256=fingerprints, market_only=True, historical_universe_complete=False,
                        source_completeness_verified=False, accepted_for_signals=False,
                        preopen_is_clock_bucket_only=True, current_partial_session_excluded=True,
                        archive_limit_bytes=MAX_ARCHIVE_BYTES, min_free_bytes=MIN_FREE_BYTES)
        out.executemany('INSERT INTO meta VALUES (?,?)', [(k, _json(v)) for k, v in manifest.items()])
        out.execute("CREATE TRIGGER no_session_insert BEFORE INSERT ON sessions BEGIN SELECT RAISE(ABORT,'frozen sessions'); END")
        out.commit()
        src.rollback()
        return manifest
    finally:
        src.close()
        out.close()


def _verify_block(conn, seq):
    block = conn.execute('SELECT * FROM blocks WHERE first_seq<=? AND last_seq>=?', (seq, seq)).fetchone()
    if block is None:
        raise ValueError('Missing frozen manifest block')
    rows = conn.execute('SELECT ' + ','.join(COLUMNS) + ' FROM sessions WHERE seq BETWEEN ? AND ? ORDER BY seq',
                        (block['first_seq'], block['last_seq'])).fetchall()
    body = b''.join((_json(list(row)) + '\n').encode('utf-8') for row in rows)
    if len(rows) != block['last_seq'] - block['first_seq'] + 1 or _digest(body) != block['sha256']:
        raise ValueError('Frozen manifest block changed')


def _verify_block_index(conn, meta):
    blocks = [tuple(row) for row in conn.execute(
        'SELECT block_no,first_seq,last_seq,sha256 FROM blocks ORDER BY block_no')]
    if not blocks or _digest(_json(blocks).encode()) != meta['blocks_sha256']:
        raise ValueError('Frozen manifest block index changed')
    expected = 1
    for index, first, last, _ in blocks:
        if index != (expected - 1) // BLOCK_SIZE or first != expected or last < first or last - first >= BLOCK_SIZE:
            raise ValueError('Frozen manifest block sequence is inconsistent')
        expected = last + 1
    if expected - 1 != meta['session_count']:
        raise ValueError('Frozen manifest count changed')


def _body(obs):
    if obs is None:
        return None, None
    body = obs.get('content')
    if not isinstance(body, bytes) or _digest(body) != obs.get('body_sha256'):
        raise ValueError('Capture returned inconsistent bytes')
    metadata = {key: value for key, value in obs.items() if key != 'content'}
    return metadata, gzip.compress(body, mtime=0)


def _not_observed(precheck):
    return dict(state='not_observed', observed=[], provisional=[], issues=precheck['issues'],
                pre_09_clock_observed=False, post_09_clock_observed=False,
                pre_09_clock_provisional=False, post_09_clock_provisional=False,
                no_event_proven=False, source_completeness_verified=False, accepted_for_signals=False)


def _restore(metadata, compressed, limit):
    if metadata is None and compressed is None:
        return None
    if not isinstance(metadata, dict) or not isinstance(compressed, bytes):
        raise ValueError('Incomplete captured source evidence')
    with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
        body = stream.read(limit + 1)
    if (len(body) > limit or len(body) != metadata.get('response_bytes')
            or _digest(body) != metadata.get('body_sha256')):
        raise ValueError('Captured source evidence changed')
    return dict(metadata, content=body)


def _daily(row):
    return {key: row[key] for key in COLUMNS if key in (
        'ins_code', 'date', 'source', 'price_unit', 'open_raw', 'high_raw', 'low_raw',
        'close_raw', 'close_final_raw', 'ref_price_raw', 'volume', 'value', 'trade_count')}


def _replay_attempt(row):
    metadata, result = json.loads(row['metadata']), json.loads(row['result'])
    if result.get('state') != row['status']:
        raise ValueError('Attempt status and result differ')
    source = metadata['observations']
    book = _restore(source.get('book'), row['book_gzip'], MAX_BOOK_BYTES)
    limits = _restore(source.get('threshold'), row['threshold_gzip'], MAX_THRESHOLD_BYTES)
    raw_tape = _restore(source.get('trade'), row['trade_gzip'], MAX_BOOK_BYTES)
    if row['status'] == 'unknown':
        return False  # Raw bodies are checked; a missing or failed source is not a negative observation.
    if book is None or limits is None:
        raise ValueError('A completed observation is missing its source evidence')
    precheck = book_precheck(book, limits, row['ins_code'], row['date'])
    if precheck['state'] == 'no_upper_bid_observed':
        rebuilt = _not_observed(precheck)
        if raw_tape is not None:
            raise ValueError('Unexpected trade tape for a screened session')
    elif precheck['state'] == 'tape_needed' and raw_tape is not None:
        tape = trade_observation(raw_tape, row['ins_code'], row['date'])
        rebuilt = study_session(book, limits, tape, _daily(row))
    else:
        raise ValueError('A completed observation no longer has consistent source evidence')
    rebuilt['prefetch_screen'] = precheck
    if rebuilt != result:
        raise ValueError('Stored research result differs from source replay')
    return True


def capture_batch(path, provider, *, max_sessions=5, retry_failed=False):
    if type(max_sessions) is not int or not 1 <= max_sessions <= 1000 or type(retry_failed) is not bool:
        raise ValueError('Explicit bounded batch and retry policy required')
    path = Path(path).resolve()
    conn = _connect(path)
    count, stop = 0, 'batch_complete'
    try:
        meta = _meta(conn)
        if meta['code_sha256'] != code_hashes():
            raise ValueError('Capture or interpretation code changed since the frozen plan')
        _verify_block_index(conn, meta)
        while count < max_sessions:
            if shutil.disk_usage(path.parent).free < meta['min_free_bytes'] or path.stat().st_size >= meta['archive_limit_bytes']:
                stop = 'storage_limit'; break
            if meta['code_sha256'] != code_hashes():
                stop = 'capture_code_changed'; break
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('''SELECT s.*,a.status AS previous_status FROM sessions s
                LEFT JOIN attempts a ON a.id=(SELECT MAX(id) FROM attempts WHERE session_seq=s.seq)
                WHERE a.id IS NULL OR (? AND a.status NOT IN ('observed','provisional','not_observed'))
                ORDER BY s.seq LIMIT 1''', (retry_failed,)).fetchone()
            if row is None:
                conn.rollback(); stop = 'cohort_complete'; break
            _verify_block(conn, row['seq'])
            started = now_tehran_iso()
            sources, blobs, transports = {}, {}, {}
            result = dict(state='unknown', issues=[], pre_09_clock_observed=False, post_09_clock_observed=False,
                          pre_09_clock_provisional=False, post_09_clock_provisional=False,
                          accepted_for_signals=False)
            try:
                code, day = row['ins_code'], row['date']
                urls = [('threshold', context_requests(code, day)[1], MAX_THRESHOLD_BYTES),
                        ('book', book_request(code, day), MAX_BOOK_BYTES)]
                observations = {}
                for kind, url, limit in urls:
                    obs = provider._get_quote_context_json(code, url, max_bytes=limit)
                    transports[kind] = provider.last_transport_evidence
                    sources[kind], blobs[kind] = _body(obs)
                    if obs is None:
                        raise ValueError('Public source unavailable or deferred: ' + kind)
                    observations[kind] = obs
                precheck = book_precheck(observations['book'], observations['threshold'], code, day)
                if precheck['state'] == 'unknown':
                    raise ValueError('Book or threshold could not be verified: ' + _json(precheck['issues']))
                if precheck['state'] == 'no_upper_bid_observed':
                    result = _not_observed(precheck)
                else:
                    obs = provider._get_quote_context_json(code, trade_request(code, day), max_bytes=MAX_BOOK_BYTES)
                    transports['trade'] = provider.last_transport_evidence
                    sources['trade'], blobs['trade'] = _body(obs)
                    if obs is None:
                        raise ValueError('Public trade tape unavailable or deferred')
                    tape = trade_observation(obs, code, day)
                    result = study_session(observations['book'], observations['threshold'], tape, _daily(row))
                result['prefetch_screen'] = precheck
            except (ValueError, TypeError, KeyError, OverflowError) as exc:
                result['issues'].append(str(exc))
            if meta['code_sha256'] != code_hashes():
                result = dict(state='unknown', issues=['Capture code changed before commit'],
                              pre_09_clock_observed=False, post_09_clock_observed=False,
                              pre_09_clock_provisional=False, post_09_clock_provisional=False,
                              accepted_for_signals=False)
            metadata = dict(observations=sources, transport=transports, historical_universe_complete=False)
            conn.execute('''INSERT INTO attempts(session_seq,attempted_at,finished_at,status,metadata,book_gzip,
                threshold_gzip,trade_gzip,result) VALUES (?,?,?,?,?,?,?,?,?)''',
                (row['seq'], started, now_tehran_iso(), result['state'], _json(metadata), blobs.get('book'),
                 blobs.get('threshold'), blobs.get('trade'), _json(result)))
            conn.commit()
            count += 1
            if result['state'] == 'unknown':
                stop = 'source_or_data_unknown'; break
    finally:
        conn.close()
    return dict(attempted=count, stop=stop, max_sessions=max_sessions)


def report(path):
    conn = _connect(path, readonly=True)
    try:
        meta = _meta(conn)
        _verify_block_index(conn, meta)
        if meta['code_sha256'] != code_hashes():
            raise ValueError('Archive needs replay under its recorded code version')
        rows = conn.execute('''SELECT s.*,a.status,a.result,a.metadata,a.book_gzip,a.threshold_gzip,a.trade_gzip FROM attempts a
            JOIN sessions s ON s.seq=a.session_seq
            WHERE a.id=(SELECT MAX(id) FROM attempts WHERE session_seq=a.session_seq) ORDER BY s.seq''').fetchall()
        verified_blocks, replayed = set(), 0
        for row in rows:
            block_no = (row['seq'] - 1) // BLOCK_SIZE
            if block_no not in verified_blocks:
                _verify_block(conn, row['seq'])
                verified_blocks.add(block_no)
            replayed += _replay_attempt(row)
        sessions = [dict(ins_code=row['ins_code'], symbol=row['symbol'], date=row['date'],
                         result=json.loads(row['result'])) for row in rows]
        summary = summarize_sessions(sessions)
        cohort = {row['ins_code']: row['n'] for row in conn.execute(
            'SELECT ins_code,COUNT(*) AS n FROM sessions GROUP BY ins_code')}
        for row in summary:
            row['frozen_cohort_sessions'] = cohort.get(row['ins_code'])
        return dict(version=VERSION, generated_at=now_tehran_iso(), plan=meta,
                    attempted_unique_sessions=len(rows), unattempted_sessions=meta['session_count']-len(rows),
                    current_registry_historical_completeness_verified=False,
                    source_history_completeness_verified=False, ranking_of_full_market_verified=False,
                    latest_attempts_replayed=replayed, latest_attempts_unknown_not_replayed=len(rows)-replayed,
                    preopen_clock_bucket_is_not_verified_market_phase=True,
                    retrospective_descriptive_only=True, predictive_validation=False,
                    execution_proven=False, accepted_for_signals=False, symbols=summary)
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('create', 'capture', 'report'))
    parser.add_argument('--archive')
    parser.add_argument('--max-sessions', type=int, default=5)
    parser.add_argument('--retry-failed', action='store_true')
    args = parser.parse_args(argv)
    root = (settings.DATA_DIR / 'private' / 'queue-fall-studies').resolve()
    root.mkdir(parents=True, exist_ok=True)
    if args.action == 'create':
        if args.archive:
            raise ValueError('Create always chooses a unique archive name')
        path = root / ('QUEUE-FALL-' + uuid.uuid4().hex + '.sqlite')
        result = create_archive(settings.DB_PATH, path, cutoff=now_tehran().date().isoformat(), fingerprints=code_hashes())
    else:
        if not args.archive:
            raise ValueError('Archive name required')
        path = (root / args.archive).resolve()
        if path.parent != root or path.suffix != '.sqlite' or path.name != args.archive:
            raise ValueError('Only a study archive name inside its private folder is allowed')
        if args.action == 'capture':
            provider = TsetmcProvider(realtime=True)
            try:
                result = capture_batch(path, provider, max_sessions=args.max_sessions, retry_failed=args.retry_failed)
            finally:
                provider.close()
        else:
            result = report(path)
            output = root / (path.stem + '-report.json')
            write_private_report(output, result, forbidden_paths=(settings.DB_PATH, path))
            result = dict(report=str(output), attempted_unique_sessions=result['attempted_unique_sessions'],
                          unattempted_sessions=result['unattempted_sessions'])
    print(_json(dict(archive=str(path), result=result)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
