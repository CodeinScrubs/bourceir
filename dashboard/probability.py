"""آزمایش احتمال و سناریوی دستی؛ بدون نمایش احتمال جعلی برای سهم امروز."""

import json

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import settings
from core.probability import scenario_expectancy
from core.timeutils import to_jalali
from database.db import DatabaseManager
from research.probability_run import load_probability_report


def _pct(value):
    return 'نامعلوم' if value is None else f'\u2066{value*100:+.2f}%\u2069'


def _rate(value):
    return 'نامعلوم' if value is None else f'{value*100:.1f}٪'


@st.fragment
def _scenario_panel():
    st.subheader('احتمال چه زمانی به سود تبدیل می‌شود؟')
    st.caption('سناریوی فرضیِ رسیدن به هدف یا خروج در حد زیان؛ احتمال را خودت وارد می‌کنی. '
               'پرشدن سفارش، زمان خروج و محدودماندن زیان در این قیمت تضمین نمی‌شود.')
    with st.form('probability-payoff'):
        cols = st.columns(3)
        entry = cols[0].number_input('ورود فرضی (تومان)', min_value=0.1, value=None)
        stop = cols[1].number_input('حد زیان فرضی (تومان)', min_value=0.1, value=None)
        target = cols[2].number_input('هدف فرضی (تومان)', min_value=0.1, value=None)
        probability = st.number_input('احتمال فرضیِ رسیدن به هدف (درصد؛ اختیاری)', min_value=0., max_value=100., value=None)
        submitted = st.form_submit_button('محاسبه سناریو')
    state_key = 'probability_last_calculated_scenario'
    if submitted:
        st.session_state.pop(state_key, None)
        if any(v is None for v in (entry, stop, target)):
            st.info('هر سه قیمت سناریو را وارد کن؛ بدون قیمت عددی ساخته نمی‌شود.')
            return
        try:
            result = scenario_expectancy(entry_rial=entry*10, stop_rial=stop*10, target_rial=target*10,
                                        target_probability=None if probability is None else probability/100)
        except ValueError as exc:
            st.error(str(exc))
            return
        st.session_state[state_key] = dict(result=result, entry=entry, stop=stop, target=target)
    calculated = st.session_state.get(state_key)
    if calculated is None:
        return
    result = calculated['result']
    st.caption(f"آخرین محاسبه: ورود {calculated['entry']:g}، حد زیان {calculated['stop']:g}، هدف {calculated['target']:g} تومان. "
               'پس از تغییر ورودی‌ها دوباره محاسبه کن.')
    cols = st.columns(3)
    cols[0].metric('بازده خالص در هدف', _pct(result['target_net_return']))
    cols[1].metric('بازده خالص در حد زیان', _pct(result['stop_net_return']))
    cols[2].metric('احتمال هدف لازم برای سربه‌سر', _rate(result['break_even_target_probability']))
    if not result['target_profitable_after_costs']:
        st.warning('هدف انتخابی پس از هزینه‌ها سودده نیست؛ حتی رسیدن همیشگی به همین هدف، سود مثبت نمی‌سازد.')
    if result['assumed_target_probability'] is not None:
        st.write('میانگین فرضی بازده خالص: ' + _pct(result['expected_net_return']))
    else:
        st.info('احتمال وارد نشده است؛ میانگین سود فرضی نامعلوم می‌ماند.')
    st.caption(f"کارمزد تنظیمات: خرید {result['fees']['buy']*100:.4f}٪، فروش {result['fees']['sell']*100:.4f}٪؛ "
               f"لغزش هر سمت {result['slippage_bps']:g} واحد پایه. تعرفه تاریخی/شخصی با این محاسبه تأیید نمی‌شود.")
    points = []
    for p in range(0, 101, 5):
        mean = (p/100 * result['target_net_per_share_rial'] - (1-p/100)*result['stop_loss_per_share_rial'])
        points.append(dict(احتمال_فرضی=p, بازده_فرضی=mean / result['entry_cost_per_share_rial']*100))
    figure = go.Figure(go.Scatter(x=[p['احتمال_فرضی'] for p in points], y=[p['بازده_فرضی'] for p in points],
        mode='lines', name='سناریوی فرضی', hovertemplate='احتمال فرضی: %{x:.0f}٪<br>میانگین فرضی: %{y:+.2f}٪<extra></extra>'))
    figure.add_hline(y=0, line_color='#999999', line_width=1)
    if result['break_even_target_probability'] is not None:
        figure.add_vline(x=result['break_even_target_probability']*100, line_color='#999999', line_dash='dot')
    figure.update_layout(xaxis=dict(title='احتمال فرضی هدف (٪)', range=[0, 100]),
                         yaxis=dict(title='میانگین بازده فرضی پس از هزینه (٪)'),
                         margin=dict(l=30, r=20, t=20, b=30), height=300, showlegend=False)
    st.plotly_chart(figure, width='stretch', key='hypothetical-payoff-plot')
    st.caption('این نمودار فقط رابطه ریاضیِ فرض‌هاست؛ پیش‌بینی بازار یا نتیجه بکتست نیست.')


def page_probability(job_panel):
    st.title('🎲 آزمایش احتمال و بازده')
    st.warning('پژوهشی: هنوز احتمال سود واقعیِ قابل اتکا یا روش پذیرفته‌شده برای سیگنال نداریم.')
    _scenario_panel()
    st.divider()
    st.subheader('آیا برآوردِ گذشته در دوره بعد درست بوده است؟')
    st.write('برای هر یک از هشت قاعده ثابت، فراوانیِ سود خالص فقط از نتایج معلومِ پیش از مرز آموزش برآورد می‌شود. '
             'سپس با نتیجه دوره بعد و برآورد ساده کل جامعه نقدشونده مقایسه می‌شود. این احتمال در هر قاعده و برش یکسان است؛ '
             'پیش‌بینی اختصاصی یک سهم نیست.')
    root = settings.DATA_DIR / 'research'
    sources = sorted(p.stem for p in root.glob('EXP-*-BASELINES-*.json')) if root.exists() else []
    if sources:
        chosen = st.selectbox('نسل ورودی پژوهش پایه', sources, index=len(sources)-1)
        job_panel('research-probability', ['research-probability', '--source', chosen], 'اجرای آزمایش احتمال',
                  'پروتکل پیش از برازش ثبت می‌شود؛ تمام ۱۶ ترکیب روش/افق و دو سناریو گزارش می‌شوند. دوره نهایی باز نمی‌شود.')
    else:
        st.info('هنوز پژوهش پایه محفوظ وجود ندارد. این آزمایش به شناسه صریح یک پژوهش پایه معتبر نیاز دارد.')
    st.caption('افق‌ها با احتساب روز ورود ۲ و ۳ جلسه‌اند؛ توقف/صف می‌تواند خروج را عقب بیندازد. '
               'این مسیر نسل ورودی محفوظ را می‌سنجد و داده جاری دیتابیس را دوباره تأیید نمی‌کند.')
    pointer = root / 'latest-probability.json'
    if not pointer.exists():
        st.info('هنوز نتیجه آزمایش احتمال ثبت نشده است. ماشین‌حساب بالا مستقل قابل استفاده است.')
        return
    try:
        experiment_id = json.loads(pointer.read_text(encoding='utf-8'))['experiment_id']
        with DatabaseManager(read_only=True) as db:
            report = load_probability_report(db, experiment_id)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        st.error('گزارش احتمال با شواهد محفوظ تطبیق ندارد: ' + str(exc))
        return
    st.write('نتیجه محفوظ: ' + to_jalali(report['created_at'], with_time=True))
    st.code(report['experiment_id'], language=None)
    st.caption('منبع: ' + report['protocol']['source_id'] + ' | داده پیش از ' +
               to_jalali(report['protocol']['end_exclusive']) + '؛ این دوره توسعه قبلاً دیده شده و آزمون مستقل تازه نیست.')
    rows = []
    for item in report['summary']:
        normal, stress = item['normal'], item['stress']
        rows.append({'قاعده': item['label_fa'], 'جلسه با ورود': item['sessions_including_entry'],
                     'فرصت': normal['opportunities'], 'نتیجه نامعلوم': normal['unresolved'],
                     'بدون برآورد': normal['no_forecast'], 'فراوانی سود در نتایج معلوم': _rate(normal['empirical_success_frequency_resolved']),
                     'میانگین خالص نتایج معلوم': _pct(normal['mean_known_net_return']),
                     'میانگین خالص فشار معلوم': _pct(stress['mean_known_net_return']),
                     'بهبود خطای احتمال در جفت‌های معلوم': _pct(normal['brier_skill'])})
    st.dataframe(pd.DataFrame(rows), hide_index=True)
    st.caption('ترتیب ثابت است و برنده انتخاب نمی‌شود. میانگینِ نتایج معلوم، سود تمام فرصت‌ها یا بازده سرمایه مشترک نیست. '
               'بهبود مثبتِ خطای احتمال، به‌تنهایی کالیبراسیون، سودآوری یا معنی‌داری آماری را ثابت نمی‌کند.')
    choices = {f"{r['label_fa']} — {r['sessions_including_entry']} جلسه": r for r in report['summary']}
    selected = choices[st.selectbox('جزئیات قاعده و افق', list(choices))]
    mode = st.radio('سناریوی اجرا', ['عادی', 'خرید در سقف / فروش در کف'], horizontal=True)
    item = selected['normal' if mode == 'عادی' else 'stress']
    cols = st.columns(3)
    cols[0].metric('فرصت با برآورد / کل', f"{item['forecasts']:,} / {item['opportunities']:,}")
    cols[1].metric('ورود مسدود در مدل روزانه', f"{item['blocked_entries']:,}")
    cols[2].metric('خروج/نتیجه نامعلوم', f"{item['unresolved']:,}")
    bounds = item['empirical_success_fraction_bounds']
    if bounds:
        st.write('کسر سود در تمام فرصت‌ها با حفظ نتیجه‌های نامعلوم: ' + _rate(bounds['lower']) + ' تا ' + _rate(bounds['upper']))
        st.caption('این کران از نامعلوم‌بودن نتیجه‌هاست؛ فاصله اطمینان آماری یا کران سود آینده نیست.')
    st.write(f"ورودِ پرشده در مدل: {item['proxy_filled_entries']:,}؛ ورود نامعلوم: {item['unknown_entries']:,}؛ "
             f"معامله بسته‌شده: {item['completed_proxy_trades']:,}. اجرای واقعی از داده روزانه اثبات نمی‌شود.")
    calibration = [r for r in item['calibration'] if r['resolved']]
    if calibration:
        figure = go.Figure()
        figure.add_trace(go.Scatter(x=[0, 1], y=[0, 1], mode='lines', name='تطابق کامل', line=dict(dash='dash')))
        figure.add_trace(go.Scatter(x=[r['mean_probability'] for r in calibration],
            y=[r['observed_frequency'] for r in calibration], mode='markers', name='نتیجه‌های معلوم دوره بعد',
            text=[f"{r['resolved']:,} معلوم، {r['unknown']:,} نامعلوم" for r in calibration],
            marker=dict(size=12), hovertemplate='برآورد: %{x:.1%}<br>فراوانی معلوم: %{y:.1%}<br>%{text}<extra></extra>'))
        figure.update_layout(xaxis=dict(title='برآورد فراوانی از گذشته', tickformat='.0%', range=[0, 1]),
                             yaxis=dict(title='فراوانی سود در نتایج معلوم بعدی', tickformat='.0%', range=[0, 1]),
                             margin=dict(l=30, r=20, t=20, b=30), height=360, legend=dict(orientation='h'))
        st.plotly_chart(figure, width='stretch', key='probability-calibration-plot')
        st.dataframe(pd.DataFrame([{'بازه برآورد': f"{r['lower']:.0%}–{r['upper']:.0%}", 'برآورد': _rate(r['mean_probability']),
            'فراوانی سود معلوم': _rate(r['observed_frequency']), 'معلوم': r['resolved'], 'نامعلوم': r['unknown']}
            for r in item['calibration'] if r['forecasts']]), hide_index=True)
    else:
        st.info('پیش‌بینی با نتیجه معلوم کافی برای نمودار وجود ندارد.')
    st.caption('نمونه‌ها و دوره‌های نگهداری می‌توانند وابسته باشند. نتایج نامعلوم ممکن است تصادفی گم نشده باشند؛ '
               'این نمودار به‌تنهایی کالیبراسیون مستقل آینده را تأیید نمی‌کند. تعداد تلاش‌های پیشین نیز نامعلوم است.')
    st.download_button('دریافت گزارش کامل احتمال (JSON)', json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
                       file_name=report['experiment_id'] + '.json', mime='application/json', on_click='ignore')
