"""One route catalog for the local dashboard; no market or account access."""

from dataclasses import dataclass

import streamlit as st

from core.text import normalize_symbol


@dataclass(frozen=True)
class Page:
    slug: str
    label: str
    group: str
    description: str
    keywords: str = ''


PAGES = (
    Page('home', '🏠 خانه', 'مرکز فرمان', 'وضعیت روز، دسترسی سریع و مسیر همه قابلیت‌ها', 'dashboard command center'),
    Page('market', '🌐 بررسی بازار امروز', 'بازار و فرصت‌ها', 'گزارش مستند جلسه، پوشش و صنایع', 'شاخص بازار صنعت'),
    Page('radar', '📡 رادار فرصت‌های پژوهشی', 'بازار و فرصت‌ها', 'پیگیری سراسری مشاهدات و اطلاعیه‌های تازه', 'شکار جهش رشد surge'),
    Page('shortlist', '🔎 فهرست بررسی', 'بازار و فرصت‌ها', 'نامزدهای توصیفی و علت نیاز به بررسی', 'انتخاب سهم screening'),
    Page('filters', '🧪 آزمایشگاه فیلترها', 'بازار و فرصت‌ها', 'فیلترهای حجم، قدرت خریدار و جریان حقیقی', 'فیلتر سرانه پول هوشمند'),
    Page('journal', '🛡️ برنامه و دفتر معاملات', 'سبد و تصمیم‌ها', 'برنامه دستی، اندازه ریسک و دفتر اجراها', 'ریسک stop loss risk'),
    Page('paper', '💼 پورتفوی مجازی', 'سبد و تصمیم‌ها', 'معامله آزمایشی، موجودی و کارنامه مستقل', 'پورتفو سبد paper portfolio'),
    Page('forward', '🗓️ دفتر مشاهده آینده', 'سبد و تصمیم‌ها', 'ثبت تصمیم پیش از نتیجه و پیگیری آینده', 'forward کاغذی'),
    Page('broker', '🏦 حساب و تطبیق فارابی', 'سبد و تصمیم‌ها', 'ورود گزارش حساب، تطبیق موجودی و هزینه', 'کارگزاری farabi دارایی'),
    Page('symbol', '📊 بررسی نماد', 'تحلیل و پژوهش', 'نمودار، داده خام و تحلیل توصیفی هر سهم', 'چارت تکنیکال fundamental بنیادی'),
    Page('live', '📡 دیده‌بان زنده', 'پایش و عملیات', 'مظنه‌های ثبت‌شده با کنترل زمان و پوشش', 'تابلو قیمت real time'),
    Page('alerts', '🔔 مرکز پایش و هشدار', 'پایش و عملیات', 'صف، حجم، فرایندها و دسترسی به پنل سریع', 'صف خرید صف فروش شکست صف ددام خیمن هشدار alert'),
    Page('codal', '📰 اطلاعیه‌های کدال', 'تحلیل و پژوهش', 'خبر، اسناد ناشر و خلاصه‌های قابل جست‌وجو', 'کدال صورت مالی بنیادی codal'),
    Page('research', '🔬 پژوهش', 'تحلیل و پژوهش', 'بکتست روش‌ها، نقد مشترک و فشار اجرا', 'backtest مدل dip'),
    Page('capital', '💸 هزینه و ظرفیت سرمایه', 'تحلیل و پژوهش', 'هزینه، ظرفیت سفارش و اثر اندازه سرمایه', 'کارمزد نقدشوندگی'),
    Page('probability', '🎲 آزمایش احتمال و بازده', 'تحلیل و پژوهش', 'سناریوی احتمال، هزینه و ارزیابی زمانی', 'احتمالات expectancy probability'),
    Page('growth', '📚 مطالعه رشد نمادها', 'تحلیل و پژوهش', 'مطالعه رشد، هم‌گروه‌ها و شواهد پیشین', 'چند برابر pattern رشد'),
    Page('query', '🔎 پرسش‌گر بازار', 'بازار و فرصت‌ها', 'ارزش شرکت، صدک و ترتیب نوسان درون‌روزی', 'ارزش بازار منفی مثبت market cap'),
    Page('reports', '🗂️ گزارش‌ها و آرشیو', 'ابزار و شواهد', 'دسترسی محلی به گزارش‌ها، رسیدها و راهنماها', 'گزارش html json حق تقدم افزایش سرمایه آرشیو'),
    Page('tools', '🧰 همه ابزارها', 'ابزار و شواهد', 'فهرست کامل فرمان‌ها، ابزارهای پیشرفته و مستندات', 'ابزار command cli افزایش سرمایه حق تقدم حقوق شرکتی'),
    Page('health', '🩺 سلامت سامانه', 'پایش و عملیات', 'آمادگی، کیفیت داده، منشأ و نصب', 'doctor readiness نقص داده'),
    Page('guide', 'ℹ️ راهنما', 'ابزار و شواهد', 'شروع کار و حدود اعتماد به نتایج', 'help آموزش'),
)
BY_SLUG = {page.slug: page for page in PAGES}
BY_LABEL = {page.label: page for page in PAGES}
MENU_KEY = 'brs-navigation-page'
QUERY_KEY = 'brs-navigation-query'


def search_pages(query):
    words = normalize_symbol(query or '').casefold().split()
    return [page for page in PAGES if all(word in normalize_symbol(
        f'{page.label} {page.group} {page.description} {page.keywords} {page.slug}').casefold() for word in words)]


def open_page(slug):
    """Callback: only known local routes may change the selected dashboard page."""
    page = BY_SLUG[slug]
    st.session_state[MENU_KEY] = page.label
    st.session_state[QUERY_KEY] = slug
    st.query_params['page'] = slug


def _menu_changed():
    open_page(BY_LABEL[st.session_state[MENU_KEY]].slug)


def sidebar_navigation(handlers):
    if set(handlers) != set(BY_LABEL):
        raise ValueError('Dashboard routes and page handlers must agree')
    values = st.query_params.get_all('page')
    requested = values[0] if len(values) == 1 else 'home'
    invalid = bool(values) and (len(values) != 1 or requested not in BY_SLUG)
    requested = requested if requested in BY_SLUG else 'home'
    if MENU_KEY not in st.session_state or st.session_state.get(QUERY_KEY) != requested:
        st.session_state[MENU_KEY] = BY_SLUG[requested].label
        st.session_state[QUERY_KEY] = requested
    if st.session_state[MENU_KEY] not in handlers:
        st.session_state[MENU_KEY] = BY_SLUG['home'].label
    query = st.text_input('پیدا کردن بخش', placeholder='مثلاً سبد، صف، کدال یا حق تقدم', key='brs-navigation-search')
    if query.strip():
        found = search_pages(query)
        if not found:
            st.caption('بخشی پیدا نشد؛ فهرست «همه ابزارها» را هم جست‌وجو کن.')
        for page in found[:5]:
            st.button(page.label, key='nav-search-'+page.slug, on_click=open_page, args=(page.slug,), width='stretch')
    choice = st.radio('منو', list(handlers), key=MENU_KEY, on_change=_menu_changed, label_visibility='collapsed')
    if invalid:
        st.warning('نشانی این بخش شناخته‌شده نیست؛ صفحه خانه انتخاب شد.')
    return choice


def launchpad():
    """All pages remain one click away; opening a page never starts a job."""
    st.markdown('<div class="brs-hub-hero"><b>مرکز فرمان بورس</b><p>بازار، سبد، هشدارها، پژوهش و شواهد؛ از یک نقطه شروع کن.</p></div>',
                unsafe_allow_html=True)
    mode = st.radio('دسترسی به قابلیت‌ها', ['دسترسی سریع', 'همه بخش‌ها'], horizontal=True,
                    key='brs-launchpad-mode', label_visibility='collapsed')
    quick = ('market', 'paper', 'alerts', 'symbol', 'radar', 'journal', 'reports', 'tools')
    pages = [BY_SLUG[slug] for slug in quick] if mode == 'دسترسی سریع' else [p for p in PAGES if p.slug != 'home']
    for offset in range(0, len(pages), 4):
        for column, page in zip(st.columns(4), pages[offset:offset+4]):
            with column.container(border=True):
                st.caption(page.group)
                st.button(page.label, key='hub-open-'+page.slug, on_click=open_page, args=(page.slug,), width='stretch')
                st.caption(page.description)
    st.caption('هر بخش نشانی مستقیم دارد؛ آدرس مرورگر را برای دسترسی بعدی ذخیره کن. '
               'ابزارهای پیشرفته در «همه ابزارها» و خروجی‌ها در «گزارش‌ها و آرشیو» هستند.')
