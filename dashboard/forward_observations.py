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
            scope = st.multiselect('نمادهای دامنه ثابت', sorted(r['symbol_fa'] for r in universe))
            rationale = st.text_area('چرا این دامنه؟ معیار زیر نظر گرفتن یا کنار گذاشتن چیست؟')
            day = st.text_input('تاریخ شمسی جلسه', value=to_jalali(next_day, fa_digits=False))
            c1, c2 = st.columns(2)
            start = c1.text_input('شروع بررسی (ساعت تهران)', '09:05')
            end = c2.text_input('پایان بررسی', '09:10')
            later = c1.text_input('شروع پیگیری', '12:00')
            deadline = c2.text_input('پایان پیگیری', '12:05')
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
    st.info('برای ثبت نقل قیمت تازه، پایش زنده را در صفحه خانه روشن نگه دار. قیمت کهنه یا غایب نامعلوم ثبت می‌شود؛ بعداً جایگزین نمی‌شود.')
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
                             'زوج قیمت معلوم': report['known_quote_pairs'], 'زوج قیمت نامعلوم': report['unknown_quote_pairs']})
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
    display = []
    names = {v: k for k, v in CHOICES_FA.items()} | {'not_recorded': 'ثبت نشده'}
    for row in report['rows']:
        display.append({'نماد': row['symbol_fa'], 'وضعیت': names[row['choice']], 'دلیل': row['rationale'],
                        'تغییر خام قیمت (%)': None if row['raw_quote_change'] is None else row['raw_quote_change'] * 100,
                        'وضعیت قیمت': 'زوج قیمت معتبر محلی' if not row['problems'] else 'نامعلوم؛ جزئیات در خروجی'})
    st.dataframe(pd.DataFrame(display), hide_index=True)
    st.warning('تغییر خام قیمت، سود خالص یا امکان پر شدن سفارش نیست. صف، هزینه، ظرفیت و حقوق اقدامات شرکتی در این عدد اثبات نمی‌شوند.')
    st.caption('ثبت فقط‌افزودنی و هش، با ساعت همین رایانه کنترل می‌شود؛ گواه زمان بیرونی یا اثبات آزمون مستقل ندارد. نسخه اول فقط مشاهده درون‌روزی است.')
    st.download_button('دریافت خصوصی پروتکل، کد و شواهد', exported, file_name=selected + '.json', mime='application/json')
