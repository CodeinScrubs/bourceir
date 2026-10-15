"""Local Farabi account observations, report imports and price reconciliation."""

import json

import pandas as pd
import streamlit as st

from core.timeutils import to_jalali
from dashboard.workbench import _account_metric_style, _toman
from data_collector.farabi import import_report
from database.broker import BrokerRepository, broker_report
from database.db import DatabaseManager
from ops import jobs


def page_broker():
    _account_metric_style()
    st.title('🏦 حساب و تطبیق فارابی')
    st.caption('نسخه محلی گزارش‌های خوانده‌شده از حساب؛ زمان برداشت با قیمت زنده تفاوت دارد. مبالغ نمایش تومان هستند.')
    with st.expander('ورود گزارش فارابی'):
        st.write('خروجی گردش حساب و خروجی سابقه سفارشات فارابی را با فایل JSON ابزار برداشت سبد وارد کن.')
        uploaded = st.file_uploader('گزارش محلی فارابی', type=['json', 'xlsx'], accept_multiple_files=True)
        if st.button('واردکردن گزارش‌ها', disabled=not uploaded):
            try:
                with DatabaseManager() as db:
                    for file in uploaded:
                        import_report(BrokerRepository(db), file.getvalue(), file.name)
                st.success('گزارش‌ها وارد شدند؛ فایل تکراری دوباره ثبت نمی‌شود.')
                st.rerun()
            except (ValueError, TypeError, KeyError) as exc:
                st.error(f'ورود متوقف شد: {exc}')
    with DatabaseManager(read_only=True) as db:
        report = broker_report(db)
    if report is None:
        st.info('هنوز اسنپ‌شات سبد وارد نشده است. فایل‌های خصوصی را از بخش بالا وارد کن.')
        return
    quote_job = jobs.job_status('broker-quotes')
    if st.button('به‌روزرسانی قیمت عمومی دارایی‌ها', disabled=quote_job['running']):
        jobs.start_job('broker-quotes', ['broker-refresh-quotes'])
        st.rerun()
    if quote_job['running']:
        st.info('قیمت عمومی دارایی‌ها در پس‌زمینه دریافت می‌شود؛ پس از پایان، صفحه را تازه کن.')
    if quote_job['started_at']:
        with st.expander('گزارش آخرین دریافت قیمت عمومی'):
            st.code('\n'.join(quote_job['log_tail']) or 'هنوز خروجی ثبت نشده است.', language=None)
    summary = report['summary']
    st.write(f"زمان برداشت: {to_jalali(report['captured_at'], with_time=True)} — {len(report['positions'])} دارایی")
    a, b = st.columns(2)
    c, d = st.columns(2)
    a.metric('خالص دارایی در فارابی', _toman(summary['net_assets_rial']))
    b.metric('قدرت خرید در فارابی', _toman(summary['buying_power_rial']))
    c.metric('سود/زیان نمای سبد فارابی', _toman(summary['broker_pnl_rial']))
    pct = summary['broker_return_pct']
    d.metric('بازده نمای سبد فارابی', 'نامعلوم' if pct is None else f'{pct:+.2f}٪')
    st.caption('سود/زیان و خالص میانگین، فیلدهای گزارش فارابی هستند؛ بازده کل حساب با واریز، برداشت و سود نقدی هنوز تأیید نشده است.')
    accounting = report['accounting_review']
    if accounting['nonpositive_cost_symbols']:
        st.info('جمع خرید یا خالص میانگین بعضی دارایی‌ها صفر یا منفی گزارش شده است. این عدد به‌تنهایی به معنی '
                'برگشت اصل سرمایه یا سود رایگان نیست؛ تاریخچه خرید، فروش و اقدامات شرکتی باید تطبیق داده شود.')
    st.caption('سهم و حق‌تقدم به‌صورت خودکار یک معامله فرض نمی‌شوند؛ مبلغ پرداخت و مهلت استفاده فقط با اطلاعیه همان رویداد قابل تأیید است.')
    if report.get('phase'):
        phase = report['phase']
        st.write(f"سرمایه مبنای مرحله جدید: {_toman(phase['capital_rial'])}؛ سقف زیان: {_toman(phase['max_loss_rial'])}؛ "
                 f"مرز توقف: {_toman(phase['floor_rial'])}")
        if phase['loss_limit_reached']:
            st.error('ارزش مشاهده‌شده به مرز توقف مرحله جدید رسیده است؛ برنامه جدید متوقف بماند.')
        st.caption('مبنای مرحله با ورود گزارش بعدی جابه‌جا نمی‌شود. ورود یا خروج وجه جدید نیازمند تعدیل مستقل مبناست؛ رسیدن به حد زیان، امکان فروش در آن قیمت را تضمین نمی‌کند.')
    positions = pd.DataFrame(report['positions'])
    def weight(value):
        return 'نامعلوم' if value is None else f'{value:.2f}٪'
    st.write(f"وزن دو دارایی بزرگ: {weight(report['concentration']['top_two_weight_pct'])}؛ "
             f"وزن ده دارایی بزرگ: {weight(report['concentration']['top_ten_weight_pct'])} از ارزش گزارش‌شده سبد")
    view = pd.DataFrame({
        'نماد': positions['symbol_fa'], 'تعداد': positions['quantity'], 'وزن سبد (٪)': positions['weight_pct'],
        'قیمت آخر فارابی (ریال)': positions['last_price_rial'], 'قیمت آخر آرشیو (ریال)': positions['archive_last_rial'],
        'تاریخ آرشیو': positions['archive_date'].map(to_jalali),
        'داخل دامنه سهام عادی پژوهش': positions['research_scope_eligible'].map({True: 'بله', False: 'خیر'}).fillna('نامعلوم'),
        'ارزش فارابی (تومان)': positions['value_rial'] / 10,
        'جمع خرید فارابی (تومان)': positions['buy_total_rial'] / 10,
        'خالص میانگین فارابی (ریال)': positions['broker_net_average_rial'],
        'سود/زیان فارابی (تومان)': positions['broker_pnl_rial'] / 10,
        'بازده فارابی (٪)': positions['broker_return_pct'],
        'وضعیت هزینه گزارش‌شده': positions['broker_cost_state'].map({
            'requires_history_reconciliation': 'صفر/منفی؛ نیازمند تطبیق سابقه',
            'positive_broker_cost': 'مثبت؛ تأیید بازده اقتصادی نیست'}),
    })
    st.dataframe(view, hide_index=True)
    st.bar_chart(positions.set_index('symbol_fa')['weight_pct'].head(10), x_label='نماد', y_label='وزن سبد (٪)')
    equal = report['price_checks'].get('matched', 0) + report['price_checks'].get('price_equal_date_unverified', 0)
    missing = report['price_checks'].get('missing_archive', 0)
    mismatch = report['price_checks'].get('price_mismatch', 0) + report['price_checks'].get('date_mismatch', 0)
    st.write(f'تطبیق قیمت آخرین معامله: {equal} برابر، {mismatch} متفاوت، {missing} فاقد قیمت آرشیو.')
    if report['price_checks'].get('price_equal_date_unverified'):
        st.info('برابری عدد قیمت تأیید شده است؛ تاریخ جلسه بازار در برداشت سبد مشخص نشده و تطبیق هم‌زمانی کامل نیست.')
    if missing or mismatch:
        st.dataframe(positions.loc[positions['price_check'].isin(['missing_archive', 'price_mismatch', 'date_mismatch']),
                                  ['symbol_fa', 'instrument_type_code', 'archive_date', 'price_check']], hide_index=True)
    st.caption('دارایی خارج از جامعه پژوهش نیز در حساب حفظ می‌شود؛ وجود آن در سبد، مجوز ورود آن به مدل معاملاتی نیست.')
    old = positions.loc[~positions['archive_session_current'], ['symbol_fa', 'archive_date']] if not positions.empty else positions
    if not old.empty:
        st.warning('قیمت عمومی بعضی دارایی‌ها مربوط به جلسه مورد انتظار نیست؛ ارزش گزارش‌شده، امکان فروش امروز را اثبات نمی‌کند.')
        old = old.copy()
        old['archive_date'] = old['archive_date'].map(to_jalali)
        st.dataframe(old.rename(columns={'symbol_fa': 'نماد', 'archive_date': 'آخرین روز آرشیو'}), hide_index=True)
    if report.get('turnover'):
        ledger = report['turnover']
        st.write(f"گردش حساب: {ledger['count']} رویداد از {to_jalali(ledger['start'])} تا {to_jalali(ledger['end'])}؛ "
                 'مانده تک‌تک ردیف‌ها با بدهکار و بستانکار تطبیق داده شده است.')
        if ledger['cash_difference_rial']:
            st.warning(f"مانده پنل و آخرین ردیف گردش حساب {ledger['cash_difference_rial']:+,} ریال اختلاف دارند؛ هر دو مقدار حفظ شده‌اند.")
        st.write(f"ورود وجه طبقه‌بندی‌شده: {_toman(ledger['classified_cash_in_rial'])}؛ "
                 f"خروج وجه طبقه‌بندی‌شده: {_toman(ledger['classified_cash_out_rial'])}")
        st.metric('دارایی منهای خالص وجه طبقه‌بندی‌شده', _toman(ledger['equity_minus_net_classified_cash_rial']))
        st.caption('این تفاضل با زیان نمای سبد فرق دارد. تفسیر آن به‌عنوان عملکرد کل، به پوشش کامل وجوه، موجودی ابتدای دوره، نبود انتقال سهم و سودهای خارج از کارگزاری وابسته است.')
        if ledger['opening_balance_rial'] is None or ledger['opening_balance_rial'] != 0 or ledger['unclassified_event_count']:
            st.warning('مانده ابتدا صفر و همه رویدادهای مالی طبقه‌بندی‌شده تأیید نشده‌اند؛ تفاضل وجه را سود کل حساب تلقی نکن.')
    if report.get('fills'):
        fills = report['fills']
        st.write(f"خروجی سابقه سفارشات: {fills['count']} ردیف معاملات؛ جمع کارمزد گزارش‌شده در همین فایل: {_toman(fills['fees_rial'])}؛ "
                 f"تعداد نام نماد نامعلوم در خروجی: {fills['missing_symbol_count']}")
        st.caption('ردیف‌های این خروجی شناسه یکتای اجرای بورس ندارند. این شمار، تعداد سفارش یا اجراهای مستقل و کامل حساب را اثبات نمی‌کند.')
    if report.get('trade_cash_comparison'):
        comparison = report['trade_cash_comparison']
        counts = comparison['status_counts']
        differences = sum(counts.get(k, 0) for k in ('amount_difference', 'only_ledger', 'only_order_export'))
        if differences:
            st.warning(f'{differences} گروه تاریخ/سمت معامله بین گردش حساب و خروجی سفارش‌ها اختلاف مبلغ یا پوشش دارند.')
        with st.expander('تطبیق مبالغ دو گزارش، بر اساس تاریخ و سمت معامله'):
            st.caption('مبلغ ضمنی خروجی سفارش‌ها = تعداد × قیمت، به‌علاوه کارمزد خرید یا منهای کارمزد فروش. برابری جمع، تطبیق تک‌اجرای بورس را ثابت نمی‌کند. اختلاف‌ها اصلاح یا حذف نشده‌اند.')
            groups = pd.DataFrame(comparison['groups'])
            if not groups.empty:
                groups['date'] = groups['date'].map(to_jalali)
                groups['side'] = groups['side'].map({'buy': 'خرید', 'sell': 'فروش'})
                groups['status'] = groups['status'].map({'only_ledger': 'فقط گردش حساب',
                    'only_order_export': 'فقط خروجی سفارشات', 'amount_difference': 'اختلاف مبلغ',
                    'equal_cash_total': 'جمع برابر', 'within_display_rounding': 'در محدوده گردکردن نمایش'})
                st.dataframe(groups.rename(columns={'date': 'تاریخ', 'side': 'سمت', 'status': 'وضعیت',
                    'ledger_cash_rial': 'مبلغ گردش (ریال)', 'order_export_implied_cash_rial': 'مبلغ ضمنی سفارشات (ریال)',
                    'difference_rial': 'اختلاف (ریال)', 'ledger_row_count': 'ردیف گردش',
                    'order_export_row_count': 'ردیف سفارشات', 'rounding_tolerance_rial': 'حد گردکردن (ریال)'}), hide_index=True)
    st.warning('بازسازی موجودی تاریخی، اقدامات شرکتی و سودهای واریزی خارج از کارگزاری کامل نیست. دفتر واقعی برای برنامه جدید تأیید نشده است.')
    st.download_button('دریافت گزارش تطبیق خصوصی', json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2),
                       file_name='brs-farabi-reconciliation.json', mime='application/json')
