"""پرسش‌های محدود و قابل بررسی؛ هیچ اجرای کد دلخواه یا توصیه معامله ندارد."""

import json
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import settings
from core.timeutils import jalali_to_iso, now_tehran, to_jalali
from ops import jobs
from ops.query_report import MEMBERSHIP, REASONS
from ops.query_screener import read_query


def page_query_screener():
    st.title('🔎 پرسش‌گر بازار')
    st.write('ارزش شرکت = تعداد کل سهام × آخرین قیمت معامله. برش صدکی از کوچک‌ترین شرکت‌ها انتخاب می‌شود.')
    st.info('این صفحه پاسخ توصیفی مستند می‌سازد. شمار رخداد، سیگنال خریدوفروش یا تضمین امکان معامله نیست.')
    today = now_tehran().date()
    with st.expander('قرارداد پرسش تازه',expanded=True):
        left,right = st.columns(2)
        start_text=left.text_input('شروع بازه (شمسی)',value=to_jalali(today-timedelta(days=365),fa_digits=False))
        end_text=right.text_input('پایان بازه (شمسی)',value=to_jalali(today-timedelta(days=1),fa_digits=False))
        try:
            start=date.fromisoformat(jalali_to_iso(start_text).split('T')[0])
            end=date.fromisoformat(jalali_to_iso(end_text).split('T')[0])
            valid_dates=start<=end<today
        except (ValueError,TypeError,OverflowError):
            start=end=None
            valid_dates=False
        if not valid_dates:
            st.error('دو تاریخ شمسی معتبر مانند ۱۴۰۵/۰۷/۲۲ وارد کنید؛ پایان نباید پیش از شروع یا در امروز/آینده باشد.')
        limits = st.slider('صدک ارزش شرکت از کوچک‌ترین‌ها',0,100,(10,40))
        c1,c2=st.columns(2)
        dip=c1.number_input('آستانه منفی نسبت به مرجع روز، درصد',min_value=-99.,max_value=-.1,value=-2.,step=.1)
        rally=c2.number_input('آستانه مثبت بعد از آن، درصد',min_value=.1,max_value=1000.,value=2.,step=.1)
        st.caption('هر روز حداکثر یک بار. آخرین معامله با قیمت پایانی رسمی تفاوت دارد. روز بدون داده صفر شمرده نمی‌شود.')
        ticks=st.checkbox('تطبیق با آرشیو ریزمعاملات عمومی (ممکن است طولانی شود)',value=False)
        budget=st.number_input('سقف دریافت روز-نماد در این اجرا',0,100000,100,step=25,disabled=not ticks)
        args=(['query','--start',start.isoformat(),'--end',end.isoformat(),'--cap-low',str(limits[0]),'--cap-high',str(limits[1]),
              f'--dip={dip}',f'--rally={rally}','--refresh-quotes'] if valid_dates else [])
        if ticks:
            args+=['--verify-trades','--max-trade-days',str(budget)]
        status=jobs.job_status('market-query')
        if st.button('ثبت پرسش و ساخت گزارش',type='primary',disabled=status['running'] or not valid_dates or limits[0]==limits[1]):
            jobs.start_job('market-query',args)
            st.rerun()
        if status['running']:
            st.info('پرسش در پس‌زمینه در حال بررسی است؛ بستن صفحه آن را متوقف نمی‌کند.')
        if status['log_tail']:
            st.code('\n'.join(status['log_tail']),language=None)
    root=settings.DATA_DIR/'private'/'market-queries'
    folders=sorted([p for p in root.glob('QUERY-*') if (p/'current.json').is_file()],key=lambda p:p.stat().st_mtime_ns,reverse=True)
    if not folders:
        st.caption('هنوز پرسش کامل و محفوظی وجود ندارد.')
        return
    folder=st.selectbox('پرسش‌های محفوظ',folders,format_func=lambda p:p.name)
    try:
        result,revision=read_query(folder)
    except (ValueError,OSError,KeyError,json.JSONDecodeError) as error:
        st.error(f'گزارش قابل تأیید نیست: {error}')
        return
    caps,spec=result['caps'],result['protocol']['spec']
    st.caption(f"{to_jalali(spec['start'])} تا {to_jalali(spec['end'])}؛ ثبت {to_jalali(result['protocol']['registered_at'],with_time=True)}")
    a,b,c=st.columns(3)
    a.metric('کل سهام عادی فعال',caps['universe_total'])
    b.metric('ارزش شرکت نامعلوم',caps['caps_unknown'])
    c.metric('روز دارای دو برخورد',result['candidate_days'])
    st.caption(f"زمان این نتیجه: {to_jalali(result['evaluated_at'],with_time=True)}؛ "
               f"ارجاع ریزمعاملات: {len(result['trade_references'])} روز-نماد. داشتن ارجاع به معنی تطبیق موفق نیست.")
    st.warning('رتبه شامل کران اثر شرکت‌های نامعلوم و هم‌ارزش‌ها است. شمار تأییدشده، کران پایین روزهای موجود است؛ پوشش کامل سال و رتبه قطعی سالانه تأیید نشده‌اند.')
    rows=[dict(نماد=r['symbol_fa'],عضویت=MEMBERSHIP[r['slice_membership']],تأییدشده=r['confirmed_count'],
        سقف_در_داده_موجود=r['possible_count'],مبهم=r['ambiguous_count'],روز_موجود=r['observed_sessions'],
        بدون_کندل_در_روزهای_بازار=r['missing_market_dates']) for r in result['results']]
    st.dataframe(pd.DataFrame(rows),hide_index=True,width='stretch')
    for name,label in [('report.html','گزارش کامل همراه نمودار'),('companies.csv','همه شرکت‌ها و رتبه‌ها'),('days.csv','همه روزها و شواهد')]:
        st.download_button(label,(revision/name).read_bytes(),file_name=name,mime='text/html' if name.endswith('.html') else 'text/csv',key='query-'+name)
    metas={str(r['symbol_id']):r for r in result['results']}
    if metas:
        key=st.selectbox('بررسی کندل‌های نماد',list(metas),format_func=lambda k:metas[k]['symbol_fa'])
        days=pd.DataFrame(result['details'].get(key,[]))
        if not days.empty:
            ref=pd.to_numeric(days['ref_price_raw'],errors='coerce').where(lambda x:x>0)
            candles={k:100*(pd.to_numeric(days[k],errors='coerce')/ref-1) for k in ['open_raw','low_raw','high_raw','close_raw']}
            fig=go.Figure(go.Candlestick(x=days['date'],open=candles['open_raw'],low=candles['low_raw'],high=candles['high_raw'],close=candles['close_raw']))
            for value in [spec['dip_pct'],spec['rally_pct']]:
                fig.add_hline(y=value,line_dash='dot')
            fig.update_layout(yaxis_title='درصد از مرجع همان روز',xaxis_rangeslider_visible=False)
            tick_rows=days.iloc[::max(1,len(days)//8)]
            fig.update_xaxes(tickvals=tick_rows['date'],ticktext=[to_jalali(d) for d in tick_rows['date']])
            st.plotly_chart(fig,width='stretch')
            view=days[['date','open_raw','low_raw','high_raw','close_raw','ref_price_raw','event_confirmed','evidence']].copy()
            view['date']=view['date'].map(to_jalali)
            view['evidence']=view['evidence'].map(REASONS)
            view['event_confirmed']=view['event_confirmed'].map({True:'بله',False:'خیر'}).fillna('نامعلوم')
            st.dataframe(view.rename(columns=dict(date='روز',open_raw='اولین: ریال',low_raw='کمترین: ریال',high_raw='بیشترین: ریال',
                close_raw='آخرین: ریال',ref_price_raw='مرجع: ریال',event_confirmed='رخداد اثبات شد',evidence='نوع شاهد')),hide_index=True,width='stretch')
        else:
            st.info('برای این نماد در بازه پرسش کندل محفوظی وجود ندارد؛ نبود داده، صفر رخداد نیست.')
    if st.button('ادامه تطبیق آرشیو همین پرسش (۱۰۰ روز-نماد)',disabled=jobs.job_status('market-query')['running']):
        jobs.start_job('market-query',['query','--resume',str(folder),'--verify-trades','--max-trade-days','100'])
        st.rerun()
