"""مشاهده منبع سود نقدی؛ رقم مصوب، وجه وصول‌شده یا تغییر قیمت مرجع نیست."""

import re
from collections import Counter
from decimal import Decimal
from urllib.parse import quote

from core.text import normalize_symbol, to_en_digits
from core.timeutils import is_canonical_iso_date, jalali_to_iso

VERSION = 'dividend-source-evidence-v1'
MAX_BYTES = 2 * 1024 * 1024


def dividend_request(ins_code, symbol):
    code = str(ins_code) if type(ins_code) in (str, int) else ''
    if not re.fullmatch(r'[1-9][0-9]{0,29}', code) or not isinstance(symbol, str):
        raise ValueError('شناسه مثبت و نام نماد لازم است')
    symbol = normalize_symbol(symbol)
    if not re.fullmatch(r'[\u0621-\u063a\u0641-\u064a\u067e\u0686\u0698\u06a9\u06af\u06cc]{1,40}', symbol):
        raise ValueError('نام سهام عادی بدون عدد، فاصله یا نشانه لازم است')
    query = symbol.translate(str.maketrans({'ی': 'ي', 'ک': 'ك'}))
    return code, symbol, 'https://old.tsetmc.com/tsev2/data/DPSData.aspx?s=' + quote(query, safe='')


def _day(value):
    value = to_en_digits(value).strip()
    if not re.fullmatch(r'[0-9]{4}/[0-9]{1,2}/[0-9]{1,2}', value):
        return None
    try:
        return jalali_to_iso(value)[:10]
    except (ValueError, OverflowError):
        return None


def _amount(value):
    value = to_en_digits(value).strip().replace('٬', ',').replace('٫', '.')
    if not re.fullmatch(r'(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?', value) or len(value) > 100:
        return None
    result = Decimal(value.replace(',', ''))
    return str(result) if result.is_finite() else None


def parse_dividend_source(content, *, end_exclusive):
    """هفت ستون منبع نامزد؛ تاریخ‌ها پیش از خواندن رقم بررسی می‌شوند.

    ستون آخر فقط نامزد DPS است. واحد، هویت، اولین انتشار و تعلق به دارنده
    تأیید نمی‌شوند. ردیف ناسازگار/اصلاحی حذف یا به مبلغ صفر تبدیل نمی‌شود.
    """
    if not is_canonical_iso_date(end_exclusive) or not isinstance(content, bytes) or len(content) > MAX_BYTES:
        raise ValueError('مرز استاندارد و پاسخ بایتی محدود لازم است')
    try:
        text = content.decode('utf-8-sig').strip()
    except UnicodeError as exc:
        raise ValueError('رمزگذاری پاسخ منبع معتبر نیست') from exc
    if '<' in text or '>' in text or '\x00' in text:
        raise ValueError('صفحه HTML، کپچا یا محتوای اجرایی، جدول سود نقدی نیست')
    raw_rows = text.split(';') if text else []
    if raw_rows and not raw_rows[-1].strip():
        raw_rows.pop()
    if len(raw_rows) > 10000:
        raise ValueError('شمار ردیف منبع از سقف گذشت')
    rows, quarantined, withheld = [], [], 0
    for index, raw in enumerate(raw_rows):
        fields = raw.strip().split('@')
        if len(fields) != 7:
            quarantined.append(dict(source_row=index, reason='unexpected_column_count'))
            continue
        published, meeting, fiscal = (_day(value) for value in fields[:3])
        if any(day and day >= end_exclusive for day in (published, meeting, fiscal)):
            withheld += 1
            continue
        if not all((published, meeting, fiscal)):
            quarantined.append(dict(source_row=index, reason='unknown_or_invalid_date'))
            continue
        if not fiscal <= meeting <= published:
            quarantined.append(dict(source_row=index, reason='date_order_conflict'))
            continue
        amount = _amount(fields[-1])
        rows.append(dict(source_row=index, stated_publication_date=published, stated_meeting_date=meeting,
            stated_fiscal_year_end=fiscal, candidate_dps_amount=amount, amount_unit=None,
            amount_status='unknown' if amount is None else 'observed_zero' if Decimal(amount) == 0 else 'observed_positive',
            original_publication_time=None, identity_attestation='request_symbol_only',
            statement_dates_verified=False, amount_unit_verified=False, per_share_meaning_verified=False,
            duplicate_meeting=False, conflicting_amounts=False, feature_eligible=False, entitlement_verified=False))
    groups = {}
    for row in rows:
        groups.setdefault((row['stated_meeting_date'], row['stated_fiscal_year_end']), []).append(row)
    for group in groups.values():
        for row in group:
            row['duplicate_meeting'] = len(group) > 1
            row['conflicting_amounts'] = len({Decimal(r['candidate_dps_amount']) for r in group
                                              if r['candidate_dps_amount'] is not None}) > 1
    return dict(version=VERSION, status='observed_unverified' if raw_rows else 'empty_response', end_exclusive=end_exclusive,
        source_rows=len(raw_rows), development_rows=len(rows), quarantined_rows=quarantined,
        withheld_rows=withheld, rows=rows, amount_status_counts=dict(Counter(row['amount_status'] for row in rows)),
        source_identity_verified=False, source_completeness_verified=False, economic_entitlements_verified=False,
        settlement_verified=False, feature_eligible=False, accepted_for_signals=False, profitability_proven=False)
