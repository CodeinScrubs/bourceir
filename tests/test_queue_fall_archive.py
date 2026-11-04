import json
import sqlite3

import pytest

from core.timeutils import now_tehran
from ops import queue_fall_archive as archive
from tests.test_queue_fall_study import CODE, DAY, book_row, make_inputs


def source_db(path):
    conn = sqlite3.connect(path)
    conn.executescript('''
      CREATE TABLE symbols(id INTEGER,symbol_fa TEXT,ins_code TEXT,instrument_type_code INTEGER);
      CREATE TABLE daily_prices(symbol_id INTEGER,date TEXT,source TEXT,price_unit TEXT,
        open_raw REAL,high_raw REAL,low_raw REAL,close_raw REAL,close_final_raw REAL,ref_price_raw REAL,
        volume INTEGER,value REAL,trade_count INTEGER,captured_at TEXT);
      CREATE TABLE account_private(should_never_be_copied TEXT);
      INSERT INTO account_private VALUES('private');
    ''')
    daily = make_inputs()[3]
    for symbol_id, symbol, code in ((1, 'نمونه', CODE), (2, 'دیگری', '67890')):
        conn.execute('INSERT INTO symbols VALUES (?,?,?,?)', (symbol_id, symbol, code, 300))
        conn.execute('INSERT INTO daily_prices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     (symbol_id, DAY, 'tsetmc', 'Rial', daily['open_raw'], daily['high_raw'], daily['low_raw'],
                      daily['close_raw'], daily['close_raw'], daily['ref_price_raw'], daily['volume'],
                      daily['value'], daily['trade_count'], '2024-10-22T10:00:00+03:30'))
    conn.commit()
    conn.close()


def for_code(observation, code):
    result = dict(observation)
    result['ins_code'] = code
    result['url'] = result['url'].replace(CODE, code)
    payload = json.loads(result['content'])
    for rows in payload.values():
        for row in rows:
            if row.get('insCode') == CODE:
                row['insCode'] = code
    body = json.dumps(payload, separators=(',', ':')).encode()
    result.update(content=body, response_bytes=len(body), body_sha256=archive._digest(body))
    return result


class FakeProvider:
    def __init__(self, *, unavailable_first=False):
        first_book, first_limits, tape, _ = make_inputs()
        second_book, second_limits, _, _ = make_inputs([book_row(price=10499)])
        raw_tape = {k: v for k, v in tape.items() if k != 'response'}
        raw_tape = dict(source='tsetmc', ins_code=CODE, url=raw_tape['source_url'], http_status=200,
                        requested_at='2024-10-22T10:00:00+03:30', received_at='2024-10-22T10:00:01+03:30')
        body = json.dumps(tape['response'], separators=(',', ':')).encode()
        raw_tape.update(content=body, response_bytes=len(body), body_sha256=archive._digest(body))
        self.observations = {o['url']: o for o in (first_book, first_limits, raw_tape,
                             for_code(second_book, '67890'), for_code(second_limits, '67890'))}
        self.calls = []
        self.unavailable_first = unavailable_first
        self.last_transport_evidence = None

    def _get_quote_context_json(self, _code, url, *, max_bytes):
        self.calls.append(url)
        self.last_transport_evidence = dict(status='test_response')
        if self.unavailable_first and CODE in url:
            return None
        response = self.observations[url]
        assert response['response_bytes'] <= max_bytes
        return dict(response)


@pytest.fixture
def study(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, 'MIN_FREE_BYTES', 0)
    source, target = tmp_path / 'source.db', tmp_path / 'study.sqlite'
    source_db(source)
    meta = archive.create_archive(source, target, cutoff=now_tehran().date().isoformat(),
                                  fingerprints=archive.code_hashes())
    return source, target, meta


def test_market_only_frozen_manifest_and_resumable_capture(study):
    _, target, meta = study
    assert meta['session_count'] == meta['registry_count'] == 2
    conn = sqlite3.connect(target)
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='account_private'").fetchone()[0] == 0
    with pytest.raises(sqlite3.DatabaseError, match='frozen'):
        conn.execute('DELETE FROM sessions')
    conn.close()
    provider = FakeProvider()
    assert archive.capture_batch(target, provider, max_sessions=1)['attempted'] == 1
    assert archive.capture_batch(target, provider, max_sessions=1)['attempted'] == 1
    done = archive.capture_batch(target, provider, max_sessions=1)
    assert done['attempted'] == 0 and done['stop'] == 'cohort_complete'
    result = archive.report(target)
    assert result['attempted_unique_sessions'] == 2 and result['unattempted_sessions'] == 0
    assert [(r['ins_code'], r['observed'], r['not_observed']) for r in result['symbols']] == [
        (CODE, 1, 0), ('67890', 0, 1)]
    assert len(provider.calls) == 5  # The second book has no upper-limit bid, so no tape is fetched.
    assert not result['ranking_of_full_market_verified']


def test_failed_source_can_be_skipped_and_explicitly_retried(study):
    _, target, _ = study
    first = archive.capture_batch(target, FakeProvider(unavailable_first=True), max_sessions=2)
    assert first == dict(attempted=1, stop='source_or_data_unknown', max_sessions=2)
    second = archive.capture_batch(target, FakeProvider(), max_sessions=1)
    assert second['attempted'] == 1
    before = archive.report(target)
    assert before['symbols'][0]['unknown'] == 1
    retried = archive.capture_batch(target, FakeProvider(), max_sessions=1, retry_failed=True)
    assert retried['attempted'] == 1
    after = archive.report(target)
    assert after['symbols'][0]['observed'] == 1
    assert after['attempted_unique_sessions'] == 2
    conn = sqlite3.connect(target)
    assert conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 3
    conn.close()


def test_changed_capture_code_blocks_new_requests(study, monkeypatch):
    _, target, _ = study
    monkeypatch.setattr(archive, 'code_hashes', lambda: {'different': 'code'})
    provider = FakeProvider()
    with pytest.raises(ValueError, match='code changed'):
        archive.capture_batch(target, provider)
    assert not provider.calls


def test_tampered_manifest_blocks_capture(study):
    _, target, _ = study
    conn = sqlite3.connect(target)
    conn.execute('DROP TRIGGER no_session_update')
    conn.execute("UPDATE sessions SET symbol='changed' WHERE seq=1")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match='manifest block changed'):
        archive.capture_batch(target, FakeProvider())


def test_report_replays_and_rejects_altered_source_bytes(study):
    _, target, _ = study
    archive.capture_batch(target, FakeProvider(), max_sessions=1)
    assert archive.report(target)['latest_attempts_replayed'] == 1
    conn = sqlite3.connect(target)
    conn.execute('DROP TRIGGER no_attempt_update')
    conn.execute("UPDATE attempts SET book_gzip=x'000102' WHERE id=1")
    conn.commit()
    conn.close()
    with pytest.raises((ValueError, OSError, EOFError)):
        archive.report(target)


def test_report_rejects_an_altered_result(study):
    _, target, _ = study
    archive.capture_batch(target, FakeProvider(), max_sessions=1)
    conn = sqlite3.connect(target)
    conn.execute('DROP TRIGGER no_attempt_update')
    result = json.loads(conn.execute('SELECT result FROM attempts WHERE id=1').fetchone()[0])
    result['observed'][0]['queue_price_rial'] += 1
    conn.execute('UPDATE attempts SET result=? WHERE id=1', (json.dumps(result),))
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match='differs from source replay'):
        archive.report(target)
