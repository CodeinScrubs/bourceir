"""Original daily-history evidence and deterministic normalization; no price inference."""

import base64
import hashlib
import json
import logging
import math
import re
from datetime import datetime

import numpy as np
import pandas as pd

from core.timeutils import TEHRAN_TZ, deven_to_iso
from data_collector.base import DAILY_COLUMNS
from data_collector.validator import DataValidator

VERSION = 'daily-history-evidence-v1'
MAX_BYTES = 2 * 1024 * 1024
RECEIPT_FIELDS = ('version', 'source', 'price_unit', 'ins_code', 'url', 'last_n', 'requested_at',
                  'received_at', 'normalization_as_of', 'http_status', 'response_bytes', 'body_sha256')
NUMERIC_COLUMNS = {
    'open_raw': 'priceFirst', 'high_raw': 'priceMax', 'low_raw': 'priceMin',
    'close_raw': 'pDrCotVal', 'close_final_raw': 'pClosing', 'ref_price_raw': 'priceYesterday',
    'volume': 'qTotTran5J', 'value': 'qTotCap', 'trade_count': 'zTotTran',
}
logger = logging.getLogger(__name__)


def history_request(ins_code, last_n=100):
    code = str(ins_code) if type(ins_code) in (str, int) else ''
    if not re.fullmatch(r'[1-9][0-9]{0,29}', code) or type(last_n) is not int or not 1 <= last_n <= 100:
        raise ValueError('History evidence requires a positive instrument code and 1..100 requested rows')
    return code, f'https://cdn.tsetmc.com/api/ClosingPrice/GetClosingPriceDailyList/{code}/{last_n}'


def _clock(value):
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(parsed, datetime) or parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError('History normalization requires an explicit timezone-aware clock')
        return parsed.astimezone(TEHRAN_TZ)
    except (TypeError, OverflowError) as exc:
        raise ValueError('Invalid history receipt clock') from exc


def normalize_history_payload(payload, *, ins_code, as_of):
    """The legacy provider mapping/validation, with its wall clock supplied explicitly."""
    now = _clock(as_of)
    rows = (payload or {}).get('closingPriceDaily')
    if rows is None:
        return None
    # The ordinary collector uses this function directly, not only the stricter
    # byte-replay path below. A stated conflicting identity must invalidate the
    # entire response before any row can be attached to the requested symbol.
    if (not isinstance(rows, list) or any(not isinstance(row, dict) or
            ('insCode' in row and (type(row['insCode']) not in (str, int)
                                   or str(row['insCode']) != str(ins_code))) for row in rows)):
        logger.error('Instrument %s: native history contains an invalid or conflicting identity', ins_code)
        return None
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=DAILY_COLUMNS)
    for column in NUMERIC_COLUMNS.values():
        if df[column].map(lambda value: isinstance(value, (bool, np.bool_))).any():
            logger.error('Instrument %s: boolean in numeric history column %s', ins_code, column)
            return None
    df = pd.DataFrame({
        'date': df['dEven'].map(deven_to_iso),
        **{target: df[source].astype(float) for target, source in NUMERIC_COLUMNS.items()},
    })
    df = df[df['volume'] > 0].sort_values('date').reset_index(drop=True)
    today = now.date().isoformat()
    df = df[(df['date'] < today) | ((df['date'] == today) & (now.hour >= 13))].reset_index(drop=True)
    df, removed = DataValidator.validate_daily_dataframe(df)
    if removed:
        logger.warning('Instrument %s: %d invalid history records removed', ins_code, removed)
    return df[DAILY_COLUMNS]


def _validate_receipt(observation):
    if not isinstance(observation, dict):
        raise ValueError('Missing history observation')
    code, url = history_request(observation.get('ins_code'), observation.get('last_n'))
    content = observation.get('content')
    if (observation.get('version') != VERSION or observation.get('source') != 'tsetmc'
            or observation.get('price_unit') != 'Rial' or observation.get('ins_code') != code
            or observation.get('url') != url or type(observation.get('http_status')) is not int
            or observation['http_status'] != 200 or not isinstance(content, bytes) or len(content) > MAX_BYTES
            or type(observation.get('response_bytes')) is not int or observation['response_bytes'] != len(content)
            or observation.get('body_sha256') != hashlib.sha256(content).hexdigest()):
        raise ValueError('History source, identity, URL, status, size or digest differs')
    start, received, normalized = (_clock(observation.get(key))
        for key in ('requested_at', 'received_at', 'normalization_as_of'))
    if received < start or normalized != received:
        raise ValueError('History receipt clock differs from its recorded normalization clock')
    return code, normalized


def archive_observation(observation):
    """Archive only public bytes and whitelisted provenance, including malformed payloads."""
    _validate_receipt(observation)
    return {**{key: observation[key] for key in RECEIPT_FIELDS},
            'body_base64': base64.b64encode(observation['content']).decode('ascii')}


def restore_observation(archived):
    if not isinstance(archived, dict) or set(archived) != {*RECEIPT_FIELDS, 'body_base64'}:
        raise ValueError('Invalid archived history receipt schema')
    encoded = archived['body_base64']
    if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_BYTES + 2) // 3):
        raise ValueError('Archived history exceeds byte limit')
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError('Invalid archived history bytes') from exc
    observation = {key: archived[key] for key in RECEIPT_FIELDS} | dict(content=content)
    _validate_receipt(observation)
    return observation


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate key in history response')
        result[key] = value
    return result


def _nonfinite(_):
    raise ValueError('Nonfinite number in history response')


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        _nonfinite(text)
    return value


def normalize_observation(observation):
    """Replay a bounded native body; failures remain failures and unknown prices stay unknown."""
    code, as_of = _validate_receipt(observation)
    try:
        payload = json.loads(observation['content'].decode('utf-8-sig'), object_pairs_hook=_unique,
                             parse_constant=_nonfinite, parse_float=_finite_float)
        if not isinstance(payload, dict) or not isinstance(payload.get('closingPriceDaily'), list):
            raise ValueError('History response requires a native daily row list')
        for row in payload['closingPriceDaily']:
            if not isinstance(row, dict):
                raise ValueError('Native history row must be an object')
            if 'insCode' in row and (type(row['insCode']) not in (str, int) or str(row['insCode']) != code):
                raise ValueError('Native history row belongs to another instrument')
        frame = normalize_history_payload(payload, ins_code=code, as_of=as_of)
    except (UnicodeError, json.JSONDecodeError, TypeError, KeyError, OverflowError, RecursionError) as exc:
        raise ValueError('History response cannot be normalized') from exc
    if frame is None:
        raise ValueError('History response normalization rejected its numeric payload')
    return frame
