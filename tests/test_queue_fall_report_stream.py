import sqlite3

import pytest

from core.timeutils import now_tehran
from ops import queue_fall_archive as archive
from ops import queue_fall_report_stream as stream
from tests.test_queue_fall_archive import FakeProvider, source_db


@pytest.fixture
def study(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, 'MIN_FREE_BYTES', 0)
    source, target = tmp_path / 'source.db', tmp_path / 'study.sqlite'
    source_db(source)
    archive.create_archive(source, target, cutoff=now_tehran().date().isoformat(),
                           fingerprints=archive.code_hashes())
    return target


def test_streaming_report_demotes_missing_depth_across_single_record_pages(study):
    archive.capture_batch(study, FakeProvider(), max_sessions=2)
    old, new = archive.report(study), stream.report(study, page_size=1)
    for key in ('attempted_unique_sessions', 'unattempted_sessions', 'latest_attempts_replayed',
                'latest_attempts_unknown_not_replayed', 'ranking_of_full_market_verified'):
        assert old[key] == new[key]
    assert new['snapshot_max_attempt_id'] == 2
    assert old['symbols'][0]['observed'] == 1
    assert new['symbols'][0]['observed'] == 0 and new['symbols'][0]['provisional'] == 1
    assert new['depth_review_totals']['missing_levels'] == 1


def test_streaming_report_does_not_lock_writer_and_keeps_snapshot(study, monkeypatch):
    archive.capture_batch(study, FakeProvider(), max_sessions=1)
    original = stream._replay_attempt
    injected = []

    def append_after_snapshot(row):
        if not injected:
            injected.append(True)
            assert archive.capture_batch(study, FakeProvider(), max_sessions=1)['attempted'] == 1
        return original(row)

    monkeypatch.setattr(stream, '_replay_attempt', append_after_snapshot)
    result = stream.report(study, page_size=1)
    assert result['attempted_unique_sessions'] == result['snapshot_max_attempt_id'] == 1
    with sqlite3.connect(study) as conn:
        assert conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 2


def test_streaming_report_rejects_bad_page_size(study):
    with pytest.raises(ValueError, match='page size'):
        stream.report(study, page_size=0)


def test_streaming_report_detects_corrupt_archive(study):
    archive.capture_batch(study, FakeProvider(), max_sessions=1)
    conn = sqlite3.connect(study)
    conn.execute('DROP TRIGGER no_attempt_update')
    conn.execute("UPDATE attempts SET book_gzip=x'000102' WHERE id=1")
    conn.commit()
    conn.close()
    with pytest.raises((ValueError, OSError, EOFError)):
        stream.report(study)
