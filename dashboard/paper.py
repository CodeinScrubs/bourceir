"""رابط پورتفوی مجازی: سرمایه مستقل، تحلیل پیش از معامله و سود پس از هزینه."""

import uuid

import altair as alt
import pandas as pd
import streamlit as st

from core.paper import aware, canonical, quote_fill
from core.timeutils import now_tehran, to_jalali
from dashboard.workbench import HORIZONS, _account_metric_style, _toman
from database.broker import BrokerRepository
from database.db import DatabaseManager
from database.paper import PaperRepository

MODES_FA = {'شبیه‌سازی با مظنه تازه بازار': 'observed', 'سناریوی دستی با قیمت انتخابی من': 'manual'}
STATUS_FA = {'open': 'باز', 'closed': 'بسته', 'untraded': 'هنوز معامله نشده'}


def _create_panel(portfolios):
    with st.expander('ساخت پورتفوی مجازی مستقل', expanded=not portfolios):
        with st.form('paper-create', clear_on_submit=True):
            name = st.text_input('نام پورتفو یا روش', max_chars=120)
            capital = st.number_input('سرمایه آغازین مجازی (تومان)', min_value=1, value=None,
                                      placeholder='مبلغ دلخواه برای این آزمایش')
            mode = st.selectbox('شیوه ثبت معامله', list(MODES_FA))
            rationale = st.text_area('روش این پورتفو و معیار ارزیابی آن',
                                      placeholder='بر چه اساسی تصمیم می‌گیری؟ چه زمانی معامله نمی‌کنی؟')
            st.caption('سقف زیان این آزمایش ۱۰٪ سرمایه آغازین است و با سود بالا نمی‌رود. سرمایه واقعی یا مبنای فارابی تغییر نمی‌کند.')
            st.caption('سناریوی دستی، امکان اجرای آن قیمت را بررسی نمی‌کند. حالت مظنه‌ای، داده تازه پس از تحلیل و ظرفیت مشاهده‌شده می‌خواهد؛ اجرای واقعی را ثابت نمی‌کند.')
            if st.form_submit_button('افتتاح پورتفوی مجازی'):
                try:
                    with DatabaseManager() as db:
                        key = PaperRepository(db).create(name=name, capital_rial=None if capital is None else capital * 10,
                                                        rationale=rationale, mode=MODES_FA[mode])
                    st.session_state['paper_selected'] = key
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))


def _broker_copy_panel():
    with st.expander('شروع پورتفوی مجازی از سبد فعلی فارابی'):
        with DatabaseManager(read_only=True) as db:
            source = BrokerRepository(db).latest('portfolio')
        if source is None:
            st.info('ابتدا گزارش سبد را از صفحه «حساب و تطبیق فارابی» وارد کن.')
            return
        recent = 0 <= (now_tehran() - aware(source['captured_at'])).total_seconds() <= 900
        st.caption(f"آخرین برداشت: {to_jalali(source['captured_at'], with_time=True)} | {len(source['rows'])} دارایی")
        st.caption('کپی، تعداد و ارزش آغازین را منتقل می‌کند؛ خرید گذشته نمی‌سازد. نقد قابل استفاده از ارزش دارایی جداست. حق‌تقدم و ابزار خارج از دامنه فقط نگهداری و نمایش داده می‌شوند.')
        if not recent:
            st.warning('برای کپی سبد جاری، برداشت حداکثر ۱۵ دقیقه اخیر لازم است. به‌روزرسانی قیمت عمومی جای برداشت سبد را نمی‌گیرد.')
        token_key = 'paper-broker-copy-token'
        if token_key not in st.session_state:
            st.session_state[token_key] = uuid.uuid4().hex
        with st.form('paper-broker-copy'):
            name = st.text_input('نام کپی مجازی سبد', value='کپی مجازی سبد فارابی', max_chars=120)
            mode = st.selectbox('شیوه معامله پس از کپی', list(MODES_FA))
            if st.form_submit_button('ساخت کپی مجازی از همین گزارش فارابی', disabled=not recent):
                try:
                    with DatabaseManager() as db:
                        key = PaperRepository(db).create_from_broker(source['id'], name=name, mode=MODES_FA[mode],
                                                                   request_id=st.session_state[token_key])
                    st.session_state[token_key] = uuid.uuid4().hex
                    st.session_state['paper_selected'] = key
                    st.session_state['paper_flash'] = 'سبد با موجودی آغازین مستقل کپی شد؛ خرید یا معامله واقعی ساخته نشد.'
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))


def _idea_panel(portfolio_id, universe):
    with st.expander('۱. ثبت تحلیل جدید پیش از معامله', expanded=True):
        if not universe:
            st.info('ابتدا جامعه نمادهای واقعی را از صفحه خانه دریافت کن.')
            return
        with st.form(f'paper-idea-{portfolio_id}', clear_on_submit=True):
            symbol = st.selectbox('نماد تحلیل مجازی', sorted(r['symbol_fa'] for r in universe))
            label = st.text_input('عنوان تحلیل', max_chars=200)
            thesis = st.text_area('دلیل انتخاب و شواهد تحلیل', placeholder='فرضیه، منبع و نسخه روش را بنویس.')
            invalidation = st.text_area('چه اتفاقی تحلیل را رد می‌کند و چه زمانی خارج می‌شوی؟')
            horizon = st.selectbox('افق تحلیل مجازی', list(HORIZONS), index=1)
            c1, c2 = st.columns(2)
            stop = c1.number_input('قیمت سناریوی زیان (تومان؛ اختیاری)', min_value=.1, value=None, step=.1)
            target = c2.number_input('قیمت هدف (تومان؛ اختیاری)', min_value=.1, value=None, step=.1)
            st.caption('تحلیل با زمان اکنون ثبت و حفظ می‌شود؛ ویرایش یا حذف ندارد. هدف و حد زیان سفارش خودکار یا تضمین خروج نیستند.')
            if st.form_submit_button('ثبت تحلیل مجازی'):
                try:
                    with DatabaseManager() as db:
                        PaperRepository(db).register_idea(portfolio_id, symbol=symbol, label=label, thesis=thesis,
                            invalidation=invalidation, horizon=HORIZONS[horizon],
                            stop_rial=None if stop is None else round(stop * 10),
                            target_rial=None if target is None else round(target * 10))
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))


def _trade_panel(portfolio_id, report):
    ideas = report['ideas']
    st.subheader('۲. خرید یا فروش مجازی طبق تحلیل ثبت‌شده')
    if not ideas:
        st.info('ابتدا تحلیل را ثبت کن؛ تحلیل‌های بدون معامله هم در کارنامه باقی می‌مانند.')
        return
    mode = report['protocol']['mode']
    if mode == 'manual':
        st.warning('سناریوی دستی: خرید و فروش با قیمت انتخابی تو محاسبه می‌شود؛ صف، توقف و امکان پر شدن آن قیمت تأیید نمی‌شود.')
    else:
        st.info('پایش زنده باید روشن باشد. مظنه باید پس از ثبت تحلیل دریافت شود؛ قیمت با لغزش و کارمزد ثابت این پورتفو محاسبه می‌شود.')
    key = st.selectbox('تحلیل یا موجودی مبنای معامله', list(ideas),
                       format_func=lambda k: f"{ideas[k]['symbol_fa']} | {ideas[k]['label']} | {k[-6:]}",
                       key=f'paper-trade-idea-{portfolio_id}')
    idea = ideas[key]
    with st.expander('متن تحلیل انتخاب‌شده'):
        st.write(idea['thesis'])
        st.write('شرط رد: ' + idea['invalidation'])
        st.caption('زمان ثبت: ' + to_jalali(idea['created_at'], with_time=True))
    observation = report['observations'][key]
    if observation['mark_rial'] is not None:
        st.caption(f"قیمت آخرین ارزش‌گذاری: {_toman(observation['mark_rial'])} برای هر سهم؛ تاریخ {to_jalali(observation['mark_date'])}")
    snapshot = observation['snapshot']
    display_key = f'paper-displayed-quote-{portfolio_id}-{key}'
    displayed_snapshot_id = st.session_state.get(display_key)
    # در ارسال فرم، مظنه‌ای که در اجرای قبلی به کاربر نشان داده شد ملاک است.
    st.session_state[display_key] = snapshot['id'] if snapshot else None
    if mode == 'observed':
        if st.button('دریافت آخرین مظنه ذخیره‌شده', key=f'paper-refresh-{portfolio_id}'):
            st.rerun()
        if snapshot:
            c1, c2 = st.columns(2)
            c1.metric('بهترین عرضه مشاهده‌شده برای خرید', _toman(snapshot.get('ask_price_1')))
            c2.metric('بهترین تقاضای مشاهده‌شده برای فروش', _toman(snapshot.get('bid_price_1')))
            st.caption('زمان گزارش‌شده: ' + to_jalali(snapshot['provider_timestamp'], with_time=True)
                       + ' | زمان دریافت: ' + to_jalali(snapshot['captured_at'], with_time=True))
            preview = []
            directions = (('sell', 'فروش'),) if idea.get('origin') == 'broker_opening' else (('buy', 'خرید'), ('sell', 'فروش'))
            for direction, label in directions:
                try:
                    estimate = quote_fill(snapshot, side=direction, quantity=1, at=now_tehran(),
                        decision_at=idea['created_at'], protocol=report['protocol'], calendar=report['calendar_exceptions'])
                    preview.append(label + ': ' + _toman(estimate['price_rial']) + ' برای هر سهم، پیش از کارمزد')
                except ValueError as exc:
                    preview.append(label + ': ' + str(exc))
            st.caption(' | '.join(preview))
        else:
            st.warning('مظنه‌ای برای این نماد دریافت نشده؛ پایش زنده را روشن کن و سپس صفحه را تازه کن.')
    token_key = f'paper-request-{portfolio_id}'
    if token_key not in st.session_state:
        st.session_state[token_key] = uuid.uuid4().hex
    with st.form(f"paper-trade-{portfolio_id}-{st.session_state[token_key]}"):
        side = st.radio('سمت معامله مجازی', ['فروش مجازی'] if idea.get('origin') == 'broker_opening'
                        else ['خرید مجازی', 'فروش مجازی'], horizontal=True)
        quantity = st.number_input('تعداد سهم مجازی', min_value=1, value=1, step=1)
        price = st.number_input('قیمت سناریوی دستی (تومان برای هر سهم)', min_value=.1, value=None,
                                step=.1, disabled=mode != 'manual')
        reason = st.text_area('دلیل انجام این خرید یا فروش مجازی')
        st.caption('ثبت با زمان اکنون است. نقد، موجودی و کارمزد کنترل می‌شوند؛ فروش جزئی مجاز است. هیچ سفارشی به کارگزاری فرستاده نمی‌شود.')
        if st.form_submit_button('ثبت معامله در پورتفوی مجازی', disabled=mode == 'observed' and snapshot is None):
            try:
                with DatabaseManager() as db:
                    PaperRepository(db).trade(portfolio_id, idea_id=key,
                        side='buy' if side == 'خرید مجازی' else 'sell', quantity=quantity, rationale=reason,
                        price_rial=None if price is None or mode != 'manual' else round(price * 10),
                        request_id=st.session_state[token_key],
                        expected_snapshot_id=displayed_snapshot_id if mode == 'observed' else None)
                st.session_state[token_key] = uuid.uuid4().hex
                st.session_state['paper_flash'] = 'معامله مجازی ثبت شد؛ نقد و موجودی پورتفو به‌روز شدند.'
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))


def _scorecard(portfolio_id, report):
    protocol, ledger = report['protocol'], report['ledger']
    st.info('این کارنامه نتیجه سناریوی قیمت دلخواه است؛ اجرای آن قیمت تأیید نشده.' if protocol['mode'] == 'manual'
            else 'این کارنامه شبیه‌سازی از مظنه است؛ پر شدن سفارش واقعی و امکان خروج کامل تأیید نشده.')
    issues = sorted({p for problems in report['integrity'].values() for p in problems})
    opening = report['opening']
    if opening:
        st.caption(f"شروع از گزارش فارابی: {to_jalali(opening['source_captured_at'], with_time=True)} | "
                   f"{len(opening['positions']) + len(opening['passive'])} دارایی آغازین؛ بدون اجرای خرید")
        st.caption('مبنای سود این آزمایش ارزش آغازین دارایی است، نه قیمت خرید تاریخی. بازده بی‌نهایت یا بهای خرید صفر کارگزاری، سود معتبر فرض نشده است.')
        st.caption(f"نقد غیرقابل استفاده آغازین: {_toman(opening['locked_cash_rial'])} | اختلاف گرد کردن غیرنقدی گزارش: {opening['rounding_rial']:,} ریال")
    c1, c2 = st.columns(2)
    c1.metric('نقد مجازی دفتر', _toman(ledger['cash_rial']))
    c2.metric('ارزش خالص برآوردی پورتفوی مجازی', _toman(report['equity_rial']))
    c3, c4 = st.columns(2)
    c3.metric('سود/زیان خالص از آغاز', _toman(report['total_net_rial']))
    c4.metric('بازده از آغاز', 'نامعلوم' if report['return_pct'] is None else f"{report['return_pct']:+.2f}٪")
    st.caption(f"سرمایه آغازین: {_toman(protocol['capital_rial'])} | سقف زیان ثابت: {_toman(protocol['max_loss_rial'])} | افتتاح: {to_jalali(protocol['created_at'])}")
    st.caption(f"کارمزد برآوردی ثابت: خرید {protocol['fees']['buy'] * 100:.4f}٪، فروش {protocol['fees']['sell'] * 100:.4f}٪. نقد سود فرضی نمی‌گیرد. ارزش خالص هزینه فروش موجودی باز را هم کم می‌کند.")
    c1.metric('سود/زیان تحقق‌یافته مجازی', _toman(None if issues or not report['runtime_matches'] else ledger['realized_net_rial']))
    c2.metric('کارمزد پرداخت‌شده مجازی', _toman(ledger['fees_rial']))
    if report['loss_limit_reached']:
        st.error('ارزش برآوردی به کف زیان رسیده؛ خرید تازه متوقف است. فروش برای ثبت نتیجه همچنان مجاز است؛ سقف زیان، تضمین حداکثر ضرر نیست.')
    if issues:
        st.warning('سود اقتصادی نامعلوم: ' + '؛ '.join(issues) + '. هیچ سود نقدی، حق‌تقدم یا تعداد سهم فرضی به دفتر اضافه نشده است.')
    if report['passive_unknown']:
        st.warning('ارزش دارایی‌های فقط‌نمایشی نامعلوم است: ' + '، '.join(report['passive_unknown']))
    if not report['runtime_matches']:
        st.warning('کد محاسبه از زمان افتتاح تغییر کرده؛ این کارنامه نیازمند بازبینی است و معامله تازه در آن متوقف است.')
    holdings = []
    for key, position in ledger['positions'].items():
        idea, observation = report['ideas'][key], report['observations'][key]
        holdings.append({'نماد': idea['symbol_fa'], 'تحلیل': idea['label'], 'تعداد': position['quantity'],
                         'بهای تمام‌شده (تومان)': position['cost_rial'] / 10,
                         'قیمت نشان‌گذاری (تومان)': None if observation['mark_rial'] is None else observation['mark_rial'] / 10,
                         'تاریخ قیمت': to_jalali(observation['mark_date']),
                         'سود/زیان باز خالص (تومان)': None if observation['issues'] or not report['runtime_matches']
                         else None if position.get('unrealized_net_rial') is None else position['unrealized_net_rial'] / 10})
    if holdings:
        st.dataframe(pd.DataFrame(holdings), hide_index=True, width='stretch')
    else:
        st.info('در این پورتفو سهم بازی وجود ندارد.')
    if opening and opening['passive']:
        st.subheader('دارایی‌های آغازین فقط برای نگهداری و نمایش')
        passive_rows = []
        for holding in opening['passive']:
            observation = report['passive_observations'][holding['symbol_fa']]
            passive_rows.append({'نماد': holding['symbol_fa'], 'تعداد': holding['quantity'],
                'ارزش مبنا (تومان)': holding['cost_rial'] / 10, 'نوع ابزار': holding['instrument_type_code'],
                'ارزش گزارش بعدی (تومان)': None if observation['value_rial'] is None else observation['value_rial'] / 10,
                'برداشت گزارش': to_jalali(observation['captured_at'], with_time=True), 'معامله مجازی': 'غیرفعال'})
        st.dataframe(pd.DataFrame(passive_rows), hide_index=True, width='stretch')
        st.caption('ارزش این دارایی‌ها فقط از گزارش فارابی با تعداد ثابت می‌آید؛ زمان جلسه قیمت و امکان فروش تأیید نشده‌اند. گزارش کهنه یا تغییر تعداد، ارزش کل را نامعلوم می‌کند. تعداد و وجوه آن‌ها خودکار تغییر نمی‌کنند.')
    st.caption('قیمت نشان‌گذاری، قیمت فروش تضمینی نیست. روزهای گمشده، کهنگی و شکست مرجع، کارنامه را نامعلوم می‌کنند. بهای هر تحلیل جدا و نقد همه تحلیل‌ها مشترک است.')
    if st.button('ثبت تصویر اکنون پورتفو برای پیگیری بعدی', key=f'paper-value-{portfolio_id}'):
        try:
            with DatabaseManager() as db:
                PaperRepository(db).save_valuation(portfolio_id)
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
    points = [{'زمان': e['recorded_at'], 'ارزش خالص (تومان)': None if e['payload']['equity_rial'] is None
               else e['payload']['equity_rial'] / 10} for e in report['events'] if e['kind'] == 'valuation']
    if points:
        chart = pd.DataFrame(points)
        chart['زمان'] = pd.to_datetime(chart['زمان'])
        known = chart.dropna(subset=['ارزش خالص (تومان)'])
        if not known.empty:
            figure = alt.Chart(known).mark_point(size=70).encode(x='زمان:T',
                y=alt.Y('ارزش خالص (تومان):Q', scale=alt.Scale(zero=False)),
                tooltip=['زمان:T', 'ارزش خالص (تومان):Q']).properties(height=220)
            st.altair_chart(figure, width='stretch')
        st.caption(f'{len(points)} تصویر ثبت شده؛ {len(chart) - len(known)} ارزش نامعلوم. این نقاط منحنی روزانه کامل، حداکثر افت واقعی یا بازده سالانه را اثبات نمی‌کنند.')


def _history(report):
    ideas = report['ideas']
    if report['attribution']:
        rows = []
        for item in report['attribution']:
            known = not report['integrity'].get(item['id']) and report['runtime_matches']
            rows.append({'نماد': item['symbol_fa'], 'تحلیل': item['label'], 'نوع ثبت': 'موجودی آغازین' if item['imported_inventory'] else 'تحلیل تازه',
                         'وضعیت': STATUS_FA[item['status']],
                         'تعداد باز': item['quantity'], 'ثبت تحلیل': to_jalali(item['created_at'], with_time=True),
                         'سود/زیان بسته‌شده (تومان)': item['realized_net_rial'] / 10 if known else None})
        st.dataframe(pd.DataFrame(rows), hide_index=True, width='stretch')
        st.caption('تحلیل‌های بدون معامله و معاملات زیان‌ده هم در این فهرست می‌مانند؛ بستن معامله سابقه آن را حذف نمی‌کند.')
    trades = []
    for event in report['events']:
        if event['kind'] != 'trade':
            continue
        trade, idea = event['payload'], ideas[event['idea_id']]
        trades.append({'زمان ثبت': to_jalali(event['recorded_at'], with_time=True), 'نماد': idea['symbol_fa'],
                       'تحلیل': idea['label'], 'سمت': 'خرید' if trade['side'] == 'buy' else 'فروش',
                       'تعداد': trade['quantity'], 'قیمت (تومان)': trade['price_rial'] / 10,
                       'کارمزد (تومان)': trade['fee_rial'] / 10, 'دلیل': trade['rationale']})
    if trades:
        st.dataframe(pd.DataFrame(trades), hide_index=True, width='stretch')
    for key, idea in ideas.items():
        with st.expander(f"{idea['symbol_fa']} — {idea['label']} ({key[-6:]})"):
            st.write(idea['thesis'])
            st.write('شرط رد و خروج: ' + idea['invalidation'])
            st.caption('ثبت: ' + to_jalali(idea['created_at'], with_time=True))


def page_paper():
    _account_metric_style()
    st.title('💼 پورتفوی مجازی')
    st.caption('با پول مجازی طبق تحلیل خودت معامله کن و نتیجه را بعداً با هزینه‌ها بسنج. این صفحه پیشنهاد خرید و فروش صادر نمی‌کند.')
    if 'paper_flash' in st.session_state:
        st.success(st.session_state.pop('paper_flash'))
    with DatabaseManager(read_only=True) as db:
        portfolios = PaperRepository(db).portfolios()
        universe = db.get_universe_symbols()
    _create_panel(portfolios)
    _broker_copy_panel()
    if not portfolios:
        st.info('برای شروع، پورتفو را با سرمایه مجازی دلخواه خودت افتتاح کن. هیچ موجودی واقعی به آن منتقل نمی‌شود.')
        return
    by_id = {p['id']: p for p in portfolios}
    choices = list(by_id)
    preferred = st.session_state.pop('paper_selected', None)
    if preferred in choices:
        st.session_state['paper-picker'] = preferred
    selected = st.selectbox('پورتفوی فعال', choices,
                            format_func=lambda k: f"{by_id[k]['name']} | {k[-6:]}", key='paper-picker')
    try:
        with DatabaseManager(read_only=True) as db:
            report = PaperRepository(db).report(selected)
        st.write('روش ثبت‌شده: ' + report['protocol']['rationale'])
        st.caption('نوع: ' + next(label for label, mode in MODES_FA.items() if mode == report['protocol']['mode']))
        trade_tab, holdings_tab, history_tab, compare_tab = st.tabs(['ثبت تحلیل و معامله', 'سبد و کارنامه', 'تحلیل‌ها و سابقه', 'مقایسه پورتفوها'])
        with trade_tab:
            _idea_panel(selected, universe)
            _trade_panel(selected, report)
        with holdings_tab:
            _scorecard(selected, report)
        with history_tab:
            _history(report)
            with DatabaseManager(read_only=True) as db:
                exported = PaperRepository(db).export(selected)
            st.download_button('دریافت دفتر خصوصی این پورتفو (JSON)', canonical(exported),
                               file_name=selected + '.json', mime='application/json')
        with compare_tab:
            comparison = []
            with DatabaseManager(read_only=True) as db:
                repo = PaperRepository(db)
                for p in portfolios:
                    try:
                        result = report if p['id'] == selected else repo.report(p['id'])
                        comparison.append({'پورتفو': p['name'], 'شروع': to_jalali(p['created_at']),
                            'نوع': 'دستی' if p['mode'] == 'manual' else 'مظنه تازه',
                            'سرمایه آغازین (تومان)': p['capital_rial'] / 10,
                            'بازده از آغاز (٪)': result['return_pct'],
                            'تعداد تحلیل': sum(p.get('origin') != 'broker_opening' for p in result['ideas'].values()),
                            'دارایی آغازین': len(result['opening']['source']['rows']) if result['opening'] else 0,
                            'تعداد معامله': sum(e['kind'] == 'trade' for e in result['events'])})
                    except ValueError:
                        comparison.append({'پورتفو': p['name'], 'وضعیت': 'دفتر نیازمند بازبینی است'})
            st.dataframe(pd.DataFrame(comparison), hide_index=True, width='stretch')
            st.caption('همه پورتفوها دیده می‌شوند. شروع و نوع اجرا ممکن است متفاوت باشند؛ این جدول رتبه‌بندی منصفانه با ریسک مشابه نیست. بازده کوتاه به سالانه تبدیل نمی‌شود و سودآوری قابل اتکا هنوز اثبات نشده است.')
    except ValueError as exc:
        st.error(str(exc))
