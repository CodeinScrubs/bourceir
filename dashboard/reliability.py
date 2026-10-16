"""نمایش گزارش محفوظ قرارداد داده؛ اجرای بررسی سنگین فقط با کار پس‌زمینه."""

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from config import settings
from core.timeutils import is_canonical_iso_date, to_jalali
from features.provenance import feature_environment, feature_logic_fingerprint
from ops import research_preflight

MAX_REPORT_BYTES = 8 * 1024 * 1024
BLOCKERS_FA = {
    'historical_membership_unverified': 'عضویت تاریخی نمادها در جامعه بازار تأیید نشده',
    'economic_corporate_entitlements_unverified': 'سود نقدی، حق‌تقدم و زمان وصول اقدامات شرکتی تأیید نشده',
    'historical_fee_schedule_unverified': 'جدول کارمزد رسمی برای همه تاریخ‌ها تأیید نشده',
    'execution_feasibility_unverified': 'امکان پرشدن سفارش و خروج از صف تأیید نشده',
    'no_development_price_rows': 'داده قیمت دوره توسعه موجود نیست',
    'invalid_development_prices': 'داده قیمت ناسازگار در دوره توسعه',
    'invalid_development_date_alignment': 'تاریخ یا تطبیق روزانه ناسازگار',
    'environment_mismatch': 'محیط محاسبه با نسخه‌های قفل‌شده سازگار نیست',
}


def load_report(path):
    with path.open('rb') as handle:
        contents = handle.read(MAX_REPORT_BYTES + 1)
    if len(contents) > MAX_REPORT_BYTES:
        raise ValueError('گزارش بزرگ‌تر از حد مجاز نمایش است')
    report = json.loads(contents)
    if not isinstance(report, dict) or report.get('version') != research_preflight.VERSION:
        raise ValueError('نسخه گزارش پشتیبانی نمی‌شود')
    count_fields = ('universe_count', 'observed_symbols', 'price_rows', 'coherent_client_pairs', 'unknown_client_pairs')
    if any(type(report.get(k)) is not int or report[k] < 0 for k in count_fields):
        raise ValueError('شمارش‌های گزارش معتبر نیستند')
    if (report['coherent_client_pairs'] + report['unknown_client_pairs'] != report['price_rows']
            or not isinstance(report.get('blockers'), list)
            or any(not isinstance(b, str) for b in report['blockers'])
            or not isinstance(report.get('records'), list)
            or len(report['records']) != report['universe_count']
            or type(report.get('input_contract_passed')) is not bool
            or report.get('accepted_for_signals') is not False
            or report.get('profitability_proven') is not False):
        raise ValueError('قرارداد گزارش یا مخرج داده نامعلوم ناسازگار است')
    record_counts = ('price_rows', 'coherent_client_pairs', 'unknown_client_pairs', 'invalid_price_rows')
    for row in report['records']:
        if (not isinstance(row, dict) or not isinstance(row.get('symbol'), str)
                or any(type(row.get(k)) is not int or row[k] < 0 for k in record_counts)
                or row['coherent_client_pairs'] + row['unknown_client_pairs'] != row['price_rows']
                or row['invalid_price_rows'] > row['price_rows']):
            raise ValueError('ردیف یا شمارش نماد معتبر نیست')
    if any(sum(row[k] for row in report['records']) != report[k] for k in record_counts[:3]):
        raise ValueError('جمع نمادها با خلاصه گزارش سازگار نیست')
    if (not is_canonical_iso_date(report.get('end_exclusive'))
            or report['end_exclusive'] > settings.RESEARCH_CONFIG['holdout_start']):
        raise ValueError('مرز گزارش توسعه معتبر نیست')
    at = datetime.fromisoformat(report['generated_at'])
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError('زمان گزارش منطقه زمانی ندارد')
    return report


def show_reliability(job_panel):
    st.subheader('قرارداد داده پیش از پژوهش')
    st.caption('این بررسی فقط دوره توسعه را می‌خواند؛ آزمون نهایی مهر و موم می‌ماند. '
               'سازگاری عددی داده، اثبات صحت مستقل منبع یا سودآوری روش نیست.')
    job_panel('research-preflight', ['research-preflight'], 'بررسی کیفیت داده پژوهش',
              'فقط‌خواندنی؛ نتیجه در گزارش خصوصی محفوظ می‌شود. داده بازار و حساب تغییر نمی‌کند.')
    target = settings.DATA_DIR / 'private' / 'research-preflight.json'
    if not target.exists():
        st.info('هنوز گزارش قرارداد داده ایجاد نشده است؛ بررسی را اجرا کن.')
        return
    try:
        report = load_report(target)
        current_hash = hashlib.sha256(Path(research_preflight.__file__).read_bytes()
                                      .replace(b'\r\n', b'\n')).hexdigest()
        archived = (report.get('diagnostic_code_sha256') != current_hash
                    or report.get('feature_logic_sha256') != feature_logic_fingerprint()
                    or report.get('feature_environment') != feature_environment())
        st.write(f"گزارش محفوظ: {to_jalali(report['generated_at'], with_time=True)} | "
                 f"داده پیش از {to_jalali(report['end_exclusive'])}")
        st.caption('این گزارش از وضعیت زمان اجرای بررسی است؛ تغییرات بعدی پایگاه داده را خودکار تأیید نمی‌کند.')
        if archived:
            st.warning('نسخه کد تغییر کرده است؛ گزارش آرشیوی است و باید دوباره اجرا شود.')
        elif report['input_contract_passed']:
            st.info('قرارداد عددی مشاهده‌شده عبور کرده؛ آمادگی صدور سیگنال یا سودآوری هنوز تأیید نشده است.')
        else:
            st.error('قرارداد داده عبور نکرده است؛ پیش از استفاده پژوهشی، موارد ناسازگار را بررسی کن.')
        cols = st.columns(4)
        cols[0].metric('ردیف قیمت توسعه', f"{report['price_rows']:,}")
        cols[1].metric('جفت حقیقی سازگار', f"{report['coherent_client_pairs']:,}")
        cols[2].metric('جفت حقیقی نامعلوم', f"{report['unknown_client_pairs']:,}")
        invalid_prices = sum(row['invalid_price_rows'] for row in report['records'])
        cols[3].metric('ردیف قیمت ناقص/ناسازگار', f'{invalid_prices:,}')
        st.write(f"نماد دارای قیمت: {report['observed_symbols']:,} از {report['universe_count']:,}")
        st.write('موانع پذیرش پژوهش:')
        for blocker in report['blockers']:
            st.write('• ' + BLOCKERS_FA.get(blocker, 'مانع ثبت‌شده: ' + blocker))
        with st.expander('جزئیات کیفیت هر نماد'):
            frame = pd.DataFrame(report['records'])
            for field in ('date_min', 'date_max'):
                if field in frame:
                    frame[field] = frame[field].map(lambda v: to_jalali(v) if is_canonical_iso_date(v) else 'نامعلوم')
            st.dataframe(frame.rename(columns={
                'symbol': 'نماد', 'instrument_type_code': 'نوع ابزار', 'price_rows': 'ردیف قیمت',
                'client_rows': 'ردیف حقیقی/حقوقی', 'date_min': 'شروع', 'date_max': 'پایان',
                'invalid_price_rows': 'قیمت ناسازگار', 'unknown_client_pairs': 'جفت حقیقی نامعلوم',
                'coherent_client_pairs': 'جفت حقیقی سازگار', 'duplicate_dates': 'تاریخ قیمت تکراری',
                'invalid_price_dates': 'تاریخ قیمت نامعتبر', 'invalid_client_dates': 'تاریخ حقیقی نامعتبر',
                'duplicate_client_dates': 'تاریخ حقیقی تکراری',
                'invalid_optional_legal_cells': 'سلول حقوقی نامعتبر'}), hide_index=True)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        st.warning('گزارش کیفیت قابل نمایش نیست؛ بررسی را دوباره اجرا کن. ' + str(exc))
