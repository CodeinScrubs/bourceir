"""Replay a frozen queue study with bounded memory and a fixed attempt snapshot."""

import argparse
import hashlib
import json
from pathlib import Path

from config import settings
from core.queue_fall_depth import review_depth
from core.queue_fall_study import MAX_BOOK_BYTES, MAX_THRESHOLD_BYTES, summarize_sessions
from core.timeutils import now_tehran_iso
from ops.queue_fall_archive import (
    BLOCK_SIZE,
    VERSION,
    _connect,
    _meta,
    _replay_attempt,
    _restore,
    _verify_block,
    _verify_block_index,
    code_hashes,
)
from ops.report_files import write_private_report

VERSION_STREAM = 'queue-fall-stream-report-v2'
PAGE_SIZE = 256
GROUP_COUNTS = ('attempted', 'observed', 'provisional', 'unknown', 'not_observed', 'pre_09_clock',
                'post_09_clock', 'pre_09_provisional', 'post_09_provisional')


def _code_digest():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _depth_digest():
    return hashlib.sha256((settings.BASE_DIR / 'core/queue_fall_depth.py').read_bytes()).hexdigest()


def report(path, *, page_size=PAGE_SIZE):
    """Verify latest attempts at a fixed ID, holding only one compressed tape at a time.

    Pages deliberately exclude blobs so the SQLite reader does not keep a read
    transaction open while a source body is replayed or while a writer commits.
    """
    if type(page_size) is not int or not 1 <= page_size <= 1000:
        raise ValueError('A bounded metadata page size is required')
    conn = _connect(path, readonly=True)
    try:
        meta = _meta(conn)
        _verify_block_index(conn, meta)
        expected_code, stream_code, depth_code = code_hashes(), _code_digest(), _depth_digest()
        if meta['code_sha256'] != expected_code:
            raise ValueError('Archive needs replay under its recorded code version')
        snapshot_id = conn.execute('SELECT COALESCE(MAX(id),0) FROM attempts').fetchone()[0]
        cohort = {str(row['ins_code']): row['n'] for row in conn.execute(
            'SELECT ins_code,COUNT(*) AS n FROM sessions GROUP BY ins_code').fetchall()}
        groups, verified_blocks = {}, set()
        depth_totals = dict(explicit_five_empty=0, missing_levels=0, conflicting_levels=0,
                            deeper_upper_bid_needs_review=0, sessions_demoted_to_unknown=0,
                            sessions_demoted_to_provisional=0)
        processed = replayed = last_seq = 0
        previous_key = None
        while True:
            page = conn.execute('''SELECT s.*,a.id AS attempt_id,a.status,a.metadata,a.result FROM sessions s
                JOIN attempts a ON a.id=(SELECT MAX(id) FROM attempts WHERE session_seq=s.seq AND id<=?)
                WHERE s.seq>? ORDER BY s.seq LIMIT ?''', (snapshot_id, last_seq, page_size)).fetchall()
            if not page:
                break
            for raw in page:
                row = dict(raw)
                key = (row['date'], str(row['ins_code']))
                if key == previous_key:
                    raise ValueError('Duplicate instrument-day in the frozen cohort')
                previous_key, last_seq = key, row['seq']
                block = (last_seq - 1) // BLOCK_SIZE
                if block not in verified_blocks:
                    _verify_block(conn, last_seq)
                    verified_blocks.add(block)
                sizes = conn.execute('''SELECT length(book_gzip),length(threshold_gzip),length(trade_gzip)
                    FROM attempts WHERE id=?''', (row['attempt_id'],)).fetchone()
                if sizes is None or any(size is not None and size > limit + 1024 * 1024 for size, limit in zip(
                        sizes, (MAX_BOOK_BYTES, MAX_THRESHOLD_BYTES, MAX_BOOK_BYTES))):
                    raise ValueError('Missing or oversized compressed source body')
                blob_row = conn.execute('SELECT book_gzip,threshold_gzip,trade_gzip FROM attempts WHERE id=?',
                                        (row['attempt_id'],)).fetchone()
                if blob_row is None:
                    raise ValueError('A snapshotted attempt disappeared')
                row.update(dict(blob_row))
                replayed += _replay_attempt(row)
                interpreted = json.loads(row['result'])
                original_state = interpreted['state']
                if original_state in ('observed', 'not_observed'):
                    metadata = json.loads(row['metadata'])['observations']
                    book = _restore(metadata.get('book'), row['book_gzip'], MAX_BOOK_BYTES)
                    limits = _restore(metadata.get('threshold'), row['threshold_gzip'], MAX_THRESHOLD_BYTES)
                    interpreted = review_depth(book, limits, row['ins_code'], row['date'], interpreted)
                    for name in ('explicit_five_empty', 'missing_levels', 'conflicting_levels',
                                 'deeper_upper_bid_needs_review'):
                        depth_totals[name] += interpreted['depth_review'][name]
                    if interpreted['state'] != original_state:
                        depth_totals['sessions_demoted_to_' + interpreted['state']] += 1
                session = dict(ins_code=row['ins_code'], symbol=row['symbol'], date=row['date'], result=interpreted)
                value = summarize_sessions([session])[0]
                code = value['ins_code']
                if code not in groups:
                    groups[code] = value
                else:
                    for name in GROUP_COUNTS:
                        groups[code][name] += value[name]
                processed += 1
        symbols = sorted(groups.values(), key=lambda r: (-r['observed'], -r['provisional'], r['ins_code']))
        for value in symbols:
            value['frozen_cohort_sessions'] = cohort.get(value['ins_code'])
        if (processed > meta['session_count'] or code_hashes() != expected_code or _code_digest() != stream_code
                or _depth_digest() != depth_code):
            raise ValueError('Capture code or snapshot count changed during report')
        return dict(version=VERSION, report_version=VERSION_STREAM, generated_at=now_tehran_iso(), plan=meta,
                    snapshot_max_attempt_id=snapshot_id, metadata_page_size=page_size,
                    report_code_sha256=stream_code, depth_code_sha256=depth_code, depth_review_totals=depth_totals,
                    attempted_unique_sessions=processed,
                    unattempted_sessions=meta['session_count']-processed,
                    current_registry_historical_completeness_verified=False,
                    source_history_completeness_verified=False, ranking_of_full_market_verified=False,
                    latest_attempts_replayed=replayed, latest_attempts_unknown_not_replayed=processed-replayed,
                    preopen_clock_bucket_is_not_verified_market_phase=True, retrospective_descriptive_only=True,
                    predictive_validation=False, execution_proven=False, accepted_for_signals=False, symbols=symbols)
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True)
    args = parser.parse_args(argv)
    root = (settings.DATA_DIR / 'private' / 'queue-fall-studies').resolve()
    path = (root / args.archive).resolve()
    if path.parent != root or path.suffix != '.sqlite' or path.name != args.archive:
        raise ValueError('Only a study archive name inside its private folder is allowed')
    result = report(path)
    output = root / (path.stem + '-stream-report.json')
    write_private_report(output, result, forbidden_paths=(settings.DB_PATH, path))
    print(json.dumps(dict(report=str(output), snapshot_max_attempt_id=result['snapshot_max_attempt_id'],
                          attempted_unique_sessions=result['attempted_unique_sessions'],
                          unattempted_sessions=result['unattempted_sessions'])))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
