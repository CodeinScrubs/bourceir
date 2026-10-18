"""شاهد مشخصات تاریخی؛ تاریخِ نشانی، تاریخِ تصریح‌شده پاسخ یا زمان انتشار نیست."""

import math
import re
from datetime import datetime

from core.text import normalize_symbol
from core.timeutils import is_canonical_iso_date

VERSION = 'instrument-history-evidence-v1'


def history_request(ins_code, trade_date):
    code = str(ins_code) if type(ins_code) in (str, int) else ''
    if not re.fullmatch(r'[1-9][0-9]{0,29}', code) or not is_canonical_iso_date(trade_date):
        raise ValueError('شناسه عددی مثبت و تاریخ میلادی استاندارد لازم است')
    return code, f'https://cdn.tsetmc.com/api/Instrument/GetInstrumentHistory/{code}/{trade_date.replace("-", "")}'


def receipt_time(value):
    if not isinstance(value, str):
        raise ValueError('زمان دریافت باید دارای منطقه زمانی باشد')
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('زمان دریافت باید دارای منطقه زمانی باشد')
    return parsed


def _integer(value):
    if type(value) is int:
        return value if 0 < value <= 10**18 else None
    if type(value) is str and re.fullmatch(r'[0-9]{1,19}', value):
        number = int(value)
        return number if 0 < number <= 10**18 else None
    if type(value) is float and math.isfinite(value) and 0 < value < 2**53 and value.is_integer():
        return int(value)
    return None


def assess_instrument_history(observation, ins_code, trade_date):
    """فقط فیلدهای مشاهده‌شده؛ تاریخ/هویت غایب و مقدار نامعلوم با پیش‌فرض پر نمی‌شوند."""
    code, url = history_request(ins_code, trade_date)
    result = dict(status='unavailable', shares_outstanding=None, base_volume=None, response_symbol=None,
        identity_attestation='unavailable', date_attestation='unavailable', candidate_fields_complete=False,
        feature_eligible=False, historical_membership_verified=False, original_publication_time=None,
        accepted_for_signals=False, notes=[])
    if observation is None:
        return result
    if (not isinstance(observation, dict) or observation.get('source') != 'tsetmc'
            or observation.get('url') != url or observation.get('ins_code') != code
            or observation.get('requested_date') != trade_date):
        raise ValueError('هویت یا نشانی مشاهده با درخواست محفوظ سازگار نیست')
    receipt_time(observation.get('received_at'))
    payload = observation.get('payload')
    info = payload.get('instrumentHistory') if isinstance(payload, dict) else None
    if not isinstance(info, dict) or not info:
        result['status'] = 'invalid_schema'
        return result
    identity = info.get('insCode')
    result['identity_attestation'] = ('request_url_only' if identity in (None, 0, '0') and type(identity) is not bool
        else 'response_matches_request' if type(identity) in (int, str) and str(identity) == code
        else 'conflicting_response')
    # lastDate تاریخ رکورد نیست؛ زمان دریافت و تاریخ URL نیز جای dEven نمی‌نشینند.
    stated = info.get('dEven')
    if stated in (None, 0, '0') and type(stated) is not bool:
        result['date_attestation'] = 'request_url_only'
    elif type(stated) in (str, int) and str(stated) == trade_date.replace('-', ''):
        result['date_attestation'] = 'response_matches_request'
    else:
        result['date_attestation'] = 'conflicting_response'
    result['status'] = ('identity_conflict' if result['identity_attestation'] == 'conflicting_response'
        else 'date_conflict' if result['date_attestation'] == 'conflicting_response' else 'observed_unverified')
    if result['status'] == 'observed_unverified':
        result['shares_outstanding'] = _integer(info.get('zTitad'))
        result['base_volume'] = _integer(info.get('baseVol'))
        symbol = info.get('lVal18AFC')
        result['response_symbol'] = normalize_symbol(symbol) if isinstance(symbol, str) and symbol.strip() else None
    result['candidate_fields_complete'] = result['shares_outstanding'] is not None and result['base_volume'] is not None
    if result['date_attestation'] == 'request_url_only':
        result['notes'].append('تاریخ فقط در نشانی درخواست است؛ lastDate به تاریخ رکورد تعبیر نشد')
    result['notes'].append('زمان دریافت، زمان انتشار تاریخی نیست؛ مشاهده به فیچر یا حقوق شرکتی ارتقا نیافت')
    return result
