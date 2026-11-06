"""Development-only projection of native TSETMC index closing levels."""

import json
import math

from config import settings
from core.instrument_history import receipt_time
from core.timeutils import TEHRAN_TZ, deven_to_iso, is_canonical_iso_date

INDEX_CODES = {
    'tse_total': '32097828799138957',
    'tse_equal_weight': '67130298613737946',
    'ifb_total': '43685683301327984',
}
VERSION = 'tsetmc-development-index-v1'
MAX_BYTES = 2_000_000
MAX_ROWS = 20_000


def index_history_request(index, start, end_exclusive):
    cutoff = settings.RESEARCH_CONFIG['holdout_start']
    if (not isinstance(index, str) or index not in INDEX_CODES
            or not all(is_canonical_iso_date(day) for day in (start, end_exclusive, cutoff))
            or not start < end_exclusive <= cutoff):
        raise ValueError('A supported index and bounded pre-holdout window are required')
    code = INDEX_CODES[index]
    return code, f'https://cdn.tsetmc.com/api/Index/GetIndexB2History/{code}'


def _unique_pairs(items):
    result = dict(items)
    if len(result) != len(items):
        raise ValueError('Duplicate index response key')
    return result


def _native_date(row):
    stamp = row.get('dEven') if isinstance(row, dict) else None
    if type(stamp) is not int or not 10_000_000 <= stamp <= 99_999_999:
        raise ValueError('Native numeric index date is required')
    day = deven_to_iso(stamp)
    if not is_canonical_iso_date(day):
        raise ValueError('Invalid native index date')
    return day


def project_index_history(body, index, start, end_exclusive, *, received_at):
    """Inspect dates before numeric levels and discard the mixed-period body."""
    code, _ = index_history_request(index, start, end_exclusive)
    received_day = receipt_time(received_at).astimezone(TEHRAN_TZ).date().isoformat()
    if type(body) is not bytes or not 0 < len(body) <= MAX_BYTES:
        raise ValueError('A bounded native index response is required')
    try:
        payload = json.loads(body.decode('utf-8-sig'), object_pairs_hook=_unique_pairs)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError('Invalid native index response') from exc
    rows = payload.get('indexB2') if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise ValueError('Unsupported native index response')
    levels = {}
    for row in rows:
        day = _native_date(row)
        if day > received_day:
            raise ValueError('Index date is later than receipt date')
        if not start <= day < end_exclusive:
            continue
        points = row.get('xNivInuClMresIbs')
        identity = row.get('insCode')
        if (type(identity) not in (int, str) or str(identity) != code or day in levels
                or type(points) not in (int, float) or not 0 < points < 2**53 or not math.isfinite(points)):
            raise ValueError('Invalid index identity, duplicate date, or closing level')
        levels[day] = points
    return dict(version=VERSION, index=index, ins_code=code, start=start, end_exclusive=end_exclusive,
                unit='index_points', levels=dict(sorted(levels.items())),
                contemporaneous_publication_verified=False, completeness_verified=False)
