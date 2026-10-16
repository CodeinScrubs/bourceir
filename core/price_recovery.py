"""بازیابی فقط قیمت اولین/آخرین معامله فعال از مسیر کامل و تطبیق‌شده؛ بدون حدس عدد."""

import math

import pandas as pd

from core.price_quality import json_price_row, price_quality
from core.query_screener import decimal, trade_evidence
from core.timeutils import is_canonical_iso_date

VERSION = 'trade-price-recovery-v1'
RECOVERABLE = {'missing_or_nonfinite_open_raw': 'open_raw', 'missing_or_nonfinite_close_raw': 'close_raw'}


def recovery_candidate(issue, observation, *, cutoff):
    """این برداشت امروز است؛ دسترسی به نسخه اصلاح‌شده در روز تاریخی اثبات نمی‌شود."""
    result = dict(version=VERSION, status='not_recoverable', before=issue['raw'], after=None,
                  changed_fields=[], reconciliation=None, applied=False,
                  source_independently_verified=False, historical_availability_verified=False,
                  price_basis='first_and_last_surviving_trade_in_reconciled_dated_history')
    raw = issue['raw']
    if (not is_canonical_iso_date(cutoff) or not is_canonical_iso_date(issue.get('date'))
            or issue['date'] >= cutoff or issue['date'] != raw.get('date') or raw != json_price_row(raw)):
        raise ValueError('تاریخ و داده بازیابی باید معتبر و پیش از مرز مهر و موم باشند')
    _, flags = price_quality(pd.DataFrame([raw]))
    reasons = flags.columns[flags.iloc[0]].tolist()
    if not reasons or not set(reasons) <= RECOVERABLE.keys():
        return result | dict(reasons=reasons)
    daily = raw | dict(source='tsetmc', price_unit='Rial', ins_code=issue['ins_code'])
    checked = trade_evidence(observation, daily)
    result.update(reconciliation=checked.get('reconciliation'), issues=checked['issues'],
                  payload_date_verified=checked['payload_date_verified'],
                  payload_identity_verified=checked['payload_identity_verified'],
                  canceled_revision_rows=checked['canceled_revision_rows'])
    checks = checked.get('reconciliation') or {}
    if checked['contradictory'] or not all(checks.get(k) is True for k in ('count', 'volume', 'value', 'low', 'high')):
        return result | dict(status='unverified_trade_path')
    for field, check in (('open_raw', 'open'), ('close_raw', 'last')):
        if raw[field] is not None and checks.get(check) is not True:
            return result | dict(status='unverified_known_endpoint')
    # مسیر قبلاً شماره/ساعت/ابطال را بررسی کرده؛ نسخه یکسان تکراری فقط یک معامله است.
    active = {int(decimal(row['nTran'])): row for row in observation['response']['tradeHistory'] if row['canceled'] == 0}
    ordered = [active[number] for number in sorted(active)]
    after = dict(raw)
    changes = []
    for field, row in (('open_raw', ordered[0]), ('close_raw', ordered[-1])):
        if raw[field] is None:
            value = decimal(row['pTran'])
            if value is None or not math.isfinite(float(value)):
                return result | dict(status='unrepresentable_trade_price')
            after[field] = float(value)
            changes.append(field)
    _, repaired_flags = price_quality(pd.DataFrame([after]))
    if repaired_flags.any(axis=None):
        return result | dict(status='candidate_still_invalid')
    return result | dict(status='verified_same_source_candidate', after=after, changed_fields=changes,
                         date_binding='payload' if checked['payload_date_verified'] and checked['payload_identity_verified']
                         else 'dated_request_and_full_known_daily_reconciliation')
