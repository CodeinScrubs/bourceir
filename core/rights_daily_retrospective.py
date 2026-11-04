"""Historical day-level candidate discovery; native archive coverage is not established."""
import hashlib
import json
import re

from config import settings
from core.instrument_history import receipt_time
from core.rights_discovery import _nonfinite, _unique
from core.text import normalize_symbol
from core.timeutils import is_canonical_iso_date, now_tehran

VERSION = 'rights-daily-retrospective-v1'
MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 10000


class _NumericToken(str):
    """Distinguish source JSON numbers from actual strings without interpreting prices."""


def day_request(day):
    if (not is_canonical_iso_date(day) or day < settings.RESEARCH_CONFIG['holdout_start']
            or day > now_tehran().date().isoformat()):
        raise ValueError('Retrospective day must be on or after holdout and no later than today Tehran')
    return 'https://cdn.tsetmc.com/api/ClosingPrice/GetInstrmentsHistoryInDay/' + day.replace('-', '')


def assess_day(day, observation):
    url = day_request(day)
    result = dict(version=VERSION, requested_date=day, status='unavailable', source_row_count=None,
                  candidate_count=None, candidates=[], native_date_verified=False,
                  identity_verified=False, historical_universe_complete=False, accepted_for_signals=False,
                  holdout_contaminated_for_rights_hypothesis=True)
    if observation is None:
        return result
    if not isinstance(observation, dict):
        raise ValueError('Observation must be an object')
    content = observation.get('content')
    if (not isinstance(content, bytes) or not 0 < len(content) <= MAX_BYTES
            or observation.get('source') != 'tsetmc' or observation.get('url') != url
            or observation.get('requested_date') != day or observation.get('http_status') != 200
            or type(observation.get('response_bytes')) is not int or observation['response_bytes'] != len(content)
            or observation.get('body_sha256') != hashlib.sha256(content).hexdigest()):
        raise ValueError('Native response or receipt differs')
    if receipt_time(observation.get('received_at')) < receipt_time(observation.get('requested_at')):
        raise ValueError('Receipt precedes request')
    try:
        # Treat all native numbers as lexical tokens; no prices are interpreted
        # before the server's missing/valid date has been established.
        body = json.loads(content.decode('utf-8-sig'), object_pairs_hook=_unique, parse_constant=_nonfinite,
                          parse_int=_NumericToken, parse_float=_NumericToken)
        rows = body.get('closingPriceDailyHistoryWithInstDetails') if isinstance(body, dict) else None
        if not isinstance(rows, list) or len(rows) > MAX_ROWS:
            raise ValueError('Bounded native row list required')
        candidates, seen, date_verified = [], set(), bool(rows)
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('Invalid source row')
            code, symbol = row.get('insCode'), row.get('lVal18AFC')
            instrument_id = row.get('instrumentID')
            if (type(code) not in (str, _NumericToken) or not re.fullmatch(r'[1-9][0-9]{0,29}', str(code))
                    or type(symbol) is not str or not symbol.strip() or len(symbol) > 128
                    or instrument_id is not None and type(instrument_id) is not str):
                raise ValueError('Unknown source instrument identity')
            code = str(code)
            if code in seen:
                raise ValueError('Duplicate source instrument identity')
            seen.add(code)
            source_day = row.get('dEven')
            if source_day in (None, 0, '0'):
                date_verified = False
            elif type(source_day) not in (str, _NumericToken) or str(source_day) != day.replace('-', ''):
                raise ValueError('Native date conflicts with requested day')
            if normalize_symbol(symbol).strip().endswith('ح'):
                candidates.append(dict(ins_code=code, symbol=symbol, instrument_id=row.get('instrumentID')))
        result.update(status='observed' if rows else 'observed_empty_unverified', source_row_count=len(rows),
                      candidate_count=len(candidates), candidates=candidates, native_date_verified=date_verified)
        return result
    except (UnicodeError, json.JSONDecodeError, TypeError, RecursionError) as exc:
        raise ValueError('Invalid native JSON') from exc


def identity_projection(content):
    """Project only nonfinancial native identity/date fields; never retain price bytes."""
    body = json.loads(content.decode('utf-8-sig'), object_pairs_hook=_unique,
                      parse_constant=_nonfinite, parse_int=str, parse_float=str)
    rows = body['closingPriceDailyHistoryWithInstDetails']
    return {'closingPriceDailyHistoryWithInstDetails': [
        {key: row.get(key) for key in ('insCode', 'lVal18AFC', 'instrumentID', 'dEven')} for row in rows]}
