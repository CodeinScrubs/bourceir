"""Native publication contracts and routine integration; all data/DBs are temporary."""
import base64
import hashlib
import json
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from core.daily_publications import CLIENT_FIELDS, MAX_BYTES, parse_publication, publication_request
from data_collector.daily_publications import DailyPublicationArchive, review_archive
from data_collector.scheduler import DataCollector
from data_collector.tsetmc import TsetmcProvider
from tests.conftest import FOOLAD_INS

DAY = '2024-01-03'
NOW = '2024-01-03T14:00:00+03:30'


def price(code=FOOLAD_INS, day=DAY):
    return dict(insCode=code, dEven=int(day.replace('-', '')), priceFirst=100., priceMax=101., priceMin=99.,
        pDrCotVal=100., pClosing=100., priceYesterday=100., qTotTran5J=100., qTotCap=10_000., zTotTran=10.)


def client(code=FOOLAD_INS, day=DAY):
    return dict(insCode=code, recDate=int(day.replace('-', '')), buy_I_Volume=80., buy_N_Volume=20.,
        buy_I_Value=8000., buy_N_Value=2000., buy_I_Count=10., buy_N_Count=1.,
        sell_I_Volume=60., sell_N_Volume=40., sell_I_Value=6000., sell_N_Value=4000., sell_I_Count=5., sell_N_Count=2.)


def observation(kind='price', code=FOOLAD_INS, day=DAY, native=None, body=None):
    if body is None:
        item = native if native is not None else (price(code, day) if kind == 'price' else client(code, day))
        body = json.dumps({'closingPriceDaily' if kind == 'price' else 'clientType': item}).encode()
    return dict(source='tsetmc', price_unit='Rial', ins_code=code, requested_date=day, kind=kind,
        url=publication_request(code, day, kind)[1], requested_at=NOW, received_at=NOW, content=body,
        response_bytes=len(body), body_sha256=hashlib.sha256(body).hexdigest())


def task(code=FOOLAD_INS, day=DAY, kind='price', sid=1, symbol='آزمون'):
    return dict(symbol_id=sid, symbol=symbol, ins_code=code, date=day, kind=kind, instrument_type_code=300)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    stamp = datetime.fromisoformat(NOW)
    monkeypatch.setattr('data_collector.daily_publications.now_tehran_iso', lambda: NOW)
    monkeypatch.setattr('data_collector.scheduler.now_tehran', lambda: stamp)
    monkeypatch.setattr('data_collector.scheduler.now_tehran_iso', lambda: NOW)
    monkeypatch.setattr('data_collector.tsetmc.now_tehran_iso', lambda: NOW)


def test_price_mapping_and_official_close_outside_prints():
    raw = price()
    raw.update(pClosing=97., priceYesterday=95.)
    result = parse_publication(observation(native=raw), FOOLAD_INS, DAY, 'price')
    assert result['row']['close_raw'] == 100 and result['row']['close_final_raw'] == 97
    assert result['row']['ref_price_raw'] == 95
    assert result['status'] == 'valid_price' and not result['accepted_for_signals']
    assert not result['independent_source_correctness_verified']


@pytest.mark.parametrize('field,value', [
    ('insCode', None), ('insCode', '0'), ('insCode', True), ('insCode', 'other'),
    ('dEven', 20240102), ('dEven', '202401030'), ('dEven', True),
    ('priceFirst', True), ('pDrCotVal', 200), ('qTotTran5J', .5),
    ('qTotCap', -1), ('zTotTran', float('inf')), ('priceYesterday', 0),
    ('qTotCap', 20000), ('qTotCap', 2**53), ('priceMax', 98),
])
def test_invalid_native_price_never_becomes_a_candle(field, value):
    raw = price()
    raw[field] = value
    with pytest.raises(ValueError):
        parse_publication(observation(native=raw), FOOLAD_INS, DAY, 'price')


def test_zero_activity_does_not_invent_a_candle_or_listing_date():
    native = dict(insCode=FOOLAD_INS, dEven=20240103, qTotTran5J=0, qTotCap=0, zTotTran=0)
    result = parse_publication(observation(native=native), FOOLAD_INS, DAY, 'price')
    assert result['row'] is None and result['status'] == 'observed_zero_activity'
    assert not result['executable'] and not result['full_session_coverage_verified']
    native['zTotTran'] = 1
    with pytest.raises(ValueError):
        parse_publication(observation(native=native), FOOLAD_INS, DAY, 'price')


def test_missing_client_values_stay_unknown_and_observed_zero_stays_zero():
    native = client()
    del native['buy_I_Value']
    native.update(buy_N_Volume=0, buy_N_Value=0, buy_N_Count=0)
    result = parse_publication(observation('client', native=native), FOOLAD_INS, DAY, 'client')
    assert result['row']['buy_i_value'] is None
    assert result['row']['buy_n_volume'] == 0 and result['row']['buy_n_count'] == 0


@pytest.mark.parametrize('field,value', [('buy_I_Count', True), ('sell_N_Volume', 1.1),
    ('buy_I_Value', float('nan')), ('sell_I_Count', -1), ('sell_I_Value', 2**53), ('recDate', 20240102)])
def test_client_schema_rejects_false_precision_and_wrong_day(field, value):
    native = client()
    native[field] = value
    with pytest.raises(ValueError):
        parse_publication(observation('client', native=native), FOOLAD_INS, DAY, 'client')


@pytest.mark.parametrize('field,value', [('price_unit', 'Toman'), ('source', 'other'),
    ('ins_code', '1'), ('url', 'https://example.invalid'), ('requested_date', '2024-01-02'),
    ('kind', 'client'), ('response_bytes', True), ('body_sha256', 'f'*64),
    ('requested_at', '2024-01-03T14:00:00'), ('received_at', '2024-01-03T13:59:59+03:30')])
def test_wrapper_provenance_is_mandatory(field, value):
    source = observation()
    source[field] = value
    with pytest.raises(ValueError):
        parse_publication(source, FOOLAD_INS, DAY, 'price')


@pytest.mark.parametrize('body', [b'[]', b'<html>captcha</html>', b'\xff', b'{}', b'x'*(MAX_BYTES+1),
    b'{"closingPriceDaily":{},"closingPriceDaily":{}}', b'{"unused":1e999}', b'{"unused":NaN}'],
    ids=['array', 'html', 'encoding', 'missing', 'oversize', 'duplicate-key', 'overflow', 'nan'])
def test_raw_json_contract_fails_closed(body):
    with pytest.raises(ValueError):
        parse_publication(observation(body=body), FOOLAD_INS, DAY, 'price')


def test_excessively_nested_json_is_rejected_without_dropping_other_observations(tmp_path):
    nested = b'['*12000+b'0'+b']'*12000
    with pytest.raises(ValueError):
        parse_publication(observation(body=nested), FOOLAD_INS, DAY, 'price')
    provider = Provider(lambda source: observation(body=nested) if source['ins_code'] == FOOLAD_INS else source)
    archive = DailyPublicationArchive(tmp_path, [task(), task(code='2', sid=2)])
    records = archive.capture(provider)
    archive.finish({})
    assert [r['parsed']['status'] for r in records] == ['rejected', 'valid_price']
    assert review_archive(archive.folder)['observations'] == 2


def test_native_timing_is_bounded_by_the_observer():
    source = observation()
    with pytest.raises(ValueError):
        parse_publication(source, FOOLAD_INS, DAY, 'price', requested_after='2024-01-03T14:00:01+03:30', received_by=NOW)
    with pytest.raises(ValueError):
        parse_publication(source, FOOLAD_INS, DAY, 'price', received_by=NOW)


class Provider:
    supports_dated_publications = True
    name = 'tsetmc'
    request_profile = 'offline-test'

    def __init__(self, transform=None):
        self.calls, self.closed, self.transform = [], False, transform

    def get_daily_publication_observation(self, code, day, kind):
        self.calls.append((code, day, kind))
        result = observation(kind, code, day)
        return self.transform(result) if self.transform else result

    def close(self):
        self.closed = True

    def get_client_types_history(self, *args, **kwargs):
        return None


def test_byte_archive_replay_and_secret_header_exclusion(tmp_path):
    provider = Provider(lambda s: dict(s, headers={'Authorization': 'never-archive-this-secret'}))
    archive = DailyPublicationArchive(tmp_path, [task()])
    records = archive.capture(provider)
    path = archive.finish(dict(rows_stored=1))
    assert not provider.closed  # Caller-owned transports remain usable.
    assert records[0]['parsed']['row']['volume'] == 100
    raw = (Path(path)/'observation-00000.json').read_text(encoding='utf-8')
    assert 'never-archive-this-secret' not in raw and 'body_base64' in raw
    replay = review_archive(path)
    assert replay['byte_replay_verified'] and not replay['application_independently_verified']


@pytest.mark.parametrize('change', ['body', 'parsed', 'clock', 'saved_code'])
def test_archive_rejects_tampering_even_after_rehashing(tmp_path, change):
    archive = DailyPublicationArchive(tmp_path, [task()])
    archive.capture(Provider())
    path = Path(archive.finish({}))
    file = path/'observation-00000.json'
    record = json.loads(file.read_text(encoding='utf-8'))
    if change == 'body':
        record['source']['body_base64'] = base64.b64encode(b'{}').decode()
    elif change == 'parsed':
        record['parsed']['row']['volume'] = 9000
    elif change == 'clock':
        record['attempted_at'] = '2024-01-02T14:00:00+03:30'
    else:
        (path/'code/core/daily_publications.py').write_text('changed', encoding='utf-8')
    file.write_text(json.dumps(record), encoding='utf-8')
    result = json.loads((path/'result.json').read_text(encoding='utf-8'))
    result['artifacts_sha256'][file.name] = hashlib.sha256(file.read_bytes()).hexdigest()
    (path/'result.json').write_text(json.dumps(result), encoding='utf-8')
    with pytest.raises(ValueError):
        review_archive(path)


def test_source_cannot_change_between_capture_and_application_seal(tmp_path):
    archive = DailyPublicationArchive(tmp_path, [task()])
    archive.capture(Provider())
    (archive.folder/'observation-00000.json').write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError):
        archive.finish({})


def test_an_attempt_and_its_result_cannot_be_overwritten_in_place(tmp_path):
    archive = DailyPublicationArchive(tmp_path, [task()])
    with pytest.raises(ValueError):
        archive.finish({})
    provider = Provider()
    archive.capture(provider)
    archive.finish({})
    original = (archive.folder/'result.json').read_bytes()
    with pytest.raises(ValueError):
        archive.capture(provider)
    with pytest.raises(ValueError):
        archive.finish(dict(rows_stored=900))
    assert len(provider.calls) == 1 and (archive.folder/'result.json').read_bytes() == original


@pytest.mark.parametrize('changes', [{'date': '2099-01-01'}, {'instrument_type_code': 309},
    {'symbol': 'آزمون2'}, {'ins_code': 'bad'}, {'symbol_id': True}])
def test_scope_is_validated_before_any_artifact_or_network(tmp_path, changes):
    with pytest.raises(ValueError):
        DailyPublicationArchive(tmp_path/'not-created', [dict(task(), **changes)])
    assert not (tmp_path/'not-created').exists()


def test_owned_worker_transports_are_not_shared_and_are_closed(tmp_path):
    owned = []
    class ThreadOwned(Provider):
        def __init__(self):
            super().__init__()
            self.thread = threading.get_ident()
            owned.append(self)

        def get_daily_publication_observation(self, *args):
            assert self.thread == threading.get_ident()
            return super().get_daily_publication_observation(*args)
    archive = DailyPublicationArchive(tmp_path, [task(code=str(i+1), sid=i+1) for i in range(9)], workers=3)
    original = Provider()
    archive.capture(original, provider_factory=ThreadOwned)
    archive.finish({})
    assert 1 <= len(owned) <= 3 and all(p.closed for p in owned)
    assert not original.calls and not original.closed
    assert review_archive(archive.folder)['observations'] == 9


def register(db, code=FOOLAD_INS, symbol='آزمون'):
    return db.upsert_symbol(dict(symbol_fa=symbol, ins_code=code, instrument_type_code=300))


def daily(db, sid, day=DAY):
    db.insert_daily_price(sid, day, 100, 101, 99, 100, volume=100, value=10000, trade_count=10,
                          close_final_raw=100, ref_price_raw=100)


def prior_clients(db, sid, day=DAY, count=1):
    native = client(day=day)
    row = dict(date=day, **{column: native[field] for column, field in CLIENT_FIELDS.items()})
    row['buy_i_count'] = count
    db.upsert_client_types_df(sid, pd.DataFrame([row]))


def test_routine_native_price_does_not_require_an_undated_watch(db):
    register(db)
    provider = Provider()
    result = DataCollector(db=db, provider=provider).collect_session_candles()
    assert result['status'] == 'success' and result['rows_inserted'] == 1
    assert db.get_last_daily_date(db.get_symbol_id('آزمون')) == DAY
    assert provider.calls == [(FOOLAD_INS, DAY, 'price')]
    assert Path(result['evidence_path']).is_relative_to(db.db_path.parent)
    assert review_archive(result['evidence_path'])['byte_replay_verified']


def test_zero_activity_is_separate_and_cannot_silently_remove_existing_trades(db):
    sid = register(db)
    def zero(source):
        return observation(native=dict(insCode=FOOLAD_INS, dEven=20240103, qTotTran5J=0, qTotCap=0, zTotTran=0))
    collector = DataCollector(db=db, provider=Provider(zero))
    result = collector.collect_session_candles()
    assert result['status'] == 'success' and result['zero_activity_observations'] == 1
    assert result['rows_inserted'] == 0 and db.get_daily_prices_df('آزمون').empty
    daily(db, sid)
    result = collector.collect_session_candles()
    assert result['status'] == 'failed' and result['rows_inserted'] == 0
    assert len(db.get_daily_prices_df('آزمون')) == 1


def test_wrong_native_identity_cannot_replace_a_known_price(db):
    sid = register(db)
    daily(db, sid)
    provider = Provider(lambda s: observation(native=price('other')))
    result = DataCollector(db=db, provider=provider).collect_session_candles()
    assert result['status'] == 'failed' and db.get_daily_prices_df('آزمون').iloc[0]['close_raw'] == 100


def test_recent_existing_client_rows_are_refreshed_with_byte_receipts(db):
    sid = register(db)
    daily(db, sid)
    prior_clients(db, sid)
    result = DataCollector(db=db, provider=Provider()).collect_client_types(['آزمون', 'آزمون'])
    assert result['status'] == 'success' and result['rows_inserted'] == 1 and result['symbols_requested'] == 1
    assert result['coherent_retail_pairs'] == 1
    assert db.get_client_types_df('آزمون').iloc[0]['buy_i_count'] == 10
    assert db.conn.execute('SELECT count(*) FROM client_types_daily_revisions').fetchone()[0] == 1
    assert review_archive(result['evidence_path'])['statuses'] == {'observed_client': 1}


def test_late_client_day_retains_failure_then_a_new_attempt_can_succeed(db):
    sid = register(db)
    for day in ('2024-01-02', DAY):
        daily(db, sid, day)
        prior_clients(db, sid, day)
    provider = Provider(lambda s: None if s['requested_date'] == DAY else s)
    collector = DataCollector(db=db, provider=provider)
    first = collector.collect_client_types(['آزمون'])
    assert first['status'] == 'partial' and first['partial_symbols'] == 1 and first['rows_inserted'] == 1
    assert db.get_client_types_df('آزمون').set_index('date').loc[DAY, 'buy_i_count'] == 1
    provider.transform = None
    second = collector.collect_client_types(['آزمون'])
    assert second['status'] == 'success' and first['evidence_path'] != second['evidence_path']
    assert review_archive(first['evidence_path'])['statuses']['source_unavailable'] == 1


def test_incoherent_raw_client_is_retained_as_raw_but_never_reported_complete(db):
    sid = register(db)
    daily(db, sid)
    prior_clients(db, sid)
    native = client()
    native['buy_I_Value'] = 20000
    result = DataCollector(db=db, provider=Provider(lambda s: observation('client', native=native))).collect_client_types(['آزمون'])
    assert result['status'] == 'partial' and result['unknown_retail_pairs'] == 1
    assert db.get_client_types_df('آزمون').iloc[0]['buy_i_value'] == 20000


def test_one_symbol_provider_exception_does_not_drop_other_prices(db):
    register(db)
    register(db, code='2', symbol='نمونه')
    def transform(source):
        if source['ins_code'] == FOOLAD_INS:
            raise ConnectionError('offline fixture')
        return source
    result = DataCollector(db=db, provider=Provider(transform)).collect_session_candles()
    assert result['status'] == 'partial' and result['symbols_succeeded'] == result['symbols_failed'] == 1
    assert len(db.get_daily_prices_df('نمونه')) == 1 and db.get_daily_prices_df('آزمون').empty


def test_invalid_metadata_isolated_before_network_instead_of_aborting_the_batch(db):
    register(db)
    register(db, code='not-a-code', symbol='نمونه')
    provider = Provider()
    result = DataCollector(db=db, provider=provider).collect_session_candles()
    assert result['status'] == 'partial' and result['symbols_failed'] == 1
    assert provider.calls == [(FOOLAD_INS, DAY, 'price')]


def test_selected_identity_mismatch_never_collects_a_different_stock(db, monkeypatch):
    register(db, code='2', symbol='نمونه')
    provider = Provider()
    collector = DataCollector(db=db, provider=provider)
    monkeypatch.setattr(collector, '_resolve', lambda _: db.get_symbol('نمونه'))
    result = collector.collect_session_candles(['آزمون'])
    assert result['status'] == 'failed' and not provider.calls
    assert db.get_daily_prices_df('نمونه').empty


def test_legacy_session_refresh_also_respects_the_requested_symbol(db):
    register(db)
    register(db, code='2', symbol='نمونه')
    provider = SimpleNamespace(supports_dated_publications=False,
        get_session_candles=lambda: dict(date=DAY, candles=[
            dict(parse_publication(observation(code=code), code, DAY, 'price')['row'], ins_code=code)
            for code in (FOOLAD_INS, '2')]))
    result = DataCollector(db=db, provider=provider).collect_session_candles(['آزمون'])
    assert result['status'] == 'success' and result['rows_inserted'] == 1
    assert db.get_daily_prices_df('نمونه').empty


def test_archive_network_is_not_called_inside_database_transaction(db):
    register(db)
    provider = Provider()
    db.conn.execute('BEGIN')
    try:
        with pytest.raises(ValueError, match='تراکنش'):
            DataCollector(db=db, provider=provider).collect_session_candles()
        assert not provider.calls
    finally:
        db.conn.rollback()


def test_selected_symbol_update_refreshes_its_session_without_touching_other_symbols(db):
    register(db)
    register(db, code='2', symbol='نمونه')
    provider = Provider()
    collector = DataCollector(db=db, provider=provider, codal=SimpleNamespace(get_latest=lambda **_: []))
    collector.collect_history = lambda *a, **k: dict(status='success')
    result = collector.update_all(symbols=['آزمون'])
    assert result['session_candles']['status'] == 'success'
    assert {call[0] for call in provider.calls} == {FOOLAD_INS}
    assert db.get_daily_prices_df('نمونه').empty


def test_native_failure_blocks_automatic_feature_publication(monkeypatch):
    import brs
    from data_collector import scheduler
    calls = []
    reports = {key: dict(status='success') for key in ('universe', 'history', 'client_types', 'codal')}
    reports['session_candles'] = dict(status='partial', native_date_and_identity_required=True)
    collector = SimpleNamespace(db=SimpleNamespace(close=lambda: calls.append('close')), update_all=lambda **_: reports)
    monkeypatch.setattr(scheduler, 'DataCollector', lambda **_: collector)
    monkeypatch.setattr(brs, '_build_features', lambda _: calls.append('features'))
    assert brs.cmd_update(SimpleNamespace(symbols='', full=False, no_features=False)) == 1
    assert calls == ['close']


def test_review_command_is_registered_without_network_or_db():
    import brs
    args = brs.build_parser().parse_args(['publication-review', 'local-archive'])
    assert args.path == Path('local-archive') and args.func is brs.cmd_publication_review


def test_native_provider_bytes_use_only_dated_public_routes():
    class Response:
        status_code = 200
        def __init__(self, url):
            self.url = url
            self.body = observation()['content']
            self.headers = {'Content-Length': str(len(self.body))}

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            yield self.body

        def close(self):
            pass
    class Session:
        calls = []
        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return Response(url)
    session = Session()
    provider = TsetmcProvider(session=session, realtime=True)
    result = provider.get_daily_publication_observation(FOOLAD_INS, DAY, 'price')
    assert parse_publication(result, FOOLAD_INS, DAY, 'price')['status'] == 'valid_price'
    assert session.calls[0][1]['allow_redirects'] is False and session.calls[0][1]['timeout'] == (5, 5)
    with pytest.raises(ValueError):
        provider.get_daily_publication_observation(FOOLAD_INS, DAY, 'orders')
    assert len(session.calls) == 1
