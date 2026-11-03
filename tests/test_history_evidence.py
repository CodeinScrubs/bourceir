"""History evidence stays linked to original bytes, including unusable native prices."""

import hashlib
import json
import math
from datetime import datetime

import pandas as pd
import pytest

from data_collector.daily_history import (
    MAX_BYTES,
    VERSION,
    archive_observation,
    history_request,
    normalize_history_payload,
    normalize_observation,
    restore_observation,
)
from data_collector.tsetmc import TsetmcProvider

CODE = '123'
AT = '2026-10-21T14:00:00+03:30'


def row(**overrides):
    return dict(insCode=CODE, dEven=20261021, priceFirst=1000, priceMax=1030, priceMin=970,
                pDrCotVal=1010, pClosing=1005, priceYesterday=1000, qTotTran5J=100,
                qTotCap=100500, zTotTran=10, last=False) | overrides


def receipt(body=None, **overrides):
    if body is None:
        body = json.dumps(dict(closingPriceDaily=[row()])).encode()
    return dict(version=VERSION, source='tsetmc', price_unit='Rial', ins_code=CODE,
                url=history_request(CODE)[1], last_n=100, requested_at=AT, received_at=AT,
                normalization_as_of=AT, http_status=200, response_bytes=len(body),
                body_sha256=hashlib.sha256(body).hexdigest(), content=body) | overrides


def test_native_zero_open_is_preserved_in_bytes_and_unknown_in_normalized_output():
    native = row()
    native['priceFirst'] = 0
    original = receipt(json.dumps(dict(closingPriceDaily=[native]), indent=2).encode())
    archived = archive_observation(original | dict(headers={'secret': 'omitted'}, cookies='omitted'))
    assert 'content' not in archived and 'headers' not in archived and 'cookies' not in archived
    restored = restore_observation(archived)
    assert restored == original
    assert json.loads(restored['content'])['closingPriceDaily'][0]['priceFirst'] == 0
    normalized = normalize_observation(restored).iloc[0]
    assert math.isnan(normalized.open_raw)
    assert normalized.close_raw == 1010 and normalized.close_final_raw == 1005


def test_parser_uses_receipt_clock_for_today_cutoff_without_consuming_the_live_clock(monkeypatch):
    native = dict(closingPriceDaily=[row(), row(dEven=20261022)])
    before = receipt(json.dumps(native).encode(), requested_at='2026-10-21T12:00:00+03:30',
                     received_at='2026-10-21T12:00:00+03:30', normalization_as_of='2026-10-21T12:00:00+03:30')
    assert normalize_observation(before).empty
    after = receipt(json.dumps(native).encode())
    first = normalize_observation(after)
    monkeypatch.setattr('data_collector.tsetmc.now_tehran', lambda: datetime.fromisoformat('2030-01-01T00:00:00+03:30'))
    pd.testing.assert_frame_equal(first, normalize_observation(restore_observation(archive_observation(after))))
    assert first.date.tolist() == ['2026-10-21']


def test_timezone_conversion_matches_tehran_cutoff():
    original = receipt(requested_at='2026-10-21T09:30:00+00:00', received_at='2026-10-21T09:30:00+00:00',
                       normalization_as_of='2026-10-21T13:00:00+03:30')
    assert normalize_observation(original).date.tolist() == ['2026-10-21']


@pytest.mark.parametrize('change', [
    dict(source='other'), dict(ins_code='124'), dict(url='https://example.org/'), dict(http_status=206),
    dict(http_status=True), dict(response_bytes=True), dict(response_bytes=1), dict(body_sha256='0' * 64),
    dict(normalization_as_of='2026-10-21T15:00:00+03:30'), dict(requested_at='2026-10-22T14:00:00+03:30'),
    dict(received_at='2026-10-21T14:00:00'), dict(last_n=True), dict(price_unit='Toman'), dict(version='next'),
], ids=['source', 'code', 'url', 'status', 'status-bool', 'size-bool', 'size', 'digest', 'as-of',
        'reverse-clock', 'naive', 'count-bool', 'unit', 'version'])
def test_receipt_tampering_is_rejected_before_archiving_or_normalizing(change):
    observation = receipt(**change)
    with pytest.raises(ValueError):
        archive_observation(observation)
    with pytest.raises(ValueError):
        normalize_observation(observation)


@pytest.mark.parametrize('body', [
    b'', b'<html>unavailable</html>', b'{', b'{"closingPriceDaily":[],"closingPriceDaily":[]}',
    b'{"closingPriceDaily":[NaN]}', b'{"closingPriceDaily":[Infinity]}', b'{"closingPriceDaily":[1e999]}',
    b'[]', b'{"closingPriceDaily":{}}', b'{"closingPriceDaily":[null]}', b'\xff',
], ids=['empty', 'html', 'broken', 'duplicate', 'nan', 'infinity', 'overflow', 'list-root',
        'object-rows', 'null-row', 'encoding'])
def test_unusable_payload_is_archivable_but_never_becomes_normalized_market_data(body):
    observation = restore_observation(archive_observation(receipt(body)))
    assert observation['content'] == body
    with pytest.raises(ValueError):
        normalize_observation(observation)


@pytest.mark.parametrize('native_identity', [None, 0, True, '124'])
def test_conflicting_stated_native_identity_cannot_be_normalized(native_identity):
    native = row()
    native['insCode'] = native_identity
    original = receipt(json.dumps(dict(closingPriceDaily=[native])).encode())
    assert archive_observation(original)
    with pytest.raises(ValueError, match='another instrument'):
        normalize_observation(original)


@pytest.mark.parametrize('native_identity', [None, 0, True, '124', 123.0])
def test_ordinary_history_mapping_rejects_a_conflicting_native_identity(native_identity):
    payload = dict(closingPriceDaily=[row(insCode=native_identity)])
    assert normalize_history_payload(payload, ins_code=CODE, as_of=AT) is None


def test_ordinary_history_rejects_a_mixed_instrument_response_in_full():
    payload = dict(closingPriceDaily=[row(), row(insCode='124', dEven=20261020)])
    assert normalize_history_payload(payload, ins_code=CODE, as_of=AT) is None


def test_absent_native_identity_is_explicitly_request_scoped_and_legacy_mapping_is_unchanged():
    native = row()
    native.pop('insCode')
    payload = dict(closingPriceDaily=[native])
    original = receipt(json.dumps(payload).encode())
    pd.testing.assert_frame_equal(normalize_observation(original),
        normalize_history_payload(payload, ins_code=CODE, as_of=AT))


def test_boolean_numeric_payload_is_archived_but_rejected():
    native = row()
    native['pClosing'] = True
    original = receipt(json.dumps(dict(closingPriceDaily=[native])).encode())
    assert archive_observation(original)
    with pytest.raises(ValueError):
        normalize_observation(original)


@pytest.mark.parametrize('code,count', [('0',100), ('1/2',100), (True,100), ('123',0), ('123',101), ('123',True)])
def test_public_history_request_is_bounded_and_cannot_select_an_arbitrary_route(code,count):
    with pytest.raises(ValueError):
        history_request(code,count)


class Response:
    def __init__(self, body, *, status=200, url=None, length=None, chunks=None):
        self.body, self.status_code, self.supplied_url = body, status, url
        self.headers = {} if length is None else {'Content-Length': str(length)}
        self.chunks = [body] if chunks is None else chunks
        self.closed = False
        self.yielded = 0
    def raise_for_status(self):
        pass
    def iter_content(self, chunk_size):
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk
    def close(self):
        self.closed = True


class Session:
    def __init__(self,response):
        self.response = response
        self.calls = []
    def get(self,url,**kwargs):
        self.calls.append((url,kwargs))
        self.response.url = self.response.supplied_url or url
        return self.response


@pytest.mark.parametrize('body', [b'', b'<html>source failed</html>', b'{"invalid":true}', b'\x00'])
def test_history_transport_preserves_failed_payload_bytes_for_diagnosis(body):
    response = Response(body)
    session = Session(response)
    observation = TsetmcProvider(session=session,realtime=True).get_symbol_history_observation(CODE)
    assert response.closed and len(session.calls) == 1
    assert observation['content'] == body and observation['http_status'] == 200
    assert restore_observation(archive_observation(observation)) == observation
    assert session.calls[0][1]['stream'] and session.calls[0][1]['allow_redirects'] is False
    assert session.calls[0][1]['timeout'] == (5,5)
    with pytest.raises(ValueError):
        normalize_observation(observation)


@pytest.mark.parametrize('case', ['header-large','stream-large','partial','redirect','url-conflict'])
def test_history_transport_stops_unacceptable_response_and_always_closes(case):
    response = (Response(b'',length=MAX_BYTES+1) if case=='header-large' else
        Response(b'',chunks=[b'x'*65536]*33+[b'not-consumed']) if case=='stream-large' else
        Response(b'{}',status=206) if case=='partial' else
        Response(b'{}',status=302) if case=='redirect' else Response(b'{}',url='https://example.org/'))
    assert TsetmcProvider(session=Session(response),realtime=True).get_symbol_history_observation(CODE) is None
    assert response.closed
    assert response.yielded == (33 if case=='stream-large' else 0)


def test_archive_body_and_schema_changes_cannot_silently_pass_restore():
    original = archive_observation(receipt())
    for changed in [original | dict(body_base64='!!!!'), original | dict(body_base64='e30='),
                    original | dict(cookies='accidental secret')]:
        with pytest.raises(ValueError):
            restore_observation(changed)


def test_shared_transport_keeps_existing_quote_content_policy_and_smaller_bound():
    url=history_request(CODE)[1]
    response = Response(b'<html>source failed</html>')
    assert TsetmcProvider(session=Session(response))._get_quote_context_json(CODE,url) is None
    assert response.closed
    response = Response(b'x'*131073)
    assert TsetmcProvider(session=Session(response))._get_quote_context_json(CODE,url) is None
    assert response.closed


@pytest.mark.parametrize('headers,expected', [
    ({'Content-Length': '2'}, True),
    ({'Content-Length': '3'}, False),
    ({'Content-Length': '1'}, False),
    ({'Content-Length': 'invalid'}, False),
    ({'Content-Length': '3', 'Content-Encoding': 'gzip'}, True),
    ({'Content-Length': '3', 'Transfer-Encoding': 'chunked'}, True),
])
def test_shared_transport_reconciles_only_comparable_declared_lengths(headers, expected):
    response = Response(b'{}')
    response.headers = headers
    url = history_request(CODE)[1]
    result = TsetmcProvider(session=Session(response), realtime=True)._get_quote_context_json(CODE, url)
    assert (result is not None) is expected
    assert response.closed
