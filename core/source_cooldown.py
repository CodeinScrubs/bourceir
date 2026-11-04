"""Pure, recomputable public TSETMC retry receipts; no clock calibration claims."""

import hashlib
import json
import math
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import unquote, urlsplit

from core.daily_publications import _clock

VERSION = 'tsetmc-source-cooldown-v1'
MAX_SECONDS = 2**31-1
DEFAULTS = {429: 3600, 503: 60}
OBSERVATION_FIELDS = {'url', 'requested_at', 'received_at', 'http_status',
                      'retry_after_seconds', 'retry_after_unsupported'}


def public_url(value):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise ValueError('A bounded public TSETMC URL is required')
    parts = urlsplit(value)
    if (parts.scheme != 'https' or parts.username is not None or parts.password is not None
            or parts.port is not None or parts.fragment or any(p in ('.','..') for p in unquote(parts.path).split('/')) or not (
                parts.netloc == 'cdn.tsetmc.com' and parts.path.startswith('/api/')
                or parts.netloc == 'old.tsetmc.com' and parts.path.startswith('/tsev2/data/'))):
        raise ValueError('Only fixed public TSETMC origins are admissible')
    return value


def retry_after(value, *, received_at):
    """Return bounded seconds, unknown, or raise for an unrepresentably long delay."""
    received = _clock(received_at)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.isascii() and text.isdigit():
        significant = text.lstrip('0') or '0'
        if len(significant) > 10:
            raise ValueError('Server retry delay exceeds the supported range')
        seconds = int(significant)
    elif len(text) <= 128:
        try:
            target = parsedate_to_datetime(text)
            if target.tzinfo is None or target.utcoffset() is None:
                return None
            seconds = max(0, math.ceil((target-received).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return None
    else:
        return None
    if seconds > MAX_SECONDS:
        raise ValueError('Server retry delay exceeds the supported range')
    return seconds


def make_backoff(observation):
    if not isinstance(observation, dict) or set(observation) != OBSERVATION_FIELDS:
        raise ValueError('A bounded native source-limit observation is required')
    public_url(observation['url'])
    status = observation['http_status']
    if type(status) is not int or status not in DEFAULTS:
        raise ValueError('Only HTTP429 and HTTP503 create source cooldowns')
    sent, received = (_clock(observation[key]) for key in ('requested_at', 'received_at'))
    if received < sent:
        raise ValueError('Source receipt cannot precede request admission')
    seconds, unsupported = observation['retry_after_seconds'], observation['retry_after_unsupported']
    if (type(unsupported) is not bool or unsupported and seconds is not None
            or seconds is not None and (type(seconds) is not int or not 0 <= seconds <= MAX_SECONDS)):
        raise ValueError('Retry metadata must be an explicit supported delay or unknown')
    delay = None if unsupported else DEFAULTS[status] if seconds is None else seconds
    try:
        deadline = None if delay is None else (received+timedelta(seconds=delay)).isoformat()
    except OverflowError as exc:
        raise ValueError('Source retry deadline is outside the supported calendar') from exc
    digest = hashlib.sha256(json.dumps(observation, sort_keys=True, separators=(',', ':'),
                                       allow_nan=False).encode()).hexdigest()
    return dict(version=VERSION, source='tsetmc', observation=dict(observation),
        observed_at=received.isoformat(), not_before=deadline, delay_seconds=delay,
        delay_source='unsupported' if unsupported else 'fallback' if seconds is None else 'server',
        manual_review_required=unsupported, receipt_sha256=digest)


def checked_backoff(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or 'observation' not in value
            or type(value.get('manual_review_required')) is not bool
            or value.get('delay_seconds') is not None and type(value['delay_seconds']) is not int):
        raise ValueError('Invalid shared-source cooldown schema')
    expected = make_backoff(value['observation'])
    if value != expected:
        raise ValueError('Shared-source cooldown differs from its native receipt')
    return expected


def deferred(value, *, at):
    now = _clock(at)
    value = checked_backoff(value)
    if value is None:
        return False
    if now < _clock(value['observed_at']):
        raise ValueError('Clock rollback cannot bypass a source cooldown')
    return value['manual_review_required'] or now < _clock(value['not_before'])


def stronger(previous, incoming):
    """A concurrent shorter receipt never clears a longer or unrepresentable delay."""
    previous, incoming = checked_backoff(previous), checked_backoff(incoming)
    if previous is None:
        return incoming
    if incoming is None:
        return previous
    def strength(value):
        return value['manual_review_required'], datetime.max.replace(tzinfo=_clock(value['observed_at']).tzinfo) if value['manual_review_required'] else _clock(value['not_before'])
    return incoming if strength(incoming) > strength(previous) else previous
