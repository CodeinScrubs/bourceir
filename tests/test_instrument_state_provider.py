"""State transport contract with synthetic session responses only."""

import hashlib
from datetime import datetime

import pytest
import requests

from config import settings
from core.instrument_states import MAX_BYTES
from core.timeutils import TEHRAN_TZ
from data_collector.base import BaseDataProvider
from data_collector.tsetmc import TsetmcProvider

URL = 'https://cdn.tsetmc.com/api/MarketData/GetInstrumentState/123/20240320'
BODY = b'{"instrumentState": []}'


class Response:
    def __init__(self, body=BODY, *, status=200, url=None, headers=None, stream_error=None):
        self.body, self.status_code, self.url = body, status, url
        self.headers = {} if headers is None else headers
        self.stream_error = stream_error
        self.closed = False
        self.iterated = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError('synthetic HTTP failure')

    def iter_content(self, chunk_size):
        self.iterated = True
        if self.stream_error:
            raise self.stream_error
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset:offset+chunk_size]

    def close(self):
        self.closed = True


class Session:
    def __init__(self, response=None, *, error=None):
        self.response = response or Response()
        self.error = error
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        if self.response.url is None:
            self.response.url = url
        return self.response

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def deterministic_transport(monkeypatch):
    monkeypatch.setattr(settings, 'RATE_LIMIT_DELAY', 0)
    monkeypatch.setattr('data_collector.tsetmc.now_tehran',
                        lambda: datetime(2026, 10, 22, 12, tzinfo=TEHRAN_TZ))


def test_fixed_public_endpoint_preserves_bytes_and_request_date_without_promoting_state():
    response, session = Response(), Session()
    session.response = response
    api = TsetmcProvider(session=session, realtime=True)
    observation = api.get_dated_instrument_state_observation('123', '2024-03-20')
    assert session.calls[0][0] == URL
    assert session.calls[0][1] == dict(timeout=(min(api.timeout, 10), min(api.timeout, 10)),
        stream=True, allow_redirects=False, headers={'Accept': 'application/json'})
    assert observation['source'] == 'tsetmc'
    assert observation['ins_code'] == '123' and observation['requested_date'] == '2024-03-20'
    assert observation['observation_kind'] == 'instrument_state'
    assert observation['content'] == BODY and observation['response_bytes'] == len(BODY)
    assert observation['body_sha256'] == hashlib.sha256(BODY).hexdigest()
    assert datetime.fromisoformat(observation['requested_at']).tzinfo is not None
    assert datetime.fromisoformat(observation['received_at']) >= datetime.fromisoformat(observation['requested_at'])
    assert 'native_identity_verified' not in observation and 'executable' not in observation
    assert response.closed
    assert api.request_profile == 'external-session-unknown' and api.timeout <= 5
    api.close()
    assert session.closed


@pytest.mark.parametrize('code,day', [
    ('', '2024-03-20'), (True, '2024-03-20'), ('123/456', '2024-03-20'),
    ('123?x=1', '2024-03-20'), ('123', '2024-3-20'), ('123', '2024-02-30'),
    ('123', '2026-10-23'), ('123', None), ('123', True),
])
def test_invalid_or_future_input_never_touches_network(code, day):
    session = Session()
    api = TsetmcProvider(session=session)
    with pytest.raises(ValueError):
        api.get_dated_instrument_state_observation(code, day)
    assert not session.calls


@pytest.mark.parametrize('response', [
    Response(status=302), Response(status=206), Response(status=500),
    Response(url='https://example.test/redirect'), Response(body=b''),
    Response(body=b'<html>unavailable</html>'), Response(body=b'{"bad":\x00}'),
    Response(headers={'Content-Length': '99999999'}),
    Response(body=b'x' * (MAX_BYTES + 1)),
    Response(stream_error=requests.ConnectionError('synthetic incomplete stream')),
])
def test_non200_redirect_partial_oversized_and_incomplete_transport_fail_closed(response):
    session = Session(response)
    api = TsetmcProvider(session=session, realtime=True)
    assert api.get_dated_instrument_state_observation('123', '2024-03-20') is None
    assert len(session.calls) == 1 and response.closed


def test_network_timeout_returns_none_without_retry_injected_session():
    session = Session(error=requests.Timeout('synthetic timeout'))
    api = TsetmcProvider(session=session, realtime=True)
    assert api.get_dated_instrument_state_observation('123', '2024-03-20') is None
    assert len(session.calls) == 1


def test_maximum_state_size_is_preserved_for_core_validation():
    body = BODY + b' ' * (MAX_BYTES - len(BODY))
    response = Response(body=body)
    api = TsetmcProvider(session=Session(response))
    observation = api.get_dated_instrument_state_observation('123', '2024-03-20')
    assert observation['content'] == body and observation['response_bytes'] == MAX_BYTES
    assert response.closed


def test_capability_is_opt_in_and_base_method_is_nonabstract():
    assert BaseDataProvider.supports_dated_instrument_states is False
    assert TsetmcProvider.supports_dated_instrument_states is True
    assert 'get_dated_instrument_state_observation' not in BaseDataProvider.__abstractmethods__
    with pytest.raises(NotImplementedError):
        BaseDataProvider.get_dated_instrument_state_observation(object(), '123', '2024-03-20')


def test_live_owned_session_keeps_existing_no_retry_profile():
    api = TsetmcProvider(realtime=True)
    try:
        assert api.request_profile == 'live-no-retry-5s-io'
        assert api.timeout <= 5
        assert api.session.get_adapter('https://').max_retries.total == 0
    finally:
        api.close()
