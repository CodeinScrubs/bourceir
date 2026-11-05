"""Local gateways to existing capabilities, monitoring and bounded report browsing."""

import hashlib
import math
from pathlib import Path

import streamlit as st

from config import settings
from dashboard.board_alerts import board_alert_panel
from dashboard.command_center import command_center_panel
from dashboard.navigation import BY_SLUG, open_page
from ops.dashboard_catalog import (
    capability_catalog,
    contained,
    document_catalog,
    filter_tools,
    help_command,
    list_library,
    read_library_file,
    report_preview,
)


@st.cache_data(ttl=60, show_spinner=False)
def _tools(base):
    return capability_catalog(base)


@st.cache_data(ttl=60, show_spinner=False)
def _documents(base):
    return document_catalog(base)


def page_tools():
    st.title('🧰 همه ابزارها')
    st.caption('فهرست از فرمان‌های واقعی برنامه و ورودی‌های تخصصی ops و research ساخته می‌شود. '
               'بازکردن این صفحه هیچ ابزار، پژوهش یا دریافت شبکه‌ای را اجرا نمی‌کند.')
    tools = _tools(str(settings.BASE_DIR))
    first, second = st.columns(2)
    first.metric('فرمان اصلی', sum(tool.group == 'فرمان اصلی' for tool in tools))
    second.metric('ورودی تخصصی و پژوهشی', sum(tool.group != 'فرمان اصلی' for tool in tools))
    view = st.radio('نمای کتابخانه ابزار', ['ابزارها', 'راهنماها'], horizontal=True, key='hub-tool-view')
    if view == 'راهنماها':
        _guides()
        return
    query = st.text_input('جست‌وجوی ابزار', placeholder='حق تقدم، صف، افزایش سرمایه، backtest…', key='hub-tool-query')
    groups = ['همه', 'فرمان اصلی', 'ابزار تخصصی', 'پژوهش پیشرفته']
    group = st.selectbox('نوع ابزار', groups, key='hub-tool-group')
    found = filter_tools(tools, query, None if group == 'همه' else group)
    st.caption(f'{len(found)} ابزار مطابق جست‌وجو؛ ابزار پیشرفته لزوماً فرم آماده در داشبورد ندارد.')
    if not found:
        st.info('ابزاری مطابق این جست‌وجو پیدا نشد.')
        return
    index = {tool.key: tool for tool in found}
    key = st.selectbox('ابزار', list(index), format_func=lambda key: index[key].description + ' — ' + index[key].name,
                       key='hub-tool-selected')
    tool = index[key]
    with st.container(border=True):
        st.subheader(tool.description)
        st.caption(tool.access)
        if tool.page != 'tools':
            st.button('بازکردن بخش مرتبط: ' + BY_SLUG[tool.page].label, on_click=open_page, args=(tool.page,),
                      key='hub-tool-open', width='stretch')
            st.caption('صفحه مرتبط، مسیر اصلی همان قابلیت است؛ ممکن است همه گزینه‌های تخصصی فرمان را نداشته باشد.')
        else:
            st.info('این ابزار تنظیمات تخصصی دارد؛ راهنمای فرمان را ببین و ورودی و شواهد لازم را آماده کن.')
        st.code(help_command(tool), language='powershell', wrap_lines=True)
        st.caption('این فرمان فقط راهنما را نشان می‌دهد. کدِ گزارش یا ضمیمه از داخل داشبورد اجرا نمی‌شود. '
                   'بازکردن دوره مهروموم یا اعمال اصلاح داده نیازمند روند صریح همان ابزار است.')
    st.button('بازکردن خروجی‌ها و رسیدهای محفوظ', on_click=open_page, args=('reports',), key='hub-tools-reports')
    with st.expander('فهرست تمام نتایج جست‌وجو'):
        st.dataframe([{'ابزار': tool.name, 'کاربرد': tool.description, 'نوع': tool.group,
                       'دسترسی': tool.access} for tool in found], hide_index=True, width='stretch')


def _guides():
    documents = _documents(str(settings.BASE_DIR))
    query = st.text_input('جست‌وجوی راهنما', key='hub-guide-query').strip().casefold()
    found = [item for item in documents if query in (item['title']+' '+item['relative']).casefold()]
    if not found:
        st.info('راهنمایی مطابق این جست‌وجو پیدا نشد.')
        return
    choices = {item['relative']: item for item in found}
    selected = st.selectbox('راهنما', list(choices), index=None, placeholder='یک راهنما انتخاب کن',
                            format_func=lambda key: choices[key]['title'], key='hub-guide-selected')
    if selected is None:
        return
    try:
        name, body = read_library_file(settings.BASE_DIR, selected)
        st.caption(selected)
        st.download_button('دریافت راهنما', body, file_name=name, key='hub-guide-download')
        preview, value = report_preview(name, body)
        if preview == 'text':
            st.code(value, language='markdown', wrap_lines=True)
        else:
            st.caption('برای بررسی متن کامل، فایل را دریافت کن.')
    except (ValueError, OSError) as exc:
        st.warning(str(exc))


def _enter_library(relative):
    st.session_state['hub-library-relative'] = '' if relative == '.' else relative
    st.session_state.pop('hub-library-open-file', None)
    st.session_state['hub-library-filter'] = ''


def page_reports():
    st.title('🗂️ گزارش‌ها و آرشیو')
    st.caption('فایل‌های محلیِ گزارش و شواهد؛ وجود فایل یا تاریخ تغییر آن، اعتبار نتیجه یا تازگی بازار را ثابت نمی‌کند. '
               'فایل HTML از این صفحه دانلود می‌شود و در داشبورد اجرا نمی‌شود.')
    roots = {'گزارش‌ها و شواهد خصوصی': settings.DATA_DIR/'private',
             'پژوهش‌های محفوظ': settings.DATA_DIR/'research', 'مستندات پروژه': settings.BASE_DIR/'docs'}
    root_name = st.selectbox('کتابخانه', list(roots), key='hub-library-root', on_change=_enter_library, args=('',))
    root = roots[root_name]
    if not root.is_dir():
        st.info('این کتابخانه هنوز ساخته نشده است.')
        return
    relative = st.session_state.get('hub-library-relative', '')
    st.caption('پوشه فعلی: ' + (relative or 'ریشه کتابخانه'))
    if relative:
        parent = Path(relative).parent.as_posix()
        st.button('↩ پوشه بالاتر', on_click=_enter_library, args=('' if parent == '.' else parent,), key='hub-library-up')
    with st.expander('رفتن مستقیم به مسیر داخل کتابخانه'):
        target = st.text_input('مسیر نسبی پوشه', placeholder='مثلاً rights-history', key='hub-library-path')
        if st.button('رفتن به پوشه', key='hub-library-go'):
            try:
                checked = contained(root, target)
                if not checked.is_dir():
                    raise ValueError('پوشه وجود ندارد')
                _enter_library(checked.relative_to(root.resolve()).as_posix())
                st.rerun()
            except (ValueError, OSError) as exc:
                st.warning(str(exc))
    query = st.text_input('جست‌وجو در همین پوشه', key='hub-library-filter')
    try:
        listing = list_library(root, relative, query=query)
        pages = max(1, math.ceil(listing['total']/100))
        signature = hashlib.sha256(f'{root}:{relative}:{query}'.encode()).hexdigest()[:16]
        page = st.selectbox('صفحه فایل‌ها', list(range(1, pages+1)), key='hub-library-page-'+signature)
        if page > 1:
            listing = list_library(root, relative, query=query, offset=(page-1)*100)
    except (ValueError, OSError) as exc:
        st.warning(str(exc))
        return
    st.caption(f"{listing['total']} پوشه یا فایل گزارش در همین پوشه؛ حداکثر ۱۰۰ مورد در هر صفحه. "
               'فهرست‌گیری بازگشتی انجام نمی‌شود؛ برای رسیدن به گزارش، پوشه را باز کن.')
    if listing['skipped']:
        st.caption('برخی مسیرهای پنهان، پیوندی یا غیرقابل‌خواندن نمایش داده نشده‌اند.')
    entries = {row['relative']: row for row in listing['entries']}
    if not entries:
        st.info('پوشه یا گزارشی مطابق این جست‌وجو وجود ندارد.')
        return
    chosen = st.selectbox('پوشه یا گزارش', list(entries), index=None, placeholder='یک مورد انتخاب کن',
                          format_func=lambda key: ('📁 ' if entries[key]['directory'] else '📄 ') + entries[key]['label'],
                          key='hub-library-selected-'+signature)
    if chosen:
        selected = entries[chosen]
        if selected['directory']:
            st.button('بازکردن پوشه', on_click=_enter_library, args=(chosen,), key='hub-library-enter')
        else:
            st.caption(f"حجم فایل: {selected['size_bytes']:,} بایت")
            if st.button('خواندن این گزارش', key='hub-library-read'):
                st.session_state['hub-library-open-file'] = (root_name, chosen)
    opened = st.session_state.get('hub-library-open-file')
    if chosen and opened == (root_name, chosen):
        try:
            name, body = read_library_file(root, chosen)
            st.subheader(name)
            st.download_button('دریافت فایل گزارش', body, file_name=name, key='hub-report-download')
            preview, value = report_preview(name, body)
            if preview == 'json':
                st.json(value, expanded=False)
            elif preview == 'text':
                st.code(value, language=None, wrap_lines=True)
            else:
                st.info('برای حفظ مرز فایل و داشبورد، این قالب فقط برای دریافت آماده است. '
                        'HTML، PDF، تصویر و فایل جدولی در این صفحه اجرا یا تفسیر نمی‌شوند.')
        except (ValueError, OSError) as exc:
            st.warning(str(exc))


def page_monitoring(job_panel):
    st.title('🔔 مرکز پایش و هشدار')
    st.caption('وضعیت همه پایش‌ها و دسترسی به هشدارهای صف و حجم؛ رویداد توصیفی، سیگنال خریدوفروش نیست.')
    command_center_panel()
    st.subheader('کنترل پایش پس‌زمینه')
    new_run = st.checkbox('رادار با نسخه تازه ثبت شود؛ سابقه قبلی حفظ شود', value=False, key='hub-radar-new-run')
    args = ['radar', 'watch'] + (['--new-run'] if new_run else [])
    job_panel('opportunity-radar', args, 'شروع رادار',
              'پایش عمومی قیمت و خبر با ثبت نقص پوشش؛ اجرای فرایند، تازگی منبع را اثبات نمی‌کند.', stoppable=True)
    job_panel('live', ['live'], 'شروع پایش عمومی بازار',
              'در ساعات تقویم محلی دریافت می‌کند و خارج آن منتظر می‌ماند؛ وضعیت هر چرخه ثبت می‌شود.', stoppable=True)
    st.divider()
    board_alert_panel()
    with st.expander('پایش در مرورگر داخلی و نشست فعلی فارابی'):
        st.write('پنل نصب‌شده در تب فارابی از دیده‌بان مستقل جداست؛ وضعیت آن باید در همان تب بررسی شود. '
                 'از این صفحه رمز، کوکی یا نشست حساب به برنامه منتقل نمی‌شود.')
        st.link_button('بازکردن فارابی در تب خودت', 'https://farabixo.irfarabi.com/')
        st.caption('تنظیم نیم‌ثانیه‌ای و صدا در پنل سریع همان پایش است؛ بازکردن لینک، ورود یا دریافت معتبر را تأیید نمی‌کند.')
    st.button('دیدن رادار و رویدادهای پژوهشی', on_click=open_page, args=('radar',), key='hub-monitor-radar')
    st.button('دیدن آرشیو و رسیدهای پایش', on_click=open_page, args=('reports',), key='hub-monitor-reports')
