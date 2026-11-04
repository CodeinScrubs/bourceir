"""Research-only instrument search: bounded native evidence, no membership inference."""

import hashlib
import json
import re
from urllib.parse import quote

from core.instrument_history import receipt_time
from core.text import normalize_symbol

VERSION = 'rights-instrument-search-v1'
MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 1000


def search_request(parent_code, parent_symbol):
    code = str(parent_code) if type(parent_code) in (str, int) else ''
    if (not re.fullmatch(r'[1-9][0-9]{0,29}', code) or type(parent_symbol) is not str
            or not parent_symbol or len(parent_symbol) > 64 or normalize_symbol(parent_symbol) != parent_symbol
            or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in parent_symbol)
            or any(c in parent_symbol for c in ('/', '\\', '?', '#', '%'))):
        raise ValueError('Canonical parent symbol and numeric identity are required')
    query = parent_symbol + 'ح'
    return {'parent_code': code, 'parent_symbol': parent_symbol, 'query_symbol': query,
            'url': 'https://cdn.tsetmc.com/api/Instrument/GetInstrumentSearch/' + quote(query, safe='')}


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate native JSON key')
        value[key] = item
    return value


def _nonfinite(_):
    raise ValueError('Nonfinite native JSON number')


def assess_search(request, observation):
    """Return exact-name candidates; missing/ambiguous data never become verified membership."""
    if not isinstance(request, dict) or set(request) != {'parent_code', 'parent_symbol', 'query_symbol', 'url'}:
        raise ValueError('The exact request contract is required')
    if request != search_request(request['parent_code'], request['parent_symbol']):
        raise ValueError('Request identity or URL differs')
    result = {'version': VERSION, 'parent_code': request['parent_code'], 'parent_symbol': request['parent_symbol'],
              'query_symbol': request['query_symbol'], 'status': 'unavailable', 'source_row_count': None,
              'malformed_rows': None, 'candidate_codes': [], 'historical_universe_complete': False,
              'candidate_identity_verified': False, 'accepted_for_signals': False}
    if observation is None:
        return result
    content = observation.get('content') if isinstance(observation, dict) else None
    if (not isinstance(content, bytes) or not 0 < len(content) <= MAX_BYTES
            or observation.get('version') != VERSION or observation.get('source') != 'tsetmc'
            or observation.get('ins_code') != request['parent_code'] or observation.get('url') != request['url']
            or observation.get('parent_symbol') != request['parent_symbol']
            or observation.get('query_symbol') != request['query_symbol']
            or type(observation.get('http_status')) is not int or observation['http_status'] != 200
            or observation.get('response_bytes') != len(content)
            or observation.get('body_sha256') != hashlib.sha256(content).hexdigest()):
        raise ValueError('Search body, receipt, source, or identity differs')
    if receipt_time(observation.get('received_at')) < receipt_time(observation.get('requested_at')):
        raise ValueError('Source receipt order differs')
    try:
        payload = json.loads(content.decode('utf-8-sig'), object_pairs_hook=_unique,
                             parse_float=str, parse_constant=_nonfinite)
        rows = payload.get('instrumentSearch') if isinstance(payload, dict) else None
        if not isinstance(rows, list) or len(rows) > MAX_ROWS:
            raise ValueError('Bounded native search list required')
        matches, malformed = set(), 0
        for row in rows:
            if not isinstance(row, dict) or type(row.get('lVal18AFC')) is not str:
                malformed += 1
                continue
            if normalize_symbol(row['lVal18AFC']) != request['query_symbol']:
                continue
            code = row.get('insCode')
            if type(code) not in (str, int) or not re.fullmatch(r'[1-9][0-9]{0,29}', str(code)):
                malformed += 1
                continue
            matches.add(str(code))
        result.update(status='observed_with_invalid_rows' if malformed else
                      'observed_matches' if matches else 'observed_no_match',
                      source_row_count=len(rows), malformed_rows=malformed, candidate_codes=sorted(matches))
        return result
    except (UnicodeError, json.JSONDecodeError, TypeError, RecursionError) as error:
        raise ValueError('Invalid native search response') from error
