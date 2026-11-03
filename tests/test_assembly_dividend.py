"""همه نام‌ها و ارقام این فایل داده آزمون‌اند؛ دفتر حساب واقعی ساخته نمی‌شود."""

import pytest

from core import assembly_dividend as core


def notice(*, summary=False, correction=False):
    family = ('خلاصه ' if summary else '') + core.FAMILY
    return dict(tracing_no=20, symbol_fa='آزمون', company_name='نمونه',
        title=family + ' دوره ۱۲ ماهه منتهی به ۱۴۰۱/۱۲/۲۹' + ('(اصلاحیه)' if correction else ''),
        published_at='2023-07-22T13:24:10+03:30')


def text(*, summary=False, correction=False, amount='۱۲۰'):
    family = ('خلاصه ' if summary else '') + core.FAMILY
    correction_text = ('این اطلاعیه اصلاحیه اطلاعیه شماره ( 10 ) می باشد.\n'
        'دلایل اصلاح: اطلاعیه مورخ 1402/04/29 به دلیل عدم درج سود نقدی هر سهم اصلاح گردید.\n'
        if correction else '')
    return correction_text + '\n'.join([
        'موضوع: ' + family, 'صاحبان سهام برای سال (دوره) مالی منتهی به',
        '1401/12/29 شرکت نمونه - نماد: آزمون', 'الف- زمان و محل برگزاری مجمع عمومی :',
        'آگهی منتشره در سامانه کدال در تاریخ 1402/04/20',
        'مجمع عمومی عادی سالیانه راُس ساعت 10:00 روز پنجشنبه مورخ 1402/04/29 برگزار گردید',
        'ب– دستور جلسه :', 'ج - حاضرین', 'و- تصمیمات مجمع :', 'جدول شرکت (میلیون ریال)',
        'EarningsPerShareAfterTax', 'سود (زیان) خالص هر سهم- ریال', '۹۹۹',
        'DividendPerShare', 'سود نقدی هر سهم (ریال)', amount, 'ListedCapital', 'سرمایه', '100000'])


def fact(body=None, meta=None, cutoff='2025-03-21'):
    return core.primary_assembly_dividend(text() if body is None else body,
        notice() if meta is None else meta, end_exclusive=cutoff)


@pytest.mark.parametrize('summary', [False, True])
@pytest.mark.parametrize('correction', [False, True])
def test_supported_forms_keep_revision_date_separate_from_the_meeting(summary, correction):
    result = fact(text(summary=summary, correction=correction), notice(summary=summary, correction=correction))
    assert result['status'] == 'supported_primary_fact', result
    assert result['dps_rial'] == '120' and result['meeting_date'] == '2023-07-20'
    assert result['stated_published_at'] == '2023-07-22T13:24:10+03:30'
    assert result['is_correction'] is correction
    assert result['prior_tracing_no'] == (10 if correction else None)
    assert result['statement_form'] == ('summary' if summary else 'decision')
    assert result['spendable_dividend_cash_rial'] is None
    for flag in ('feature_eligible', 'settlement_verified', 'entitlement_verified',
                 'original_publication_time_verified', 'accepted_for_signals', 'profitability_proven'):
        assert result[flag] is False


def test_visible_browser_table_without_english_ids_preserves_the_explicit_unit():
    body = text().replace('DividendPerShare\n', '').replace('EarningsPerShareAfterTax\n', '')
    assert fact(body)['dps_rial'] == '120'


@pytest.mark.parametrize('amount,expected', [('۰', '0'), ('۰٫۰۰', '0.00'), ('۱,۲۳۴٫۵۰', '1234.50')])
def test_explicit_zero_and_decimal_are_known_but_blank_is_not(amount, expected):
    assert fact(text(amount=amount))['dps_rial'] == expected


@pytest.mark.parametrize('mutation', ['blank', 'negative', 'overflow', 'nan', 'duplicate', 'eps_only',
    'million', 'toman', 'thousand', 'issuer', 'symbol', 'period', 'two_meetings', 'no_section',
    'no_meeting', 'bad_date', 'naive', 'future_meeting', 'extraordinary', 'duplicate_subject',
    'summary_mismatch', 'unsupported_text', 'no_notice', 'bool_trace'])
def test_unknown_or_conflicting_text_is_not_a_known_dividend(mutation):
    body, meta = text(), notice()
    if mutation in ('blank', 'negative', 'overflow', 'nan'):
        body = text(amount={'blank': '', 'negative': '-120', 'overflow': str(2**63), 'nan': 'NaN'}[mutation])
    elif mutation == 'duplicate':
        body += '\nسود نقدی هر سهم (ریال)\n120'
    elif mutation == 'eps_only':
        body = body.split('DividendPerShare')[0]
    elif mutation in ('million', 'toman', 'thousand'):
        body = body.replace('سود نقدی هر سهم (ریال)', 'سود نقدی هر سهم (' +
            {'million': 'میلیون ریال', 'toman': 'تومان', 'thousand': 'هزار ریال'}[mutation] + ')')
    elif mutation in ('issuer', 'symbol'):
        meta['company_name' if mutation == 'issuer' else 'symbol_fa'] = 'دیگر'
    elif mutation == 'period':
        meta['title'] = meta['title'].replace('۱۴۰۱', '۱۴۰۰')
    elif mutation == 'two_meetings':
        body = body.replace('ب– دستور جلسه :', 'جلسه دیگر مورخ 1402/04/30 ب– دستور جلسه :')
    elif mutation == 'no_section':
        body = body.replace('الف- زمان و محل برگزاری مجمع عمومی :', '')
    elif mutation == 'no_meeting':
        body = body.replace('مورخ 1402/04/29', '')
    elif mutation == 'bad_date':
        body = body.replace('مورخ 1402/04/29', 'مورخ 1402/13/29')
    elif mutation == 'naive':
        meta['published_at'] = '2023-07-22T10:00:00'
    elif mutation == 'future_meeting':
        meta['published_at'] = '2023-07-19T10:00:00+03:30'
    elif mutation == 'extraordinary':
        body = body.replace(core.FAMILY, 'تصمیمات مجمع عمومی فوق العاده')
        meta['title'] = meta['title'].replace(core.FAMILY, 'تصمیمات مجمع عمومی فوق العاده')
    elif mutation == 'duplicate_subject':
        body += '\nموضوع: تصمیمات مجمع دیگری'
    elif mutation == 'summary_mismatch':
        meta = notice(summary=True)
    elif mutation == 'unsupported_text':
        body = '<html>' + body + '</html>'
    elif mutation == 'no_notice':
        meta = None
        result = core.primary_assembly_dividend(body, meta, end_exclusive='2025-03-21')
        assert result['status'] == 'unsupported_primary_text' and result['dps_rial'] is None
        return
    else:
        meta['tracing_no'] = True
    result = fact(body, meta)
    assert result['status'] == 'unsupported_primary_text' and result['dps_rial'] is None, result


@pytest.mark.parametrize('mutation', ['missing_prior', 'same_prior', 'multiple_prior', 'unmarked_title', 'missing_banner'])
def test_correction_lineage_cannot_be_silently_ignored(mutation):
    body, meta = text(correction=True), notice(correction=True)
    if mutation == 'missing_prior':
        body = body.replace('( 10 )', '( )')
    elif mutation == 'same_prior':
        body = body.replace('( 10 )', '( 20 )')
    elif mutation == 'multiple_prior':
        body += '\nاین اطلاعیه اصلاحیه اطلاعیه شماره ( 9 ) می باشد.'
    elif mutation == 'unmarked_title':
        meta = notice()
    else:
        body = '\n'.join(body.splitlines()[1:])
    result = fact(body, meta)
    assert result['status'] == 'unsupported_primary_text' and result['dps_rial'] is None


def test_cutoff_is_checked_before_any_financial_number_is_interpreted(monkeypatch):
    monkeypatch.setattr(core, '_amount', lambda _: pytest.fail('sealed financial amount read'))
    result = fact(text(correction=True), notice(correction=True), cutoff='2023-07-22')
    assert result['status'] == 'withheld_by_cutoff' and result['dps_rial'] is None
    assert result['meeting_date'] == '2023-07-20'


def test_the_legacy_interpreter_version_and_contract_are_preserved():
    from core import dividend_reconciliation as legacy
    from tests.test_dividend_reconciliation import fact as legacy_fact

    assert legacy.VERSION == 'dividend-primary-reconciliation-v1'
    assert legacy_fact()['dps_rial'] == '120'
