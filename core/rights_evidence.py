"""Declared claims/cash funding in a registration notice, without inventing holder rights."""

import re
from datetime import datetime
from fractions import Fraction

from core.capital_actions import DAY, MAX_TEXT_CHARS, NUMBER, _funding, _one, _rial
from core.text import normalize_fa, normalize_symbol, to_en_digits
from core.timeutils import TEHRAN_TZ, is_canonical_iso_date, jalali_to_iso

VERSION = 'rights-registration-facts-v1'
FALSE_FLAGS = ('external_authenticity_verified', 'historical_identity_verified',
    'original_publication_time_verified', 'legal_registration_independently_verified',
    'funding_completeness_verified', 'holder_entitlement_verified', 'holder_cash_payment_verified',
    'holder_claims_usage_verified', 'shares_credited_verified', 'tradability_verified',
    'feature_eligible', 'accepted_for_signals', 'profitability_proven')


def primary_rights_registration(text, notice, *, end_exclusive):
    """Read explicit million-rial amounts only after identity and date checks.

    Some old registration templates omit the license. Absence remains None; a present
    malformed/duplicate license is an error. Combined claims/cash funding cannot tell
    how much a particular holder paid, or when that holder's shares became tradable.
    This parser deliberately leaves the existing capital-registration v1 unchanged.
    """
    out = dict(version=VERSION, status='unsupported_primary_text', reasons=[], symbol=None,
        license_date=None, license_status='not_assessed', meeting_date=None, registration_date=None,
        published_date=None, capital_before_rial=None, capital_after_rial=None, increase_rial=None,
        claims_and_cash_rial=None, retained_earnings_rial=None, waived_preemption_cash_rial=None,
        unassigned_after_known_funding_rial=None, claims_cash_equals_increase=None,
        capital_multiplier=None, increase_ratio=None, holder_cash_required_rial=None,
        holder_claims_used_rial=None, ex_date=None, holder_credit_date=None,
        **dict.fromkeys(FALSE_FLAGS, False))
    try:
        if not is_canonical_iso_date(end_exclusive):
            raise ValueError('مرز صریح و استاندارد توسعه لازم است')
        if not isinstance(text, str) or len(text) > MAX_TEXT_CHARS or any(c in text for c in '\x00<>'):
            raise ValueError('متن نمایان محدود و بدون HTML لازم است')
        if not isinstance(notice, dict) or type(notice.get('tracing_no')) is not int or notice['tracing_no'] <= 0:
            raise ValueError('شماره پیگیری صریح لازم است')
        for key in ('symbol_fa', 'company_name', 'title', 'published_at'):
            if not isinstance(notice.get(key), str) or not notice[key].strip():
                raise ValueError('هویت و انتشار اظهارشده لازم‌اند')
        lines = [normalize_fa(to_en_digits(line)).replace('ۀ', 'ه').replace('هٔ', 'ه')
                 .replace('٬', ',').replace('٫', '.') for line in text.splitlines() if line.strip()]
        joined = normalize_fa(' '.join(lines).replace('\u200c', ' '))
        if (normalize_fa(notice['title']) != 'آگهی ثبت افزایش سرمایه'
                or 'موضوع: آگهی ثبت افزایش سرمایه' not in joined):
            raise ValueError('عنوان و بدنه باید آگهی ثبت افزایش سرمایه باشند')
        names = [match for line in lines if (match := re.fullmatch(r'شرکت (.+?) - نماد:\s*(.+)', line))]
        if (len(names) != 1 or normalize_symbol(names[0][1]) != normalize_symbol(notice['company_name'])
                or normalize_symbol(names[0][2]) != normalize_symbol(notice['symbol_fa'])):
            raise ValueError('هویت یکتای شرکت و نماد در سند و فراداده برابر نیست')
        meeting = jalali_to_iso(_one(r'بر اساس تصمیمات مجمع عمومی فوق العاده مورخ\s*' + DAY, joined))[:10]
        registered = jalali_to_iso(_one(r'در(?: در)? تاریخ\s*' + DAY + r'\s*در مرجع ثبت شرکت ها', joined))[:10]
        license_day = None
        if 'مجوز' in joined:
            pattern = r'مجوز افزایش سرمایه به شماره مجوز [A-Za-z0-9-]+ مورخ\s*' + DAY
            license_day = jalali_to_iso(_one(pattern, joined))[:10]
            if 'مجوز' in re.sub(pattern, '', joined):
                raise ValueError('مجوز تکراری/مبهم است')
        published = datetime.fromisoformat(notice['published_at'])
        if published.tzinfo is None or published.utcoffset() is None:
            raise ValueError('زمان انتشار باید منطقه زمانی داشته باشد')
        published_day = published.astimezone(TEHRAN_TZ).date().isoformat()
        out.update(symbol=normalize_symbol(notice['symbol_fa']), meeting_date=meeting,
            registration_date=registered, published_date=published_day, license_date=license_day,
            license_status='not_in_primary_text' if license_day is None else 'stated_in_primary_text')
        if max(meeting, registered, published_day, license_day or meeting) >= end_exclusive:
            return dict(out, status='withheld_by_cutoff')
        if not meeting <= registered <= published_day or (license_day is not None and license_day > meeting):
            raise ValueError('ترتیب تاریخ‌های اظهارشده ناسازگار است')
        before, after = _one(r'افزایش سرمایه شرکت از مبلغ\s*(' + NUMBER
            + r')\s*میلیون ریال به مبلغ\s*(' + NUMBER + r')\s*میلیون ریال', joined)
        before, after = _rial(before), _rial(after)
        if not 0 < before < after:
            raise ValueError('دو سرمایه مثبت و افزایش اکید لازم است')
        claims_cash = _funding(r'از محل مطالبات و آورده نقدی مبلغ', joined)
        retained = _funding(r'از محل سود انباشته مبلغ', joined)
        waived = _funding(r'از آورده نقدی با سلب حق تقدم از سهامداران فعلی مبلغ', joined)
        if claims_cash is None or claims_cash <= 0:
            raise ValueError('این قرارداد مبلغ مثبت صریح مطالبات و آورده نقدی می‌خواهد')
        increase = after-before
        known = sum(value for value in (claims_cash, retained, waived) if value is not None)
        if known > increase:
            raise ValueError('جمع منابع معلوم از افزایش سرمایه بیشتر است')
        multiplier, ratio = Fraction(after, before), Fraction(increase, before)
        return dict(out, status='supported_primary_fact', capital_before_rial=before,
            capital_after_rial=after, increase_rial=increase, claims_and_cash_rial=claims_cash,
            retained_earnings_rial=retained, waived_preemption_cash_rial=waived,
            unassigned_after_known_funding_rial=increase-known,
            claims_cash_equals_increase=claims_cash == increase,
            capital_multiplier=dict(numerator=multiplier.numerator, denominator=multiplier.denominator),
            increase_ratio=dict(numerator=ratio.numerator, denominator=ratio.denominator))
    except (ValueError, TypeError, KeyError, OverflowError) as error:
        return dict(out, reasons=[str(error)])
