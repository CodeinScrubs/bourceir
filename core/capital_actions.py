"""حقایق محدود آگهی ثبت سرمایه؛ مبلغ ثبت‌شده، تعداد سهم قابل فروش نیست."""

import re
from datetime import datetime
from decimal import Decimal, localcontext
from fractions import Fraction

from core.dividend_evidence import _amount
from core.text import normalize_fa, normalize_symbol, to_en_digits
from core.timeutils import TEHRAN_TZ, is_canonical_iso_date, jalali_to_iso

VERSION = 'capital-registration-facts-v1'
MAX_TEXT_CHARS = 1_000_000
NUMBER = r'(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?'
DAY = r'([0-9]{4}/[0-9]{1,2}/[0-9]{1,2})'


def _one(pattern, text):
    matches = re.findall(pattern, text)
    if len(matches) != 1:
        raise ValueError('فیلد سند باید صریح و یکتا باشد')
    return matches[0]


def _rial(million):
    value = _amount(million)
    if value is None:
        raise ValueError('رقم میلیون ریال معتبر نیست')
    with localcontext() as context:
        context.prec = 120
        rial = Decimal(value) * 1_000_000
    if rial != rial.to_integral_value() or rial > 2**63 - 1:
        raise ValueError('تبدیل میلیون ریال باید صحیح و در دامنه پول باشد')
    return int(rial)


def _funding(label, text):
    labels = re.findall(label, text)
    if len(labels) > 1:
        raise ValueError('رقم منبع سرمایه تکراری/مبهم است')
    if not labels:
        return None
    value = _one(label + r'\s*(.*?)\s*میلیون ریال', text).strip()
    return _rial(value) if value else None


def primary_capital_registration(text, notice, *, end_exclusive):
    """تاریخ‌ها پیش از ارقام؛ ریال فقط از واحد صریح میلیون ریال ساخته می‌شود.

    قالب پشتیبانی‌شده آگهی ثبت است، نه پیشنهاد هیئت‌مدیره یا تصمیمات مجمع.
    فیلد خالی صفر نیست؛ نسبت سرمایه صرفاً حساب است و حق یا نقدشوندگی نمی‌سازد.
    """
    out = dict(version=VERSION, status='unsupported_primary_text', reasons=[],
        symbol=None, license_date=None, meeting_date=None, registration_date=None,
        capital_before_rial=None, capital_after_rial=None, increase_rial=None,
        retained_earnings_rial=None, waived_preemption_cash_rial=None,
        retained_equals_increase=None, unassigned_after_known_funding_rial=None,
        capital_multiplier=None, increase_ratio=None,
        external_authenticity_verified=False, historical_identity_verified=False,
        original_publication_time_verified=False, funding_completeness_verified=False,
        shareholder_entitlement_verified=False, shares_credited_verified=False,
        tradability_verified=False, feature_eligible=False, accepted_for_signals=False,
        profitability_proven=False)
    try:
        if not is_canonical_iso_date(end_exclusive):
            raise ValueError('مرز توسعه استاندارد لازم است')
        if not isinstance(text, str) or len(text) > MAX_TEXT_CHARS or any(c in text for c in '\x00<>'):
            raise ValueError('متن نمایان محدود و بدون HTML لازم است')
        if not isinstance(notice, dict) or type(notice.get('tracing_no')) is not int or notice['tracing_no'] <= 0:
            raise ValueError('شماره پیگیری صریح لازم است')
        for key in ('symbol_fa', 'company_name', 'title', 'published_at'):
            if not isinstance(notice.get(key), str) or not notice[key].strip():
                raise ValueError('فراداده هویت و انتشار کامل نیست')
        lines = [normalize_fa(to_en_digits(line)).replace('ۀ', 'ه').replace('هٔ', 'ه').replace('٬', ',').replace('٫', '.')
                 for line in text.splitlines() if line.strip()]
        joined = normalize_fa(' '.join(lines).replace('\u200c', ' '))
        if (normalize_fa(notice['title']) != 'آگهی ثبت افزایش سرمایه'
                or 'موضوع: آگهی ثبت افزایش سرمایه' not in joined):
            raise ValueError('آگهی ثبت سرمایه با عنوان پشتیبانی‌شده لازم است')
        names = [re.fullmatch(r'شرکت (.+?) - نماد:\s*(.+)', line) for line in lines]
        names = [m for m in names if m]
        if (len(names) != 1 or normalize_symbol(names[0][1]) != normalize_symbol(notice['company_name'])
                or normalize_symbol(names[0][2]) != normalize_symbol(notice['symbol_fa'])):
            raise ValueError('نام شرکت و نماد در متن و اطلاعیه یکتا و برابر نیستند')
        license_day = _one(r'مجوز افزایش سرمایه به شماره مجوز [A-Za-z0-9-]+ مورخ\s*' + DAY, joined)
        meeting = _one(r'بر اساس تصمیمات مجمع عمومی فوق العاده مورخ\s*' + DAY, joined)
        registered = _one(r'در(?: در)? تاریخ\s*' + DAY + r'\s*در مرجع ثبت شرکت ها', joined)
        license_day, meeting, registered = [jalali_to_iso(v)[:10] for v in (license_day, meeting, registered)]
        published = datetime.fromisoformat(notice['published_at'])
        if published.tzinfo is None or published.utcoffset() is None:
            raise ValueError('زمان انتشار اظهارشده باید منطقه زمانی داشته باشد')
        published = published.astimezone(TEHRAN_TZ).date().isoformat()
        out.update(symbol=normalize_symbol(notice['symbol_fa']), license_date=license_day,
                   meeting_date=meeting, registration_date=registered)
        if max(license_day, meeting, registered, published) >= end_exclusive:
            return dict(out, status='withheld_by_cutoff')
        if not license_day <= meeting <= registered <= published:
            raise ValueError('ترتیب تاریخ مجوز، مجمع، ثبت و انتشار ناسازگار است')
        before, after = _one(r'افزایش سرمایه شرکت از مبلغ\s*(' + NUMBER
            + r')\s*میلیون ریال به مبلغ\s*(' + NUMBER + r')\s*میلیون ریال', joined)
        before, after = _rial(before), _rial(after)
        if not 0 < before < after:
            raise ValueError('سرمایه اولیه مثبت و سرمایه ثبت‌شده بزرگ‌تر لازم است')
        retained = _funding(r'از محل سود انباشته مبلغ', joined)
        cash = _funding(r'از آورده نقدی با سلب حق تقدم از سهامداران فعلی مبلغ', joined)
        known = sum(value for value in (retained, cash) if value is not None)
        increase = after - before
        if known > increase:
            raise ValueError('منابع معلوم از افزایش سرمایه بزرگ‌ترند')
        multiplier, ratio = Fraction(after, before), Fraction(increase, before)
        return dict(out, status='supported_primary_fact', capital_before_rial=before,
            capital_after_rial=after, increase_rial=increase, retained_earnings_rial=retained,
            waived_preemption_cash_rial=cash,
            retained_equals_increase=None if retained is None else retained == increase,
            unassigned_after_known_funding_rial=None if retained is None and cash is None else increase-known,
            capital_multiplier=dict(numerator=multiplier.numerator, denominator=multiplier.denominator),
            increase_ratio=dict(numerator=ratio.numerator, denominator=ratio.denominator))
    except (ValueError, TypeError, KeyError, OverflowError) as error:
        return dict(out, reasons=[str(error)])
