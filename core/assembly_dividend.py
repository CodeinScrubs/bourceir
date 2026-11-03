"""سود مصوب در تصمیمات/خلاصه مجمع؛ نسخه اصلاحی، استحقاق و وصول یک چیز نیستند."""

import re
from datetime import datetime
from decimal import Decimal

from core.dividend_evidence import _amount
from core.text import normalize_fa, normalize_symbol, to_en_digits
from core.timeutils import TEHRAN_TZ, is_canonical_iso_date, jalali_to_iso

VERSION = 'assembly-dividend-statement-v1'
MAX_TEXT_CHARS = 1_000_000
DAY = r'([0-9]{4}/[0-9]{1,2}/[0-9]{1,2})'
FAMILY = 'تصمیمات مجمع عمومی عادی سالیانه'


def _one(pattern, text):
    matches = re.findall(pattern, text)
    if len(matches) != 1:
        raise ValueError('فیلد سند باید صریح و یکتا باشد')
    return matches[0]


def primary_assembly_dividend(text, notice, *, end_exclusive):
    """فقط رقم صریح ریال/سهم؛ تاریخ مجمع از بخش زمان و محل استخراج می‌شود.

    متن می‌تواند متن نمایان کامل یا گزیدهٔ مشخصات، بخش الف/ب و جدول مالی باشد.
    این تابع منشأ گزیده یا زمان انتشار اولیه را اثبات نمی‌کند؛ فراخوان باید شواهد
    و زمان دریافت را مستقل نگه دارد. قرارداد قدیمی dividend_reconciliation تغییر نمی‌کند.
    """
    out = dict(version=VERSION, status='unsupported_primary_text', reasons=[],
        symbol=None, meeting_date=None, fiscal_year_end=None, stated_published_at=None,
        statement_form=None, is_correction=None, prior_tracing_no=None, dps_rial=None,
        original_publication_time_verified=False, historical_identity_verified=False,
        source_completeness_verified=False, entitlement_verified=False,
        settlement_verified=False, spendable_dividend_cash_rial=None,
        feature_eligible=False, accepted_for_signals=False, profitability_proven=False)
    try:
        if not is_canonical_iso_date(end_exclusive):
            raise ValueError('مرز توسعه استاندارد لازم است')
        if not isinstance(text, str) or len(text) > MAX_TEXT_CHARS or any(c in text for c in '\x00<>'):
            raise ValueError('متن نمایان محدود و بدون HTML لازم است')
        if not isinstance(notice, dict) or type(notice.get('tracing_no')) is not int or notice['tracing_no'] < 1:
            raise ValueError('شماره پیگیری صریح لازم است')
        for key in ('symbol_fa', 'company_name', 'title', 'published_at'):
            if not isinstance(notice.get(key), str) or not notice[key].strip():
                raise ValueError('هویت و زمان اظهارشده اطلاعیه کامل نیست')
        lines = [normalize_fa(to_en_digits(line)) for line in text.splitlines() if line.strip()]
        joined = ' '.join(lines)
        title = normalize_fa(to_en_digits(notice['title']))
        form = 'summary' if title.startswith('خلاصه ' + FAMILY) else 'decision'
        family = ('خلاصه ' if form == 'summary' else '') + FAMILY
        if not title.startswith(family) or joined.count('موضوع:') != 1:
            raise ValueError('فقط تصمیمات یا خلاصه تصمیمات مجمع عادی سالیانه پشتیبانی می‌شود')
        header, meeting_text = _one(
            r'موضوع:\s*(.*?)\s*الف\s*[-–—]\s*زمان و محل برگزاری مجمع عمومی\s*:\s*'
            r'(.*?)\s*ب\s*[-–—]\s*دستور جلسه\s*:', joined)
        if not header.startswith(family):
            raise ValueError('نوع متن و عنوان اطلاعیه ناسازگار است')
        company, symbol = _one(r'شرکت (.+?) - نماد:\s*(\S+)\s*$', header)
        if (normalize_symbol(company) != normalize_symbol(notice['company_name'])
                or normalize_symbol(symbol) != normalize_symbol(notice['symbol_fa'])):
            raise ValueError('هویت شرکت و نماد متن با اطلاعیه برابر نیست')
        fiscal = _one(r'صاحبان سهام برای سال \(دوره\) مالی منتهی به\s*' + DAY, header)
        stated = _one(r'منتهی به\s*' + DAY, title)
        meeting = _one(r'\bمورخ\s*' + DAY + r'\b', meeting_text)
        fiscal, stated, meeting = [jalali_to_iso(value)[:10] for value in (fiscal, stated, meeting)]
        publication = datetime.fromisoformat(notice['published_at'])
        if publication.tzinfo is None or publication.utcoffset() is None:
            raise ValueError('زمان اظهارشده انتشار باید منطقه زمانی داشته باشد')
        publication = publication.astimezone(TEHRAN_TZ)
        if fiscal != stated or not fiscal <= meeting <= publication.date().isoformat():
            raise ValueError('ترتیب تاریخ‌ها یا دوره متن و اطلاعیه ناسازگار است')
        prior = re.findall(r'این اطلاعیه اصلاحیه اطلاعیه شماره\s*\(\s*([0-9]+)\s*\)', joined)
        corrected = 'اصلاحیه' in title
        if corrected:
            if len(prior) != 1 or not 0 < int(prior[0]) < notice['tracing_no']:
                raise ValueError('نسخه اصلاحی باید شماره اطلاعیه قبلی صریح و متفاوت داشته باشد')
        elif prior:
            raise ValueError('برچسب نسخه اصلاحی در متن و اطلاعیه ناسازگار است')
        out.update(symbol=normalize_symbol(symbol), meeting_date=meeting, fiscal_year_end=fiscal,
            stated_published_at=publication.isoformat(), statement_form=form, is_correction=corrected,
            prior_tracing_no=int(prior[0]) if corrected else None)
        if max(fiscal, meeting, publication.date().isoformat()) >= end_exclusive:
            return dict(out, status='withheld_by_cutoff')
        indices = [i for i, line in enumerate(lines) if line.startswith('سود نقدی هر سهم')]
        if len(indices) != 1:
            raise ValueError('سطر سود نقدی هر سهم باید یکتا باشد')
        index = indices[0]
        # خالی بودن خانه، EPS و میلیون ریالِ کل شرکت هرگز جانشین DPS نمی‌شوند.
        if lines[index] != 'سود نقدی هر سهم (ریال)' or index + 1 >= len(lines):
            raise ValueError('واحد صریح ریال/سهم و مقدار در سطر بعد لازم است')
        amount = _amount(lines[index + 1])
        if amount is None or Decimal(amount) > 2**63 - 1:
            raise ValueError('رقم صریح غیرمنفی و محدود سود هر سهم لازم است')
        return dict(out, status='supported_primary_fact', dps_rial=amount)
    except (ValueError, TypeError, KeyError, OverflowError) as error:
        return dict(out, reasons=[str(error)])
