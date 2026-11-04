"""Recomputable local backoff from whitelisted public HTTP 429/503 receipts."""

from datetime import timedelta

from core.opportunity_radar import digest, stamp

VERSION = 'codal-backoff-v1'
SERVICE_VERSION = 'codal-service-backoff-v1'
DEFAULT_SECONDS = 3600
MAX_SECONDS = 2**31-1
OBSERVATION_FIELDS = ('requested_page', 'url', 'requested_at', 'received_at', 'http_status',
                      'rate_limit_retry_after_seconds', 'rate_limit_retry_after_unsupported')
STATE_FIELDS = frozenset(('version', 'source', 'observation', 'observed_at', 'not_before',
                         'manual_review_required', 'delay_seconds', 'delay_source', 'default_seconds', 'receipt_sha256'))


def _state(observation, default_seconds):
    if not isinstance(observation, dict) or set(observation) != set(OBSERVATION_FIELDS):
        raise ValueError('A whitelisted public rate-limit receipt is required')
    number = observation['requested_page']
    status = observation['http_status']
    if (type(number) is not int or not 1 <= number <= 100
            or observation['url'] != f'https://search.codal.ir/api/search/v2/q?&PageNumber={number}'
            or type(status) is not int or status not in (429, 503)):
        raise ValueError('The public rate-limit receipt identity or HTTP status is invalid')
    if type(default_seconds) is not int or not 1 <= default_seconds <= MAX_SECONDS:
        raise ValueError('A positive bounded default delay is required')
    requested, received = stamp(observation['requested_at']), stamp(observation['received_at'])
    if received < requested:
        raise ValueError('Rate-limit receipt clock precedes its request')
    seconds = observation['rate_limit_retry_after_seconds']
    unsupported = observation['rate_limit_retry_after_unsupported']
    if (type(unsupported) is not bool or seconds is not None
            and (type(seconds) is not int or not 0 <= seconds <= MAX_SECONDS)
            or unsupported and seconds is not None):
        raise ValueError('Retry delay must be an explicit supported integer or an unknown unsupported value')
    if unsupported:
        delay, source, not_before = None, 'unsupported', None
    else:
        delay, source = (default_seconds, 'fallback') if seconds is None else (seconds, 'server')
        try:
            not_before = (received + timedelta(seconds=delay)).isoformat()
        except OverflowError as exc:
            raise ValueError('Rate-limit deadline is outside the supported calendar') from exc
    return dict(version=VERSION if status == 429 else SERVICE_VERSION, source='codal', observation=dict(observation),
        observed_at=received.isoformat(), not_before=not_before, manual_review_required=unsupported,
        delay_seconds=delay, delay_source=source, default_seconds=default_seconds,
        receipt_sha256=digest(observation))


def make_backoff(page, default_seconds=DEFAULT_SECONDS, *, include_service_unavailable=True):
    """Reproduce a 429/503 deadline; v3 replay can retain its old 429-only behavior."""
    if type(include_service_unavailable) is not bool:
        raise ValueError('Service-unavailable backoff mode must be explicit')
    supported = (429, 503) if include_service_unavailable else (429,)
    if (not isinstance(page, dict) or type(page.get('http_status')) is not int or page['http_status'] not in supported):
        return None
    observation = {key: page.get(key) for key in OBSERVATION_FIELDS}
    observation['rate_limit_retry_after_unsupported'] = page.get('rate_limit_retry_after_unsupported', False)
    return _state(observation, default_seconds)


def checked_backoff(state):
    """Validate every derived field by reconstructing the state from its bound receipt."""
    if state is None:
        return None
    if (not isinstance(state, dict) or set(state) != STATE_FIELDS
            or type(state.get('manual_review_required')) is not bool
            or state.get('delay_seconds') is not None and type(state['delay_seconds']) is not int):
        raise ValueError('Invalid rate-limit backoff state schema')
    expected = _state(state['observation'], state['default_seconds'])
    if state != expected:
        raise ValueError('Rate-limit backoff state differs from its receipt')
    return expected


def deferred(state, *, at):
    """Unknown unsupported server delays stay deferred until explicitly reviewed."""
    clock = stamp(at)
    checked = checked_backoff(state)
    if checked is None:
        return False
    if clock < stamp(checked['observed_at']):
        raise ValueError('Clock rollback must not bypass the rate-limit deadline')
    return checked['manual_review_required'] or clock < stamp(checked['not_before'])
