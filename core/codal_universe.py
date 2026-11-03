"""Public registration-notice discovery. Counts documents, never legal events."""

import hashlib
import json
from datetime import date, timedelta
from urllib.parse import urlencode

from core.codal_search import (
    MAX_BYTES,
    SOURCE_FIELDS,
    _constant,
    _integer,
    _letter,
    _native_stamp,
    _Number,
    _pairs,
    stamp,
)
from core.text import normalize_symbol
from core.timeutils import is_canonical_iso_date, to_jalali

VERSION = 'codal-registration-universe-v1'


def registration_request(*, start, end_exclusive, cutoff, page):
    if (any(not is_canonical_iso_date(x) for x in (start, end_exclusive, cutoff))
            or not start < end_exclusive <= cutoff or type(page) is not int or not 1 <= page <= 100):
        raise ValueError('invalid pre-holdout window or page')
    params = dict(FromDate=to_jalali(start, fa_digits=False),
                  ToDate=to_jalali(date.fromisoformat(end_exclusive)-timedelta(days=1), fa_digits=False),
                  LetterType=28, PageNumber=page)
    return 'https://search.codal.ir/api/search/v2/q?' + urlencode(params)


def assess_registration(content, observation, *, start, end_exclusive, cutoff, page):
    """Fail closed on ambiguous identities, timestamps, dates or response shape."""
    url = registration_request(start=start, end_exclusive=end_exclusive, cutoff=cutoff, page=page)
    if (not isinstance(observation, dict) or set(observation) != {
            'source', 'url', 'requested_at', 'received_at', 'response_bytes', 'body_sha256'}
            or observation['source'] != 'codal' or observation['url'] != url
            or not isinstance(content, bytes) or not 0 < len(content) <= MAX_BYTES
            or type(observation['response_bytes']) is not int or observation['response_bytes'] != len(content)
            or observation['body_sha256'] != hashlib.sha256(content).hexdigest()
            or stamp(observation['requested_at']) > stamp(observation['received_at'])):
        raise ValueError('invalid observation')
    try:
        payload = json.loads(content.decode('utf-8-sig'), parse_int=_Number, parse_float=_Number,
                             parse_constant=_constant, object_pairs_hook=_pairs)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ValueError('invalid source JSON') from error
    if not isinstance(payload, dict) or set(payload) != {'IsAttacker', 'Letters', 'Page', 'Total'}:
        raise ValueError('unsupported source response')
    if payload['IsAttacker'] is True:
        return {'status': 'verification_required', 'letters': [], 'total_claim': None, 'page_claim': None}
    if payload['IsAttacker'] is not False:
        raise ValueError('unknown verification state')
    rows = payload['Letters']
    total, pages = _integer(payload['Total'], zero=True), _integer(payload['Page'], zero=True)
    if not isinstance(rows, list) or len(rows) > 100 or total is None or pages is None or total < len(rows):
        raise ValueError('invalid native count')
    letters = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) - SOURCE_FIELDS:
            raise ValueError('unsupported source row')
        published = _native_stamp(row.get('PublishDateTime'))
        if not start <= published[:10] < end_exclusive or published[:10] >= cutoff:
            raise ValueError('source row outside requested window')
        if stamp(published) > stamp(observation['received_at']):
            raise ValueError('source publication time is after collection')
        if not isinstance(row.get('Symbol'), str) or not normalize_symbol(row['Symbol']):
            raise ValueError('invalid source symbol')
        letter = _letter(row, published)
        if letter['tracing_no'] in seen:
            raise ValueError('duplicate tracing number within page')
        seen.add(letter['tracing_no'])
        letters.append(letter)
    return {'status': 'observed' if rows else 'empty_page', 'letters': letters,
            'total_claim': total, 'page_claim': pages}
