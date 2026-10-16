"""مرزهای اصلاح ضمیمه: قطع واقعی نوشتن و ورودی نامعتبر، فقط در مسیر موقت آزمون."""

import copy
import json
from datetime import datetime

import pytest

from config import settings
from core.query_screener import trade_evidence
from core.timeutils import TEHRAN_TZ
from data_collector.public_site import PublicSiteArchive
from data_collector.trade_archive import TradeArchive
from ops.query_screener import read_query, start_query
from tests.test_public_site import Response, session
from tests.test_query_screener import bar, trades, universe


@pytest.mark.parametrize('field,value', [('trade_count', True), ('trade_count', 'bad'),
    ('trade_count', float('nan')), ('value', True), ('value', float('inf')), ('value', 'bad')])
def test_present_invalid_daily_totals_are_conflicts_even_for_partial_witness(field, value):
    source = trades()
    source['response']['tradeHistory'] = source['response']['tradeHistory'][1:3]
    result = trade_evidence(source, bar(**{field: value}))
    assert result['contradictory'] and result['event_confirmed'] is None and result['witness'] is None


def test_exhausted_value_with_remaining_volume_is_a_conflict():
    source = trades()
    source['response']['tradeHistory'] = source['response']['tradeHistory'][1:3]
    result = trade_evidence(source, bar(value=200, volume=10, trade_count=None))
    assert result['contradictory'] and result['event_confirmed'] is None


def test_unknown_optional_totals_still_allow_a_dated_identified_partial_witness():
    source = trades()
    source['response']['tradeHistory'] = source['response']['tradeHistory'][1:3]
    result = trade_evidence(source, bar(value=None, trade_count=None))
    assert result['event_confirmed'] is True and not result['complete'] and not result['contradictory']


@pytest.mark.parametrize('stamp,expected', [('2024-02-04T09:00:00+03:30', 101),
    ('2024-02-04T10:00:00+03:30', 101), ('2024-02-04T10:00:01+03:30', 99),
    ('2024-02-04T12:00:00+03:30', 101), ('2024-02-04T10:00:01', 101)])
def test_current_day_quote_requires_known_market_time_after_daily_capture(db, tmp_path, monkeypatch, stamp, expected):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path / 'data')
    monkeypatch.setattr('ops.query_screener.now_tehran', lambda: datetime(2024, 2, 4, 11, tzinfo=TEHRAN_TZ))
    sid = db.upsert_symbol(universe(1)[0])
    db.insert_daily_price(sid, '2024-02-04', 100, 102, 98, 101, volume=4, value=401,
                          trade_count=4, close_final_raw=102, ref_price_raw=100)
    with db.conn:
        db.conn.execute("UPDATE daily_prices SET captured_at='2024-02-04T10:00:00+03:30'")

    class Quote:
        def get_market_snapshots(self):
            return [dict(ins_code='1', provider_timestamp=stamp, last_price=99, source='tsetmc', price_unit='Rial')]

    folder = start_query(db, dict(start='2024-02-03', end='2024-02-03', cap_low=0, cap_high=100), quote_provider=Quote())
    row = read_query(folder)[0]['caps']['rows'][0]
    assert row['price_rial'] == expected
    if expected == 99:
        assert row['price_kind'] == 'dated_market_snapshot' and row['price_provider_timestamp'] == stamp
        assert row['price_received_at'] == '2024-02-04T11:00:00+03:30'


def interrupted_archive(tmp_path, monkeypatch):
    monkeypatch.setattr('data_collector.public_site.time.sleep', lambda _: None)
    archive = PublicSiteArchive('https://example.test/', tmp_path, session=session())
    archive.crawl()
    def fail(line):
        with archive.manifest.open('ab') as stream:
            stream.write(line[:20])
        raise OSError('interrupted')
    monkeypatch.setattr(archive, '_append_line', fail)
    with pytest.raises(OSError):
        archive._record(copy.deepcopy(archive.records[-1]))
    return archive


@pytest.mark.parametrize('damage', ['prefix', 'fragment', 'receipt', 'extra_record', 'invalid_record'])
def test_recovery_conflicts_preserve_all_files(tmp_path, monkeypatch, damage):
    archive = interrupted_archive(tmp_path, monkeypatch)
    pending = tmp_path / '.pending-source-record.json'
    contents = archive.manifest.read_bytes()
    if damage == 'prefix':
        archive.manifest.write_bytes(contents.replace(b'https', b'httpx', 1))
    elif damage == 'fragment':
        archive.manifest.write_bytes(contents[:-1] + b'!')
    elif damage == 'receipt':
        receipt = json.loads(pending.read_bytes())
        receipt['line_sha256'] = '0' * 64
        pending.write_text(json.dumps(receipt), encoding='utf-8')
    elif damage == 'extra_record':
        archive.manifest.write_bytes(contents + b'\n{}\n')
    else:
        import hashlib

        receipt = json.loads(pending.read_bytes())
        record = json.loads(receipt['line'])
        record['record_id'] = True
        receipt['line'] = json.dumps(record, ensure_ascii=False) + '\n'
        receipt['line_sha256'] = hashlib.sha256(receipt['line'].encode()).hexdigest()
        pending.write_text(json.dumps(receipt), encoding='utf-8')
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    with pytest.raises(ValueError):
        PublicSiteArchive('https://example.test/', tmp_path, session=session())
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}


def test_damaged_pending_body_never_changes_manifest(tmp_path, monkeypatch):
    archive = interrupted_archive(tmp_path, monkeypatch)
    body = tmp_path / archive.records[-1]['body_file']
    body.write_bytes(b'bad compressed data')
    before = archive.manifest.read_bytes()
    with pytest.raises((OSError, ValueError)):
        PublicSiteArchive('https://example.test/', tmp_path, session=session())
    assert archive.manifest.read_bytes() == before and (tmp_path / '.pending-source-record.json').exists()


def test_legacy_partial_manifest_without_receipt_is_not_silently_truncated(tmp_path):
    manifest = tmp_path / 'sources.jsonl'
    manifest.write_bytes(b'{"url": "https://example.test/')
    with pytest.raises(ValueError):
        PublicSiteArchive('https://example.test/', tmp_path, session=session())
    assert manifest.read_bytes() == b'{"url": "https://example.test/'


def test_derived_text_has_independent_hash_and_damage_is_detected(tmp_path, monkeypatch):
    monkeypatch.setattr('data_collector.public_site.time.sleep', lambda _: None)
    archive = PublicSiteArchive('https://example.test/', tmp_path, session=session())
    archive.crawl()
    record = next(r for r in archive.records if r.get('text_file'))
    assert record['text_sha256'] in record['text_file']
    (tmp_path / record['text_file']).write_bytes(b'damaged')
    with pytest.raises((OSError, ValueError)):
        PublicSiteArchive('https://example.test/', tmp_path, session=session())


def test_catalog_rebuild_never_ignores_corrupted_original_body(tmp_path, monkeypatch):
    from ops.source_catalog import catalog_site

    monkeypatch.setattr('data_collector.public_site.time.sleep', lambda _: None)
    archive = PublicSiteArchive('https://example.test/', tmp_path, session=session())
    archive.crawl()
    record = next(r for r in archive.records if r.get('text_file'))
    (tmp_path / record['body_file']).write_bytes(b'damaged original')
    with pytest.raises((OSError, ValueError)):
        catalog_site(tmp_path)


def test_unavailable_robots_publishes_current_failure_coverage(tmp_path, monkeypatch):
    monkeypatch.setattr('data_collector.public_site.time.sleep', lambda _: None)
    fake = session()
    archive = PublicSiteArchive('https://example.test/', tmp_path, session=fake)
    archive.crawl()
    fake.routes['https://example.test/robots.txt'] = Response(status=503)
    result = archive.crawl()
    assert result['stopped'] == 'robots_unavailable'
    assert json.loads((tmp_path / 'coverage.json').read_bytes()) == result


def test_failed_trade_publication_removes_temp_and_preserves_cached_version(tmp_path, monkeypatch):
    class Provider:
        def get_trade_session(self, *_): return trades()
    archive = TradeArchive(tmp_path)
    _, path = archive.capture(Provider(), '123', '2024-02-03')
    before = path.read_bytes()
    def fail(*_): raise OSError('replacement interrupted')
    monkeypatch.setattr('data_collector.trade_archive.os.replace', fail)
    with pytest.raises(OSError):
        archive.capture(Provider(), '123', '2024-02-03', refresh=True)
    assert path.read_bytes() == before and not list(tmp_path.glob('.*.tmp'))
