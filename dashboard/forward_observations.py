"""رابط دفتر مشاهده آینده؛ بدون ساخت سیگنال یا اجرای فرضی."""

import json
from datetime import timedelta

import pandas as pd
import streamlit as st

from core.timeutils import is_trading_session, jalali_to_iso, now_tehran, to_jalali
from database.db import DatabaseManager
from database.forward_observations import ForwardRepository

CHOICES_FA = {'بررسی نشده': 'not_reviewed', 'زیر نظر': 'watch', 'کنار گذاشته شد': 'skip'}
PHASE_FA = {'pending': 'هنوز نرسیده', 'open': 'مهلت ثبت باز است', 'missed': 'مهلت از دست رفت', 'recorded': 'ثبت شده'}
PROBLEM_FA = {
    'missing_snapshot': 'مظنه موجود نیست', 'capture_receipt_missing': 'رسید دریافت موجود نیست',
    'capture_receipt_time_invalid': 'ترتیب یا روز ساعت دریافت ناسازگار است',
    'capture_receipt_time_unknown': 'ساعت رسید معلوم نیست', 'capture_receipt_stale': 'رسید دریافت قدیمی است',
    'capture_observation_unavailable': 'آخرین دریافت ناموفق یا فاقد این نماد است',
    'snapshot_binding_missing': 'پیوند محتوایی معتبر برای این نسخه موجود نیست',
    'snapshot_binding_conflict': 'محتوای مظنه با رسید تطبیق ندارد',
    'snapshot_receipt_clock_conflict': 'ساعت ذخیره و دریافت ناسازگار است',
    'snapshot_receipt_clock_unknown': 'ساعت ذخیره یا دریافت معلوم نیست',
    'capture_scope_conflict': 'شناسه یا نوع سهم با برنامه تفاوت دارد',
    'capture_code_conflict': 'نسخه دریافت با برنامه تفاوت دارد',
    'capture_provider_conflict': 'منبع دریافت با قرارداد تفاوت دارد',
    'provider_timestamp_stale': 'ساعت قیمت قدیمی است', 'provider_timestamp_unknown': 'ساعت قیمت معلوم نیست',
    'provider_timestamp_future_or_other_day': 'ساعت قیمت در آینده یا روز دیگری است',
    'captured_at_stale': 'ساعت ذخیره قدیمی است', 'captured_at_unknown': 'ساعت ذخیره معلوم نیست',
    'captured_at_future_or_other_day': 'ساعت ذخیره در آینده یا روز دیگری است',
    'source_or_unit_invalid': 'منبع یا واحد قیمت معتبر نیست', 'price_outside_range': 'قیمت خارج از کمینه/بیشینه روز است',
    'no_new_quote_after_assessment': 'نقل قیمت تازه پس از بررسی موجود نیست',
    'reference_price_changed': 'قیمت مرجع تغییر کرده است', 'nonfinite_quote_change': 'تغییر قیمت قابل محاسبه نیست',
}


def problem_fa(problem):
    for phase, label in (('assessment_', 'بررسی'), ('followup_', 'پیگیری')):
        if problem.startswith(phase) and problem[len(phase):] in PHASE_FA:
            return label + ': ' + PHASE_FA[problem[len(phase):]]
    if problem.endswith('_invalid'):
        field = {'last_price': 'آخرین قیمت', 'low_price': 'کمترین قیمت', 'high_price': 'بیشترین قیمت',
                 'ref_price': 'قیمت مرجع', 'volume': 'حجم', 'value': 'ارزش معاملات'}.get(problem[:-8])
        if field:
            return field + ' نامعتبر است'
    return PROBLEM_FA.get(problem, 'نقص داده؛ جزئیات در خروجی خصوصی')


def page_forward_observations():
    st.title('🗓️ دفتر مشاهده آینده')
    st.caption('دامنه و زمان را پیشاپیش ثبت کن؛ بررسی و نقل قیمت بعدی جدا حفظ می‌شوند. این دفتر سیگنال یا سود معامله تولید نمی‌کند.')
    with DatabaseManager(read_only=True) as db:
        repo = ForwardRepository(db)
        trials = repo.list_trials()
        universe = db.get_universe_symbols()
        calendar = db.get_market_calendar()
    with st.expander('ثبت برنامه مشاهده جدید', expanded=not trials):
        next_day = now_tehran().date() + timedelta(days=1)
        while not is_trading_session(next_day, calendar):
            next_day += timedelta(days=1)
        with st.form('forward-register'):
            scope = st.multiselect('نمادهای دامنه ثابت', sorted(r['symbol_fa'] for r in universe),
                                   placeholder='نمادها را انتخاب کن')
            rationale = st.text_area('چرا این دامنه؟ معیار زیر نظر گرفتن یا کنار گذاشتن چیست؟')
            day = st.text_input('تاریخ شمسی جلسه', value=to_jalali(next_day, fa_digits=False))
            c1, c2 = st.columns(2)
            start = c1.text_input('شروع بررسی (ساعت تهران)', '09:05')
            end = c2.text_input('پایان بررسی', '09:10')
            c3, c4 = st.columns(2)
            later = c3.text_input('شروع پیگیری', '12:00')
            deadline = c4.text_input('پایان پیگیری', '12:05')
            st.caption('هر پنجره حداکثر پنج دقیقه، در یک جلسه است. تقویم تنظیم‌شده تأیید مستقل تعطیلات یا باز بودن نماد نیست.')
            if st.form_submit_button('ثبت دامنه و زمان ثابت'):
                try:
                    values = [jalali_to_iso(f'{day} {t}:00') for t in (start, end, later, deadline)]
                    schedule = dict(zip(('assessment_start', 'assessment_end', 'followup_start', 'followup_end'), values))
                    with DatabaseManager() as db:
                        ForwardRepository(db).register(symbols=scope, rationale=rationale, schedule=schedule)
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
    st.info('برای برنامه جدید، پایش زنده را پس از به‌روزرسانی دوباره از خانه شروع کن. قیمت تازه به رسید همان دریافت متصل می‌شود؛ '
            'پاسخ غایب، ناموفق یا ناسازگار و ساعت قیمت قدیمی، نامعلوم ثبت می‌شوند و بعداً جایگزین نمی‌شوند.')
    if not trials:
        st.info('هنوز برنامه‌ای ثبت نشده است. برنامه یا نتیجه نمونه در دفتر واقعی ساخته نمی‌شود.')
        return
    labels = {}
    statuses = []
    for trial in trials:
        try:
            with DatabaseManager(read_only=True) as db:
                report = ForwardRepository(db).report(trial['id'])
            labels[trial['id']] = f"{to_jalali(report['protocol']['schedule']['assessment_start'], with_time=True)} — {trial['id'][-8:]}"
            statuses.append({'برنامه': labels[trial['id']], 'دامنه': report['scope_count'],
                             'بررسی': PHASE_FA[report['phases']['assessment']], 'پیگیری': PHASE_FA[report['phases']['followup']],
                             'زوج قیمت معلوم': report['known_quote_pairs'], 'زوج قیمت نامعلوم': report['unknown_quote_pairs'],
                             'زوج قیمت با رسید متصل': report['receipt_bound_quote_pairs']})
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            st.error(f"شواهد برنامه {trial['id']} قابل تأیید نیست: {exc}")
    st.write(f'کل برنامه‌های ثبت‌شده: {len(trials)}؛ برنامه‌های جاافتاده در همین فهرست می‌مانند.')
    st.dataframe(pd.DataFrame(statuses), hide_index=True)
    if not labels:
        return
    selected = st.selectbox('برنامه مشاهده', list(reversed(labels)), format_func=labels.get)
    with DatabaseManager(read_only=True) as db:
        report = ForwardRepository(db).report(selected)
        exported = ForwardRepository(db).export(selected)
    protocol = report['protocol']
    st.write(protocol['rationale'])
    st.caption('قرارداد محفوظ: ' + report['version'] + ' | پیوند دریافت محلی، اصالت زمان بیرونی یا اجرای سفارش را ثابت نمی‌کند.')
    st.caption(' | '.join(f"{label}: {to_jalali(protocol['schedule'][key], with_time=True)}" for key, label in
                         [('assessment_start', 'شروع بررسی'), ('assessment_end', 'پایان بررسی'),
                          ('followup_start', 'شروع پیگیری'), ('followup_end', 'پایان پیگیری')]))
    if report['phases']['assessment'] == 'open':
        with st.form('forward-assessment-' + selected):
            values = pd.DataFrame([{'نماد': r['symbol_fa'], 'وضعیت': 'بررسی نشده', 'دلیل': ''} for r in report['rows']])
            edited = st.data_editor(values, hide_index=True, disabled=['نماد'], num_rows='fixed',
                                    column_config={'وضعیت': st.column_config.SelectboxColumn(options=list(CHOICES_FA), required=True)})
            st.caption('با ثبت، وضعیت همه نمادهای دامنه و داده همان لحظه حفظ می‌شود؛ «زیر نظر» توصیه خرید نیست.')
            if st.form_submit_button('ثبت یک‌باره بررسی همه دامنه'):
                assessments = {r['نماد']: dict(choice=CHOICES_FA.get(r['وضعیت']), rationale=r['دلیل']) for r in edited.to_dict('records')}
                try:
                    with DatabaseManager() as db:
                        ForwardRepository(db).record(selected, 'assessment', assessments=assessments)
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
    if report['phases']['followup'] == 'open' and st.button('ثبت یک‌باره نقل قیمت پیگیری برای همه دامنه'):
        try:
            with DatabaseManager() as db:
                ForwardRepository(db).record(selected, 'followup')
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
    display, details = [], []
    names = {v: k for k, v in CHOICES_FA.items()} | {'not_recorded': 'ثبت نشده'}
    for row in report['rows']:
        display.append({'نماد': row['symbol_fa'], 'وضعیت': names[row['choice']], 'دلیل': row['rationale'],
                        'تغییر خام قیمت (%)': None if row['raw_quote_change'] is None else row['raw_quote_change'] * 100,
                        'وضعیت قیمت': 'زوج قیمت معتبر محلی' if not row['problems'] else 'نامعلوم',
                        'علت': '؛ '.join(problem_fa(p) for p in row['problems'] if p not in
                                        ('assessment_quote_invalid', 'followup_quote_invalid'))})
        for phase, label in (('assessment', 'بررسی'), ('followup', 'پیگیری')):
            quote = row[phase]
            if quote:
                details.append({'نماد': row['symbol_fa'], 'مرحله': label,
                    'پیوند محتوای رسید': 'تأیید محلی' if quote.get('capture_receipt_verified') else 'تأیید نشده',
                    'کیفیت قیمت': 'معلوم' if not quote['problems'] else 'نامعلوم',
                    'علت': '؛ '.join(problem_fa(p) for p in quote['problems'])})
    st.dataframe(pd.DataFrame(display), hide_index=True)
    if details:
        with st.expander('جزئیات کیفیت قیمت و رسید هر مرحله'):
            st.dataframe(pd.DataFrame(details), hide_index=True)
    st.warning('تغییر خام قیمت، سود خالص یا امکان پر شدن سفارش نیست. صف، هزینه، ظرفیت و حقوق اقدامات شرکتی در این عدد اثبات نمی‌شوند.')
    st.caption('ثبت فقط‌افزودنی و هش، با ساعت همین رایانه کنترل می‌شود؛ گواه زمان بیرونی یا اثبات آزمون مستقل ندارد. مشاهده فعلاً درون‌روزی است.')
    st.download_button('دریافت خصوصی پروتکل، کد و شواهد', exported, file_name=selected + '.json', mime='application/json')
