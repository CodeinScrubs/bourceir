"""Counterexamples found in the 0.7.8 audit; fixture data stay in temporary paths."""

import json
from datetime import datetime

import pytest

from config import settings
from core.query_screener import trade_evidence
from core.timeutils import TEHRAN_TZ
from data_collector.public_site import PublicSiteArchive
from data_collector.trade_archive import TradeArchive
from ops.query_screener import evaluate_query, read_query, start_query
from tests.test_public_site import session
from tests.test_query_screener import bar, trades, universe


@pytest.mark.parametrize('damage', ['excess_count', 'excess_volume', 'excess_value', 'fractional_count',
                                     'closed_count_wrong_value', 'boolean_count'])
def test_contradictory_trade_totals_cannot_prove_a_witness(damage):
    observed, day = trades(), bar()
    rows = observed['response']['tradeHistory']
    if damage == 'excess_count':
        rows.append(dict(rows[-1], nTran=5, hEven=90005))
    elif damage == 'excess_volume':
        rows[1]['qTitTran'] = 10
    elif damage == 'excess_value':
        day['value'] = 300
    elif damage == 'fractional_count':
        day['trade_count'] = 4.5
    elif damage == 'closed_count_wrong_value':
        day['value'] = 402
    else:
        day['trade_count'] = True
    result = trade_evidence(observed, day)
    assert result['event_confirmed'] is None and result['witness'] is None and not result['complete']
    assert result['issues']


def test_query_keeps_the_registered_weekday_fallback(db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path / 'data')
    sid = db.upsert_symbol(universe(1)[0])
    db.insert_daily_price(sid, '2024-02-03', 100, 102, 98, 101, volume=4, value=401, trade_count=4,
                          close_final_raw=102, ref_price_raw=100)
    folder = start_query(db, dict(start='2024-02-03', end='2024-02-04', cap_low=0, cap_high=100))
    before, _ = read_query(folder)
    assert before['calendar_unverified_no_data_dates'] == ['2024-02-04']
    # This must not reinterpret the frozen query's absent-session coverage.
    monkeypatch.setattr(settings, 'TRADING_DAYS', [])
    after, _ = evaluate_query(folder)
    assert before['calendar_unverified_no_data_dates'] == after['calendar_unverified_no_data_dates']
    policy = before['protocol'].get('calendar_policy')
    assert policy and policy['trading_weekdays']


def _query_with_quote(db, tmp_path, monkeypatch, at, quote_at, last_day):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path / 'data')
    monkeypatch.setattr('ops.query_screener.now_tehran', lambda: at)
    sid = db.upsert_symbol(universe(1)[0])
    db.insert_daily_price(sid, last_day, 100, 102, 98, 101, volume=4, value=401, trade_count=4,
                          close_final_raw=102, ref_price_raw=100)

    class Quote:
        def get_market_snapshots(self):
            return [dict(ins_code='1', provider_timestamp=quote_at, captured_at=at.isoformat(),
                         last_price=99, source='tsetmc', price_unit='Rial')]

    folder = start_query(db, dict(start='2024-02-03', end='2024-02-03', cap_low=0, cap_high=100),
                         quote_provider=Quote())
    return read_query(folder)[0]['caps']['rows'][0]


def test_stale_same_day_quote_cannot_replace_a_completed_daily_last(db, tmp_path, monkeypatch):
    row = _query_with_quote(db, tmp_path, monkeypatch,
        datetime(2024, 2, 4, 10, tzinfo=TEHRAN_TZ), '2024-02-03T10:00:00+03:30', '2024-02-03')
    assert row['price_rial'] == 101 and row['price_date'] == '2024-02-03'


def test_quote_date_is_the_tehran_date_even_when_source_uses_utc(db, tmp_path, monkeypatch):
    row = _query_with_quote(db, tmp_path, monkeypatch,
        datetime(2024, 2, 4, 1, tzinfo=TEHRAN_TZ), '2024-02-03T21:00:00+00:00', '2024-02-03')
    assert row['price_date'] == '2024-02-04' and row['price_rial'] == 99


@pytest.mark.parametrize('observation', [[], {'response': []}, {'response': 'unexpected'}])
def test_malformed_provider_payload_is_preserved_as_invalid_not_a_crash(tmp_path, observation):
    class Provider:
        def get_trade_session(self, *_):
            return observation

    archive = TradeArchive(tmp_path / 'archive')
    record, path = archive.capture(Provider(), '123', '2024-02-03')
    assert record['status'] == 'invalid' and record['observation'] == observation
    assert archive.load(path) == record


def test_precreated_public_archive_reloads_manifest_after_other_writer_finishes(tmp_path, monkeypatch):
    monkeypatch.setattr('data_collector.public_site.time.sleep', lambda _: None)
    first = PublicSiteArchive('https://example.test/', tmp_path, session=session())
    second_session = session()
    second = PublicSiteArchive('https://example.test/', tmp_path, session=second_session)
    first.crawl()
    result = second.crawl()
    assert second_session.calls == ['https://example.test/robots.txt', 'https://example.test/sitemap.xml']
    assert result['sitemap_successful'] == 1
    on_disk = [json.loads(line) for line in (tmp_path / 'sources.jsonl').read_text().splitlines()]
    assert len(second.records) == len(on_disk)


def test_same_archive_object_can_retry_after_rate_limit(tmp_path, monkeypatch):
    from tests.test_public_site import Response

    monkeypatch.setattr('data_collector.public_site.time.sleep', lambda _: None)
    fake = session()
    original = fake.routes['https://example.test/']
    fake.routes['https://example.test/'] = Response(status=429)
    archive = PublicSiteArchive('https://example.test/', tmp_path, session=fake)
    assert archive.crawl()['stopped'] == 'HTTP 429'
    fake.routes['https://example.test/'] = original
    assert archive.crawl()['stopped'] is None


def test_failed_public_publication_keeps_old_file_and_removes_temp(tmp_path, monkeypatch):
    archive = PublicSiteArchive('https://example.test/', tmp_path, session=session())
    target = tmp_path / 'coverage.json'
    target.write_bytes(b'previous complete report')

    def fail(*_):
        raise OSError('simulated failed replacement')

    monkeypatch.setattr('data_collector.public_site.os.replace', fail)
    with pytest.raises(OSError):
        archive._publish_bytes(target, b'new complete report')
    assert target.read_bytes() == b'previous complete report'
    assert not list(tmp_path.glob('*.tmp')) and not list(tmp_path.glob('.*.tmp'))


def test_archive_reader_uses_registered_source_list_but_reevaluation_requires_current_code(db, tmp_path, monkeypatch):
    import hashlib

    from tests.test_query_screener import make_query

    folder = make_query(db, tmp_path, monkeypatch)
    before, _ = read_query(folder)
    # Model an older source inventory: its captured output remains readable.
    code_file = 'config/production.yaml'
    protocol = json.loads((folder / 'protocol.json').read_text())
    protocol['code'].pop(code_file)
    inputs = json.loads((folder / 'inputs.json').read_text())
    inputs.pop('code/' + code_file)
    (folder / 'protocol.json').write_text(json.dumps(protocol))
    inputs['protocol.json'] = hashlib.sha256((folder / 'protocol.json').read_bytes()).hexdigest()
    (folder / 'inputs.json').write_text(json.dumps(inputs))
    pointer = json.loads((folder / 'current.json').read_text())
    revision = folder / pointer['revision']
    result = json.loads((revision / 'result.json').read_text())
    result['protocol'] = protocol
    (revision / 'result.json').write_text(json.dumps(result))
    hashes = json.loads((revision / 'checksums.json').read_text())
    hashes['result.json'] = hashlib.sha256((revision / 'result.json').read_bytes()).hexdigest()
    (revision / 'checksums.json').write_text(json.dumps(hashes))
    pointer['result_sha256'] = hashes['result.json']
    (folder / 'current.json').write_text(json.dumps(pointer))
    assert read_query(folder)[0]['results'] == before['results']
    with pytest.raises(ValueError, match='نسخه کد'):
        evaluate_query(folder)


@pytest.mark.parametrize('stage', ['before_append', 'partial_append', 'after_append'])
def test_public_manifest_recovers_interrupted_record_once_without_losing_prior_evidence(tmp_path, monkeypatch, stage):
    monkeypatch.setattr('data_collector.public_site.time.sleep', lambda _: None)
    archive = PublicSiteArchive('https://example.test/', tmp_path, session=session())
    archive.crawl()
    previous = archive.manifest.read_bytes()
    record = dict(archive.records[-1])

    def interrupt(line):
        if stage != 'before_append':
            with archive.manifest.open('ab') as stream:
                stream.write(line[:20] if stage == 'partial_append' else line)
        raise OSError('interrupted append')

    monkeypatch.setattr(archive, '_append_line', interrupt)
    with pytest.raises(OSError):
        archive._record(record)
    receipt = json.loads((tmp_path / '.pending-source-record.json').read_bytes())
    pending = receipt['line'].encode('utf-8')
    recovered = PublicSiteArchive('https://example.test/', tmp_path, session=session())
    assert recovered.manifest.read_bytes() == previous + pending
    assert recovered.records[-1] == record
    assert len(recovered.records) == len(archive.records) + 1
    assert not (tmp_path / '.pending-source-record.json').exists()
    assert PublicSiteArchive('https://example.test/', tmp_path, session=session()).records == recovered.records
