"""تطبیق محدود سود هر سهم با متن اولیه؛ نه اثبات استحقاق، وصول یا سیگنال."""

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext

from core.dividend_evidence import _amount
from core.text import normalize_fa, normalize_symbol, to_en_digits
from core.timeutils import TEHRAN_TZ, is_canonical_iso_date, jalali_to_iso

VERSION = 'dividend-primary-reconciliation-v1'
MAX_TEXT_CHARS = 1_000_000


def primary_annual_dividend(text, notice, *, end_exclusive):
    """فقط متن نمایان قالب تصمیمات مجمع سالیانه با سطر صریح DividendPerShare.

    تاریخ‌ها قبل از عدد بررسی می‌شوند؛ میلیون ریال جدول کل شرکت، واحد سطر
    سود هر سهم نیست. انتشار تاریخی/هویت پایدار با تطبیق عنوان اثبات نمی‌شود.
    """
    out = dict(version=VERSION, status='unsupported_primary_text', dps_rial=None,
               meeting_date=None, fiscal_year_end=None, symbol=None, reasons=[])
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
        subject = 'موضوع: تصمیمات مجمع عمومی عادی سالیانه'
        if subject not in joined or 'تصمیمات مجمع عمومی عادی سالیانه' not in title:
            raise ValueError('تصمیمات مجمع عادی سالیانه لازم است')
        names = [re.fullmatch(r'شرکت (.+?) - نماد:\s*(.+)', line) for line in lines]
        names = [m for m in names if m]
        if (len(names) != 1 or normalize_symbol(names[0][1]) != normalize_symbol(notice['company_name'])
                or normalize_symbol(names[0][2]) != normalize_symbol(notice['symbol_fa'])):
            raise ValueError('نام شرکت و نماد متن با اطلاعیه یکتا و برابر نیست')
        fiscal = re.findall(r'صاحبان سهام برای سال \(دوره\) مالی منتهی به\s+([0-9]{4}/[0-9]{1,2}/[0-9]{1,2})', joined)
        stated = re.findall(r'منتهی به\s+([0-9]{4}/[0-9]{1,2}/[0-9]{1,2})', title)
        meetings = re.findall(r'\bمورخ\s+([0-9]{4}/[0-9]{1,2}/[0-9]{1,2})\b', joined)
        if len(fiscal) != 1 or len(stated) != 1 or len(meetings) != 1:
            raise ValueError('سال مالی و تاریخ مجمع باید صریح و یکتا باشند')
        fiscal_day, title_day, meeting = [jalali_to_iso(v)[:10] for v in (fiscal[0], stated[0], meetings[0])]
        publication = datetime.fromisoformat(notice['published_at'])
        if publication.tzinfo is None or publication.utcoffset() is None:
            raise ValueError('زمان اظهارشده انتشار باید منطقه زمانی داشته باشد')
        published = publication.astimezone(TEHRAN_TZ).date().isoformat()
        if fiscal_day != title_day or not fiscal_day <= meeting <= published:
            raise ValueError('ترتیب تاریخ‌ها یا دوره متن و اطلاعیه ناسازگار است')
        out.update(meeting_date=meeting, fiscal_year_end=fiscal_day, symbol=normalize_symbol(notice['symbol_fa']))
        if max(fiscal_day, meeting, published) >= end_exclusive:
            return dict(out, status='withheld_by_cutoff')
        labels = [i for i, line in enumerate(lines) if line.startswith('سود نقدی هر سهم')]
        if len(labels) != 1:
            raise ValueError('سطر سود نقدی هر سهم باید یکتا باشد')
        index = labels[0]
        if (lines[index] != 'سود نقدی هر سهم (ریال)' or index == 0
                or lines[index - 1] != 'DividendPerShare' or index + 1 >= len(lines)):
            raise ValueError('معنای هر سهم و واحد ریال صریح پشتیبانی‌شده لازم است')
        amount = _amount(lines[index + 1])
        if amount is None:
            raise ValueError('رقم صریح غیرمنفی سود هر سهم لازم است')
        return dict(out, status='supported_primary_fact', dps_rial=amount)
    except (ValueError, TypeError, KeyError, InvalidOperation, OverflowError) as error:
        return dict(out, reasons=[str(error)])


def reconcile_dividend(primary, source_rows):
    """همه نسخه‌های همان مجمع در مخرج می‌مانند؛ انتخاب ردیف موافق ممنوع است."""
    out = dict(version=VERSION, status='primary_fact_unavailable', primary=primary,
               matching_rows=[], matching_row_count=0, unknown_amount_rows=0,
               cross_source_value_corroborated=False, source_completeness_verified=False,
               external_authenticity_verified=False,
               historical_identity_verified=False, original_publication_time_verified=False,
               economic_entitlements_verified=False, settlement_verified=False,
               feature_eligible=False, accepted_for_signals=False, profitability_proven=False)
    if primary.get('status') != 'supported_primary_fact':
        return out
    rows = [r for r in source_rows if r['stated_meeting_date'] == primary['meeting_date']
            and r['stated_fiscal_year_end'] == primary['fiscal_year_end']]
    target = Decimal(primary['dps_rial'])
    for row in rows:
        value = row['candidate_dps_amount']
        amount = _amount(value) if isinstance(value, str) else None
        out['matching_rows'].append(dict(source_row=row['source_row'], amount=amount,
            agrees=None if amount is None else Decimal(amount) == target,
            source_unit=row.get('amount_unit')))
    out['matching_row_count'] = len(rows)
    out['unknown_amount_rows'] = sum(r['agrees'] is None for r in out['matching_rows'])
    if not rows:
        out['status'] = 'no_matching_source_row'
    elif any(r['agrees'] is False for r in out['matching_rows']):
        out['status'] = 'source_amount_conflict'
    elif out['unknown_amount_rows']:
        out['status'] = 'source_amount_incomplete'
    else:
        out.update(status='corroborated_values', cross_source_value_corroborated=True)
    return out


def compare_cash_dividend_marks(*, prior_close_rial, ex_reference_rial, ex_close_rial, dps_rial):
    """تشخیص حسابداری ناخالص به ازای یک سهم مشروط به استحقاق؛ بازده راهبرد نیست.

    سهم واقعی ثابت و طلب به ارزش اسمی است؛ وصول و قابلیت خرج‌کردن وجه مجهول‌اند.
    فقط شکاف مرجعی که تماماً با همین رقم برابر باشد در این تشخیص مصرف می‌شود.
    """
    values = []
    for value in (prior_close_rial, ex_reference_rial, ex_close_rial, dps_rial):
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
            raise ValueError('قیمت و سود نقدی متناهی لازم است')
        try:
            number = Decimal(str(value))
        except InvalidOperation as error:
            raise ValueError('قیمت و سود نقدی متناهی لازم است') from error
        if not number.is_finite() or not 0 <= number <= 2**63 - 1:
            raise ValueError('رقم متناهی، غیرمنفی و در دامنه عددی منبع لازم است')
        values.append(number)
    prior, ref, close, dps = values
    if min(prior, ref, close) <= 0 or any(n != n.to_integral_value() for n in (prior, ref, close)):
        raise ValueError('قیمت خام صحیح و مثبت به ریال لازم است')
    out = dict(version=VERSION, reference_gap_matches_dps=prior-ref == dps,
               cash_only_model_quantity_per_original_share=None, receivable_face_rial_per_eligible_share=str(dps),
               synthetic_gross_mark_rial=None, shares_plus_receivable_gross_mark_rial=None,
               synthetic_minus_receivable_mark_rial=None, settlement_verified=False,
               spendable_dividend_cash_rial=None, entitlement_verified=False,
               fees_included=False, strategy_return=None, accepted_for_signals=False)
    if out['reference_gap_matches_dps']:
        with localcontext() as context:
            context.prec = 60
            synthetic, economic = close * prior / ref, close + dps
            out.update(cash_only_model_quantity_per_original_share='1',
                       synthetic_gross_mark_rial=str(synthetic), shares_plus_receivable_gross_mark_rial=str(economic),
                       synthetic_minus_receivable_mark_rial=str(synthetic-economic))
    return out
