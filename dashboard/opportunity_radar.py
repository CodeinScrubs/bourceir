"""Broad factual attention view. Rendering never performs market requests."""

import pandas as pd
import streamlit as st

from core.opportunity_radar import REASONS, stamp
from core.timeutils import is_canonical_iso_date, now_tehran, to_jalali
from ops import jobs
from ops.opportunity_radar import status

PAGE = '📡 رادار فرصت‌های پژوهشی'
STATES = dict(observed='کندل معتبر',stale_session='جلسه قدیمی',unavailable='دریافت‌نشده',
    conflicting_publications='انتشارهای متعارض؛ نیازمند بررسی',
    invalid_history='تاریخچه ناسازگار',invalid_current_bar='کندل ناقص یا ناسازگار',awaiting_price_scan='در انتظار دریافت قیمت')
KINDS = dict(new_attention='مشاهده تازه',new_notice='اطلاعیه تازه',new_scope_identity='شناسه تازه در دامنه',
    attention_observed_after_gap='مشاهده دوباره پس از قطع داده',baseline_observation='مشاهده در سابقه اولیه',
    baseline_notice='اطلاعیه در سابقه اولیه')


def runtime_health(worker, heartbeat, *, checked_at):
    """Process liveness and heartbeat age do not attest market-data freshness."""
    at = (heartbeat or {}).get('heartbeat_at')
    try:
        observed = stamp(at)
        age = (checked_at - observed).total_seconds()
    except (TypeError, ValueError):
        observed, age = None, None
    running = worker.get('running') is True
    fresh = age is not None and 0 <= age <= 180
    return dict(running=running, heartbeat_at=observed, heartbeat_age_seconds=age,
                heartbeat_fresh=fresh)


def last_source_session(row):
    """A requested stale-row date is not a source session; malformed dates stay unknown."""
    value = row.get('last_observed_date')
    if is_canonical_iso_date(value):
        return value
    value = row.get('date') if row.get('status') == 'observed' else None
    return value if is_canonical_iso_date(value) else None


def price_session_summary(rows, *, requested_session):
    requested = requested_session if is_canonical_iso_date(requested_session) else None
    source_dates = [day for row in rows if (day := last_source_session(row)) is not None]
    valid = sum(row.get('status') == 'observed' and row.get('date') == requested
                for row in rows) if requested is not None else 0
    return dict(requested_session=requested, latest_source_session=max(source_dates, default=None),
                valid_target_candles=valid)


def _session_label(value):
    return to_jalali(value) if value is not None else 'نامعلوم'


def page_opportunity_radar():
    st.title(PAGE)
    st.caption('مشاهدهٔ سراسری و درخواست بررسی؛ این علامت‌ها احتمال جهش یا پیشنهاد خرید نیستند.')
    st.caption('عبور از سقف بر اساس شاخص پایانی÷مرجع محاسبه می‌شود؛ شکستِ قیمت اسمی نمودار نیست. '
               'این شاخص حقوق واقعی افزایش سرمایه و سود نقدی را تأیید نمی‌کند.')
    worker=jobs.job_status('opportunity-radar')
    new_run=st.checkbox('پس از تغییر کد، نسخه تازه ثبت شود و سابقه قبلی حفظ شود',value=False)
    if st.button('شروع پایش سراسری',disabled=worker['running']):
        args=['radar','watch']+(['--new-run'] if new_run else [])
        if jobs.start_job('opportunity-radar',args):
            st.rerun()
    if st.button('توقف پایش سراسری',disabled=not worker['running']):
        jobs.stop_job('opportunity-radar')
        st.rerun()
    st.caption('دریافت قیمت پس از ۱۳:۱۵؛ کدال هر ۱۰ دقیقه در بازهٔ ۸:۳۰ تا ۱۵ روز کاری و هر ساعت خارج آن. '
               'پایش به روشن‌بودن رایانه و دسترسی به منابع وابسته است. پس از راه‌اندازی دوباره ویندوز، دوباره شروع کنید.')
    radar_view()


@st.fragment(run_every=30)
def radar_view():
    worker=jobs.job_status('opportunity-radar')
    st.caption('وضعیت فعلی فرایند پایش: '+('در حال اجرا' if worker['running'] else 'متوقف'))
    if not worker['running']:
        st.warning('پایش پس‌زمینه فعال نیست؛ گزارش محفوظ به معنی دریافت تازه نیست.')
    try:
        data=status()
    except (ValueError,OSError,KeyError) as error:
        st.error('گزارش رادار قابل تأیید نیست: '+str(error))
        return
    if not data['active']:
        st.info('هنوز نسخه‌ای ثبت نشده است. شروع پایش، داده عمومی را در پایگاه مستقل ثبت می‌کند.')
        return
    heartbeat=data.get('heartbeat') or {}
    health=runtime_health(worker,heartbeat,checked_at=now_tehran())
    st.caption('وضعیت ثبت‌شده در آخرین ضربان: '+str(heartbeat.get('status','نامعلوم'))+' — '
               +(to_jalali(health['heartbeat_at'],with_time=True) if health['heartbeat_at'] else 'زمان نامعتبر یا نامعلوم'))
    if not health['heartbeat_fresh']:
        st.warning('ضربان فرایند کهنه یا نامعلوم است؛ سلامت دریافت تازه تأیید نمی‌شود.')
    if worker['running'] and health['heartbeat_fresh'] and heartbeat.get('status')=='collecting':
        progress = (f"کدال: {heartbeat.get('news_pages_fetched',0)} صفحه" if heartbeat.get('collection_phase')=='news'
                    else f"{heartbeat.get('completed',0)} از {heartbeat.get('total','نامعلوم')} نماد")
        st.info(f"دریافت در حال انجام است: {progress}. "
                'تا ثبت کامل چرخه، گزارش قبلی با تاریخ خودش نمایش داده می‌شود.')
    if heartbeat.get('message'):
        st.error(heartbeat['message'])
    latest=data.get('latest')
    if not latest:
        st.info('دریافت نخست هنوز ثبت نشده است.')
        return
    report=latest['report']
    rows=report['rows']
    prices=price_session_summary(rows,requested_session=report.get('price_session_date'))
    columns=st.columns(3)
    columns[0].metric('همه نمادهای حفظ‌شده',len(report['scope']))
    columns[1].metric('کندل معتبر در جلسهٔ مورد بررسی',prices['valid_target_candles'])
    columns[2].metric('نیازمند بررسی داده',len(rows)-prices['valid_target_candles'])
    st.caption('جلسهٔ مورد بررسی: '+_session_label(prices['requested_session'])+' — زمان پایان پردازش قیمت: '
               +to_jalali(report.get('price_observed_at'),with_time=True))
    retry=report.get('price_retry') or {}
    if retry.get('deferred') is True and retry.get('daily_limit_reached') is True:
        try:
            retry_at=to_jalali(stamp(retry.get('not_before')),with_time=True)
        except (TypeError,ValueError):
            retry_at='نامعلوم'
        st.warning('در زمان ثبت این چرخه، سقف روزانهٔ دریافت تاریخچه پر بود و دریافت تا '
                   +retry_at+' به تعویق افتاد. به‌روزرسانی همان روز ممکن است جا بماند.')
    if report.get('price_composition'):
        composition=report['price_composition']
        counts=composition.get('statuses',{})
        st.caption('کندل کامل افزوده از انتشار روزانه: '+str(counts.get('appended_whole_dated_candle',0))
                   +'؛ انتشار متعارض: '+str(counts.get('conflicting_publications',0))
                   +'؛ زمان دریافت اصلیِ آرشیو با زمان پردازش تازه جایگزین نمی‌شود.')
    st.caption('آخرین جلسهٔ موجود در دادهٔ منبع: '+_session_label(prices['latest_source_session'])
               +'؛ زمان دریافت پاسخ، تاریخ جلسهٔ منبع را تأیید نمی‌کند.')
    if prices['valid_target_candles']==0:
        st.warning('برای جلسهٔ مورد بررسی، هیچ کندل معتبر تأیید نشده است. آخرین جلسهٔ موجود در پاسخ منبع: '
                   +_session_label(prices['latest_source_session'])+'؛ دریافت تازهٔ پاسخ به معنی قیمت تازه نیست.')
    if prices['latest_source_session']!=now_tehran().date().isoformat():
        st.warning('قیمت‌های منبع مربوط به امروز نیستند یا تاریخشان نامعلوم است. داده قدیمیِ زمان شروع، سابقه اولیه است و '
                   'هشدار پیش‌نگر شمرده نمی‌شود.')
    news=report['news_coverage']
    rate_limit=news.get('rate_limit_state')
    if rate_limit:
        if rate_limit.get('manual_review_required'):
            st.warning('زمان انتظار کدال قابل تفسیر نیست؛ دریافت خودکار خبر تا بررسی متوقف است.')
        else:
            st.caption('وقفهٔ دریافت کدال تا '+to_jalali(rate_limit['not_before'],with_time=True)+
                       ('؛ زمان اعلام‌شدهٔ سرور.' if rate_limit['delay_source']=='server' else
                        '؛ وقفهٔ پیش‌فرض محلی، چون زمان معتبر سرور در دسترس نیست.'))
    st.caption(f"کدال: {news.get('pages_fetched')} صفحه؛ شکست صفحه: {news.get('failed_page')}؛ "
               f"رسیدن به سقف صفحه: {news.get('page_limit_reached')}. پوشش کامل اطلاعیه‌ها تأیید نشده است. "
               'عنوان اطلاعیه، اثبات بهبود سود شرکت نیست؛ متن سند باید بررسی شود.')
    if news.get('window_start'):
        st.caption('بازهٔ پیگیری خبر از '+to_jalali(news['window_start'],with_time=True)+
                   '؛ رسیدن به مرز زمانی و ثبات صفحهٔ نخست: '+
                   ('بررسی شد' if news.get('window_scan_satisfied') else 'تأیید نشد'))
        st.caption('مرز زمانیِ آخرین پیمایش محفوظ خبر: '
                   +(to_jalali(news['checkpoint_through'],with_time=True)
                     if news.get('checkpoint_through') else 'تأیید نشده')
                   +'؛ نتیجهٔ پیمایش محفوظ، تأیید دریافت زندهٔ اکنون نیست.')
        if not news.get('window_scan_satisfied'):
            st.warning('پیمایش بازهٔ خبر کامل نشده است؛ مرز قبلی برای تلاش بعدی حفظ می‌شود. '
                       'احتمال جاافتادن خبر وجود دارد. علت توقف: '+str(news.get('stop_reason') or news.get('errors')))
    coverage=report['source_coverage']
    st.caption(f"دیده‌بان عمومی: {coverage['source_scope_count']} نماد در دامنهٔ تأییدشده؛ "
               f"{len(coverage['known_missing_from_watch'])} شناسهٔ محفوظ در این دامنه پذیرفته نشده‌اند. "
               'این شمار، تعداد شناسه‌های غایب در بدنهٔ پاسخ نیست.')
    if coverage.get('native_presence_available'):
        st.caption(f"بدنهٔ دریافتی: {coverage['native_row_count']} ردیف؛ "
                   f"{len(coverage['known_native_present_codes'])} شناسهٔ محفوظ دیده شده و "
                   f"{len(coverage['known_native_absent_codes'])} شناسهٔ محفوظ در این بدنه وجود ندارد.")
        st.caption(f"از شناسه‌های محفوظِ دیده‌شده، نوع اوراق برای "
                   f"{len(coverage['known_native_present_unclassified_codes'])} شناسه نامعلوم یا متناقض است؛ "
                   f"{len(coverage['known_native_present_outside_scope_codes'])} شناسه نوع صریح خارج از دامنه دارد. "
                   'حضور شناسه، نوع معتبر ابزار یا تاریخ و تازگی قیمت را تأیید نمی‌کند.')
        if coverage['native_invalid_identity_rows'] or coverage['native_duplicate_identity_rows']:
            st.warning(f"بدنهٔ دیده‌بان شامل {coverage['native_invalid_identity_rows']} شناسهٔ نامعتبر و "
                       f"{coverage['native_duplicate_identity_rows']} ردیف تکراری شناسه است؛ این ردیف‌ها جدا بررسی می‌شوند.")
    else:
        st.caption('حضور شناسه‌ها در بدنهٔ بومی این آرشیو معلوم نیست؛ '
                   'عدم پذیرش در دامنه، نبود شناسه در بدنه را اثبات نمی‌کند.')
    episodes={(e['ins_code'],e['reason']):e for e in latest['episodes'] if e['active']}
    display=[]
    for row in rows:
        found=[key for key,value in row['reasons'].items() if value is True]
        starts=[episodes[(row['ins_code'],key)]['first_seen_at'] for key in found if (row['ins_code'],key) in episodes]
        display.append(dict(نماد=row['symbol'],شناسه=row['ins_code'],گروه_صنعت=row.get('sector_code'),
            وضعیت=STATES.get(row['status'],row['status']),جلسه_مورد_بررسی=to_jalali(row.get('date')),
            آخرین_جلسه_منبع=_session_label(last_source_session(row)),
            مشاهدات='؛ '.join(REASONS[k] for k in found),
            نامعلوم='؛ '.join(REASONS[k] for k,v in row['reasons'].items() if v is None),
            اولین_مشاهده=to_jalali(min(starts),with_time=True) if starts else '',
            پایانی_ریال=row.get('close_final_rial'),نسبت_ارزش_به_قبل=row.get('value_ratio_prior20')))
    st.subheader('تمام نمادها؛ بدون حذف خاموش یا محدودکردن به چند برنده')
    st.dataframe(pd.DataFrame(display),hide_index=True,width='stretch')
    st.subheader('رویدادها و اطلاعیه‌های تازه')
    events=data['events']
    old=st.session_state.get('radar_last_event_id')
    newest=max((e['id'] for e in events),default=0)
    fresh=[e for e in events if old is not None and e['id']>old and not e.get('baseline')]
    if fresh:
        st.toast(f'{len(fresh)} مشاهده یا اطلاعیهٔ تازه برای بررسی',icon='📡')
    st.session_state['radar_last_event_id']=newest
    if events:
        st.dataframe(pd.DataFrame([dict(نماد=e.get('symbol'),نوع=KINDS.get(e['kind'],e['kind']),
            زمان_کشف=to_jalali(e['recorded_at'],with_time=True),عنوان=e.get('title') or REASONS.get(e.get('reason'),''),
            سابقه_اولیه=e.get('baseline',False),پیوند=e.get('source_url')) for e in events]),
            hide_index=True,width='stretch',column_config={'پیوند':st.column_config.LinkColumn()})
        st.caption('نمایش حداکثر ۵۰۰ رویداد آخر؛ کل سابقه در پایگاه محفوظ است. اعلان صفحه فقط هنگام بازبودن این صفحه دیده می‌شود.')
    else:
        st.info('هنوز رویدادی ثبت نشده است؛ همهٔ نتایج منفی و نامعلوم هم در چرخه‌ها حفظ می‌شوند.')
