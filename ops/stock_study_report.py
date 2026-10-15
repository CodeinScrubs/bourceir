"""گزارش HTML مستقل و آفلاین برای شواهد مطالعه؛ متن علت از اعداد ساخته نمی‌شود."""

from html import escape

import plotly.graph_objects as go
from plotly.offline import get_plotlyjs
from plotly.subplots import make_subplots

from core import market_rules
from core.timeutils import to_jalali


def _number(value, digits=2):
    return 'نامعلوم' if value is None else f'{value:,.{digits}f}'


def _table(headers, rows):
    return '<div class="scroll"><table><thead><tr>'+''.join('<th>'+escape(str(v))+'</th>' for v in headers)+\
        '</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+escape(str(v))+'</td>' for v in row)+'</tr>' for row in rows)+\
        '</tbody></table></div>'


def _news(records, limit=6):
    rows=[]
    for row in sorted(records,key=lambda row:row['published_at'],reverse=True)[:limit]:
        url=row.get('url') or ''
        # سندِ ورودی نمی‌تواند scheme اجرایی یا دامنه بیرونی بسازد.
        safe=url.startswith(('https://codal.ir/','https://www.codal.ir/'))
        title=escape(row['title'])
        label=f'<a href="{escape(url,quote=True)}">{title}</a>' if safe else title
        rows.append(f'<li>{escape(to_jalali(row["published_at"],with_time=True))} — {label} '
                    f'— پیگیری {int(row["tracing_no"])}</li>')
    return '<ul>'+''.join(rows)+'</ul>' if rows else '<p>ردیفی در ورودی این مطالعه یافت نشد؛ نبود خبر تأیید نشده است.</p>'


def render_stock_study(result, panel, target):
    title=f'مطالعهٔ {len(result["cases"])} نماد و کنترل ادعاهای رشد'
    parts=[f'<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
           f'<title>{title}</title><style>body{{font:16px/1.9 Tahoma,Arial,sans-serif;background:#f4f7fa;color:#17283b;margin:0}}'
           'main{max-width:1200px;margin:auto;padding:24px}section,header{background:white;padding:24px;margin:18px 0;border-radius:12px}'
           'h1,h2,h3{line-height:1.6}a{color:#086c92}.note{background:#fff1ce;padding:14px;border-radius:8px}'
           'table{border-collapse:collapse;width:100%;font-size:14px}th,td{padding:9px;border:1px solid #d7e0e6;text-align:right;white-space:nowrap}'
           'th{background:#eaf2f7}.scroll{overflow:auto}nav a{display:inline-block;margin:4px;padding:5px 10px;background:#eaf2f7;border-radius:7px}'
           '.chart{direction:ltr}code{direction:ltr;display:inline-block}small{color:#4f6273}@media(max-width:600px){main{padding:8px}section,header{padding:14px}}'
           '</style><main>',
           f'<header><h1>{title}</h1><p>قرارداد {escape(result["version"])} — زمان ساخت '
           f'{escape(to_jalali(result["created_at"],with_time=True))}</p>'
           '<p class="note">این گزارش، مطالعهٔ توصیفیِ سهم‌های انتخاب‌شده پس از رشد است. '
           'اثر اقتصادی افزایش سرمایه/سود نقدی، امکان اجرا و علت قطعی از نمودار استنتاج نمی‌شوند. '
           'نتیجهٔ این دوره برای فرضیه‌های برگرفته از آن، آزمون ندیده نیست. سیگنال و احتمال سود نداریم.</p>'
           '<p>قیمت‌ها و تعداد سهام از منابع عمومی‌اند. سهم در قیاس صنعت/بازار خودش حذف شده است؛ '
           'عضویت و صنعت، متادیتای فعلی‌اند و تاریخچه رسمی عضویت نداریم. میانگین زیرمجموعه معلوم '
           'با پوشش کنار آن نشان داده می‌شود؛ اگر عضوی نامعلوم باشد، میانگین کل نامعلوم می‌ماند.</p></header>']
    rows=[]
    for case in result['cases']:
        w=case.get('window',{})
        cap=case.get('market_cap') or {}
        lo,hi=cap.get('rank_lower_bound'),cap.get('rank_upper_bound')
        rows.append([case['symbol'],case.get('sector_name') or case.get('sector_code'),to_jalali(case['start']),
                     _number(w.get('last_price_multiple'))+'×',_number(w.get('synthetic_multiple'))+'×',
                     _number(cap.get('market_cap_rial')/1e13 if cap.get('market_cap_rial') is not None else None),
                     str(lo) if lo==hi and lo is not None else f'{lo} تا {hi}' if lo else 'نامعلوم'])
    parts += ['<section><h2>خلاصهٔ مشاهده</h2>',_table(['نماد','گروه رسمی فعلی','شروع','نسبت آخرین قیمت خام',
               'نسبت شاخص مصنوعی PIT','ارزش بازار مشاهده؛ همت','رتبه در جامعه مشاهده'],rows),
              '<p>همت = هزار میلیارد تومان. ارزش بازار = تعداد سهام مشاهده‌شده × آخرین قیمت ریالی ÷ ۱۰¹³. '
              'این رتبه بر اساس مشاهده فعلی و تاریخ اثر تأییدنشده تعداد سهام است؛ رتبه تاریخیِ روز شروع/پایان نیست. '
              'مقادیر مفقود در کران رتبه باقی می‌مانند. قیمت پایین هر برگه به معنی شرکت کوچک یا ارزنده نیست.</p>',
              '<nav>'+''.join(f'<a href="#s{i}">{escape(row["symbol"])}</a>' for i,row in enumerate(result['cases']))+'</nav></section>',
              '<script>'+get_plotlyjs()+'</script>']
    if result['market_cap_universe']:
        scope=result['market_cap_universe'][0]
        parts.append('<section><p>جامعه ارزش بازار مشاهده‌شده: '+str(scope['universe_total'])+' سهم عادی؛ '
                     +str(scope['universe_known'])+' مقدار معلوم و '+str(scope['universe_missing'])+' نامعلوم. زمان دریافت: '
                     +escape(to_jalali(result['market_observation_at'],with_time=True))+'. این فهرست، تاریخچه همه اعضای بازار نیست.</p></section>')
    for index,case in enumerate(result['cases']):
        symbol=case['symbol']
        parts.append(f'<section id="s{index}"><h2>{escape(symbol)} — {escape(case.get("name", ""))}</h2>')
        if case['status']=='missing_history':
            parts.append('<p>سابقه معتبر موجود نیست؛ هیچ عددی جایگزین نشده است.</p></section>');continue
        w=case['window'];cap=case.get('market_cap') or {};peers=case['peers']
        parts.append(f'<p>{escape(case.get("sector_name") or "")}؛ کد گروه {escape(str(case["sector_code"]))}. '
                     f'بازه: {escape(to_jalali(case["start"]))} تا {escape(to_jalali(case["end"]))}.</p>')
        if cap:
            parts.append(_table(['تعداد سهام مشاهده','آخرین تابلو؛ تومان','زمان منبع قیمت','رتبه در مقادیر معلوم / جامعه'],[
                [_number(cap['shares_observed'],0),_number(cap['last_price_observed_rial']/10 if cap['last_price_observed_rial'] is not None else None),
                 to_jalali(cap['provider_timestamp'],with_time=True) if cap['provider_timestamp'] else 'نامعلوم',
                 f'{cap["rank_among_known"]} / {cap["universe_total"]}']]))
        if case.get('start_basis')=='common_window_assumption_no_user_start':
            parts.append('<p class="note">قیمت/شروع دقیق از کاربر داده نشده؛ برای مقایسه، ۳ خرداد فرض شده است. '
                         'این تاریخ، شروع کشف‌شدهٔ صعود نیست.</p>')
        if not w['exact_endpoints']:
            parts.append('<p class="note">حداقل یک تاریخ دقیق در آرشیو نیست؛ روز نزدیک جانشین نشده و بازده بازه نامعلوم است.</p>')
        parts.append(_table(['تاریخ دقیق','آخرین؛ تومان','پایانی رسمی؛ تومان','حجم','ارزش معاملات؛ میلیارد تومان'],[
            [to_jalali(row['date']),_number(row['close_raw']/10 if row['close_raw'] is not None else None),
             _number(row['close_final_raw']/10 if row['close_final_raw'] is not None else None),
             _number(row['volume'],0),_number(row['value']/1e10 if row['value'] is not None else None)] for row in case['endpoints']]))
        claim_buy,claim_sell=case.get('start_price_claim_toman'),case.get('end_price_claim_toman')
        if claim_buy and claim_sell:
            parts.append(f'<p>قیمت ادعاشده: {_number(claim_buy)} ← {_number(claim_sell)} تومان؛ نسبت حسابی '
                         f'{claim_sell/claim_buy:.3f} برابر، رشد {(claim_sell/claim_buy-1)*100:.2f}٪. '
                         'این نسبت از اعداد پیام است؛ ستون‌های بالا، دادهٔ مستقل آرشیو هستند.</p>')
        parts.append(f'<p>نسبت پایانی خام: {_number(w["official_price_multiple"])}×؛ شاخص مصنوعی PIT: '
                     f'{_number(w["synthetic_multiple"])}×؛ افت بیشینهٔ پایانی شاخص در مسیر: '
                     f'{_number(-100*w["synthetic_close_drawdown"] if w["synthetic_close_drawdown"] is not None else None)}٪؛ '
                     f'{len(w["reference_gaps"])} شکاف مرجع. بازده اقتصادی و بازده قابل اجرا: نامعلوم.</p>')
        if w['reference_gaps']:
            parts.append(_table(['تاریخ شکاف','نسبت مرجع به پایانی پیشین'],[
                [to_jalali(row['date']),_number(row['reference_ratio'],5)] for row in w['reference_gaps']]))
            parts.append('<p>شکاف مرجع به‌تنهایی نوع افزایش سرمایه، سود نقدی یا حق‌تقدم را ثابت نمی‌کند.</p>')
        if case.get('evidence_notes'):
            parts.append('<h3>شواهد شرکتی و برداشت محتاطانه</h3>')
            for note in case['evidence_notes']:
                url=note.get('url','')
                source=f'<a href="{escape(url,quote=True)}">اصل سند</a>' if url.startswith(('https://codal.ir/','https://www.codal.ir/')) else ''
                parts.append('<p>'+escape(note['text'])+' '+source+'</p>')
            parts.append('<p>یادداشت‌های فوق بررسی انسانیِ سند/متن صفحه‌اند؛ استخراج عددی کامل پیوست‌ها و اثر علّی قیمت تأیید نشده است.</p>')
        if case.get('mechanism_to_check'):
            parts.append('<p><strong>مسیر اقتصادی قابل بررسی:</strong> '+escape(case['mechanism_to_check'])+'</p>')
        if case.get('unresolved_company_evidence'):
            parts.append('<p><strong>شاهد شرکتی باقی‌مانده:</strong> '+escape(case['unresolved_company_evidence'])+'</p>')
        parts.append('<h3>پیش از شروع انتخابی چه دیده می‌شد؟</h3>')
        age=case['pre_20']['calendar_days_since_last_observation']
        parts.append('<p>پنجره‌ها مشاهده ثبت‌شده‌اند؛ تقویم کامل و علت نبود روزهای میانی تأیید نشده است. '
                     'فاصله آخرین مشاهده پیشین تا شروع: '+(_number(age,0) if age is not None else 'نامعلوم')+' روز تقویمی. '
                     '۲۰ مشاهده، الزاماً ۲۰ روز اخیر یا ۲۰ جلسه متوالی بازار نیست.</p>')
        parts.append(_table(['پنجره پیشین','تعداد موجود','آخرین روز مجاز','بازده شاخص مصنوعی','میانگین ارزش؛ میلیارد تومان','تک‌قیمتی صعودی/نزولی'],[
            [n,case[f'pre_{n}']['observed_rows'],to_jalali(case[f'pre_{n}']['end']),
             _number(case[f'pre_{n}']['synthetic_return']*100 if case[f'pre_{n}']['synthetic_return'] is not None else None)+'٪',
             _number(case[f'pre_{n}']['mean_value_rial']/1e10 if case[f'pre_{n}']['mean_value_rial'] is not None else None),
             f'{case[f"pre_{n}"]["locked_up_rows"]}/{case[f"pre_{n}"]["locked_down_rows"]}'] for n in (20,60)]))
        parts.append(_table(['متریک پیشین','معلوم از ۲۰','میانه زیرمجموعه معلوم','میانگین کامل'],[
            [label,case['flow_pre_20'][field]['known'],_number(case['flow_pre_20'][field]['known_subset_median']),
             _number(case['flow_pre_20'][field]['full_mean'])] for field,label in (
                ('buyer_power_value','قدرت خریدار ارزشی'),('volume_ratio_20d','حجم نسبت به ۲۰ جلسه قبلی'),
                ('net_real_value_rial','خالص ارزش حقیقی؛ ریال'))]))
        parts.append('<p>این نشانه‌ها قبل از تاریخ انتخابی‌اند؛ تاریخ انتخابی الزاماً شروع واقعی حرکت نیست. '
                     'شمارنده حقیقی، پول یک فرد یا کد خاص را شناسایی نمی‌کند؛ داده ناسازگار حذف یا صفر نشده، خروجی وابسته نامعلوم است.</p>')
        parts.append('<h3>بازار، صنعت و نمونه‌های مقایسه</h3>')
        parts.append(_table(['قیاس با حذف خود سهم','معلوم/مورد انتظار','میانه نسبت معلوم','میانگین نسبت معلوم','میانگین همه','دوبرابری معلوم'],[
            [label,f'{peers[key]["known"]}/{peers[key]["expected"]}',_number(peers[key]['known_subset_median_multiple']),
             _number(peers[key]['known_subset_mean_multiple']),_number(peers[key]['mean_multiple']),
             peers[key]['known_subset_doubled']] for key,label in (('market','بازار متادیتای فعلی'),('sector','گروه رسمی فعلی'))]))
        parts.append(_table(['همتای نزدیک از نظر نقدشوندگی پیشین','بازده ۲۰ جلسه پیشین','نسبت مصنوعی در همین بازه'],[
            [row['symbol'],_number(row['pre_20']['synthetic_return']*100 if row['pre_20']['synthetic_return'] is not None else None)+'٪',
             _number(row['synthetic_multiple'])] for row in peers['matched_by_prior_liquidity']]))
        parts.append('<p>همتاها با مقدار رشد آینده انتخاب نشده‌اند: حداکثر پنج عضو همان صنعت فعلی با نزدیک‌ترین ارزش معاملات '
                     '۲۰ مشاهده پیشین. تطبیق اندازه/شناور تاریخی اثبات نشده است. شاخص رسمی صنعت، زیرصنعت اقتصادی دقیق '
                     '(مس/روی/رنگ/پخش) یا اثر علّی از این قیاس ساخته نمی‌شود.</p>')
        parts.append('<h3>اطلاعیه‌های پیش از ساعت ۹ روز شروع</h3>'+_news(case['news_before_start']))
        parts.append('<h3>اطلاعیه‌های همان روز پس از شروع معاملات</h3>'+_news(case['news_start_day_after_open']))
        parts.append('<details><summary>اطلاعیه‌های منتشرشده حین رشد؛ ورودی روز شروع نیستند</summary>'+_news(case['news_during_window'],12)+'</details>')
        parts.append('<p>فهرست و زمان انتشار از منبع کدال‌اند؛ محتوای عددی اصل پیوست‌ها جدا باید تأیید شود. '
                     'عنوان گزارش، اثبات بهبود سود یا علت جهش نیست. اصلاحیه به زمان نسخه اولیه منتقل نمی‌شود.</p>')
        p=panel.loc[(panel.symbol==symbol)&(panel.date<=case['end'])].tail(250).copy()
        if not p.empty:
            figure=make_subplots(rows=2,cols=1,shared_xaxes=True,vertical_spacing=.08,row_heights=[.7,.3])
            figure.add_trace(go.Scatter(x=p.date,y=market_rules.official_close(p)/10,name='پایانی خام؛ تومان',connectgaps=False),row=1,col=1)
            figure.add_trace(go.Bar(x=p.date,y=p.value/1e10,name='ارزش معاملات؛ میلیارد تومان'),row=2,col=1)
            figure.add_vline(x=case['start'],line_dash='dash',line_color='#c36518')
            figure.update_layout(height=480,margin=dict(l=55,r=20,t=20,b=40),template='plotly_white',
                                 font=dict(family='Tahoma,Arial'),legend=dict(orientation='h',y=1.1),hovermode='x unified')
            figure.update_yaxes(title_text='تومان، خام',row=1,col=1)
            figure.update_yaxes(title_text='میلیارد تومان',row=2,col=1)
            parts.append('<div class="chart">'+figure.to_html(full_html=False,include_plotlyjs=False,config={'responsive':True})+'</div>')
        parts.append('</section>')
    parts.append('<section><h2>کارهای باز برای روش قابل معامله</h2><p>حقوق اقتصادی اقدامات شرکتی و زمان وصول/فروش؛ '
                 'عضویت، تعداد سهام و شناور تاریخ‌دار؛ داده واقعی صف و وضعیت معامله؛ زمان اولین انتشار و اصلاحات؛ '
                 'هزینه و قواعد تاریخ‌دار؛ آزمون کنترل‌شده خارج از نمونه و ثبت تصمیم پیش از نتیجه. '
                 'نوشتن کد و امتیاز ۶۲/۶۶/۱۰۰، جای این شواهد را نمی‌گیرد.</p><p>این گزارش هیچ سفارش یا توصیه خرید و فروش ندارد.</p></section></main></html>')
    target.write_text(''.join(parts),encoding='utf-8')
