"""گزارش مستقل فارسی با جدول جامعه کامل، شاهد روز و کندلِ نسبت به مرجع روز."""

import csv
import html
import json
from decimal import Decimal

from plotly.offline import get_plotlyjs

from core.timeutils import to_jalali

MEMBERSHIP = {'inside':'داخل بازه', 'outside':'بیرون بازه', 'boundary_uncertain':'مرز نامطمئن', 'unknown':'ارزش نامعلوم'}
REASONS = {'not_touched':'دو آستانه لمس نشد', 'open_before_high':'شروع روز منفی، سپس سقف مثبت',
           'low_before_last':'کف منفی، آخرین معامله مثبت', 'order_unknown':'ترتیب نامعلوم',
           'ordered_trade_witness':'شاهد ریزمعاملات', 'daily_trade_conflict':'تعارض کندل و ریزمعاملات',
           'complete_trades_no_event':'آرشیو کامل؛ ترتیب رخ نداد', 'invalid_daily':'داده روز نامعتبر'}


def escaped(value):
    return html.escape(str(value)) if value is not None else 'نامعلوم'


def cell(value):
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')):
        return "'"+value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    return value


def export_csv(path, rows, fields):
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            writer.writerow({k:cell(row.get(k)) for k in fields})


def cap_text(row):
    value = row['market_cap_rial']
    return f'{Decimal(value)/Decimal(10_000_000_000):,.3f}' if value is not None else 'نامعلوم'


def summary_table(result):
    rows = []
    for r in result['results']:
        rows.append('<tr>'+''.join(f'<td>{escaped(v)}</td>' for v in (
            r['symbol_fa'], MEMBERSHIP[r['slice_membership']], r['rank_lower'], r['rank_upper'], cap_text(r),
            r['confirmed_count'], r['possible_count'], r['ambiguous_count'], r['invalid_sessions'],
            r['observed_sessions'], r['missing_market_dates'], r['where_result']))+'</tr>')
    headers = ('نماد','عضویت بازه','رتبه از','رتبه تا','ارزش: میلیارد تومان','تأییدشده','سقف در داده موجود',
               'ترتیب مبهم','روز نامعتبر','روز موجود','روز بازار بدون کندل','شرط اضافی')
    return '<table><thead><tr>'+''.join(f'<th>{h}</th>' for h in headers)+'</tr></thead><tbody>'+''.join(rows)+'</tbody></table>'


def write_report(folder, result):
    caps, spec = result['caps'], result['protocol']['spec']
    export_csv(folder/'companies.csv', caps['rows'], ('symbol_fa','name_fa','ins_code','sector_code','shares_outstanding',
        'metadata_observed_at','price_rial','price_date','price_captured_at','price_provider_timestamp','price_received_at',
        'price_kind','market_cap_rial','rank_lower','rank_upper','slice_membership'))
    export_csv(folder/'summary.csv', result['results'], ('symbol_fa','ins_code','sector_code','market_cap_rial','rank_lower',
        'rank_upper','slice_membership','confirmed_count','possible_count','ambiguous_count','invalid_sessions','observed_sessions',
        'valid_sessions','missing_market_dates','avg_value_rial','event_rate_among_valid_pct','where_result','full_year_count_verified'))
    days, chart = [], {}
    names = {str(r['symbol_id']):r['symbol_fa'] for r in result['results']}
    for key, records in result['details'].items():
        chart[key] = []
        for row in records:
            tick = row.get('tick_evidence') or {}
            witness = tick.get('witness') or {}
            day = dict(row, symbol_fa=names[key], jalali=to_jalali(row['date'],fa_digits=False),
                tick_complete=tick.get('complete'), tick_issues=tick.get('issues'), **witness)
            days.append(day)
            chart[key].append({k:day.get(k) for k in ('date','jalali','open_raw','low_raw','high_raw','close_raw','ref_price_raw',
                'event_confirmed','event_possible','evidence','tick_complete','dip_clock','rally_clock','dip_price_rial','rally_price_rial')})
    export_csv(folder/'days.csv', days, ('symbol_fa','date','jalali','open_raw','low_raw','high_raw','close_raw','close_final_raw',
        'ref_price_raw','volume','value','trade_count','event_confirmed','event_possible','evidence','tick_complete',
        'dip_number','dip_clock','dip_price_rial','rally_number','rally_clock','rally_price_rial','tick_issues','source','captured_at'))
    description = (
        f"جامعه {caps['universe_total']} سهم عادی فعال؛ ارزش معلوم {caps['caps_known']} و نامعلوم {caps['caps_unknown']}. "
        f"برش صدک {spec['cap_low']} تا {spec['cap_high']} از کوچک‌ترین‌ها، رتبه‌های نزولی {caps['slice_first_rank']} تا {caps['slice_last_rank']}. "
        f"بازه {to_jalali(spec['start'])} تا {to_jalali(spec['end'])}. "
        f"رخداد: ابتدا معامله در {spec['dip_pct']}٪ یا پایین‌تر و سپس معامله‌ای بعد از آن در {spec['rally_pct']}٪ یا بالاتر از مرجع همان روز؛ هر روز یک بار."
    )
    limitations = (
        'تأییدشده، کران پایین رخدادها در روزهای موجود است. سقف نیز فقط به داده موجود مربوط است؛ روز گم‌شده صفر نیست. '
        'کندل در صورت شروع روز در آستانه منفی، یا پایان آخرین معامله در آستانه مثبت، ترتیب را ثابت می‌کند. باقی کندل‌ها به ریزمعاملات نیاز دارند. '
        'قیمت پایانی رسمی و رنگ سبز کندل، به‌تنهایی ترتیب را ثابت نمی‌کنند. '
        'جامعه و تعداد سهام مربوط به مشاهده فعلی‌اند؛ این انتخاب، جامعه تاریخیِ قابل مشاهده در ابتدای دوره نیست. '
        'قیمت ارزش شرکت، آخرین معامله خام با تاریخ مخصوص هر نماد است؛ مظنه زنده یا تاریخی بودن تعداد سهام تأیید نشده است. '
        'تقویم کامل رسمی و پوشش کامل یک سال تأیید نشده‌اند؛ روز بدون کندل می‌تواند تعطیلی، توقف یا فقدان داده باشد. '
        'مرتب‌سازی با شمار تأییدشده است؛ برتری قطعی رتبه سالانه، احتمال تکرار، امکان خریدوفروش یا سودآوری از آن نتیجه نمی‌شود.'
    )
    md = ['# پاسخ توصیفی پرسش بازار', '', description, '', limitations, '',
          '| نماد | عضویت | تأییدشده | سقف در داده موجود | مبهم | روز موجود |', '|---|---|---:|---:|---:|---:|']
    for r in result['results']:
        md.append(f"| {r['symbol_fa']} | {MEMBERSHIP[r['slice_membership']]} | {r['confirmed_count']} | {r['possible_count']} | {r['ambiguous_count']} | {r['observed_sessions']} |")
    (folder/'report.md').write_text('\n'.join(md)+'\n',encoding='utf-8')
    payload = json.dumps(dict(chart=chart, names=names, reasons=REASONS, spec=spec),ensure_ascii=False,allow_nan=False)
    payload = payload.replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026').replace('\u2028','\\u2028').replace('\u2029','\\u2029')
    options = ''.join(f'<option value="{escaped(r["symbol_id"])}">{escaped(r["symbol_fa"])} — {MEMBERSHIP[r["slice_membership"]]}</option>' for r in result['results'])
    cap_rows = ''.join('<tr>'+''.join(f'<td>{escaped(v)}</td>' for v in (r['symbol_fa'],r['name_fa'],r['shares_outstanding'],
        r['price_rial'],r['price_date'],cap_text(r),r['rank_lower'],r['rank_upper'],MEMBERSHIP[r['slice_membership']]))+'</tr>' for r in caps['rows'])
    template = '''<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>پرسش‌گر بازار BRS</title><style>body{font:16px Tahoma,sans-serif;background:#f2f5f8;color:#17283c;margin:0;padding:24px}
main{max-width:1500px;margin:auto}section,header{background:white;border:1px solid #d8e0ea;border-radius:12px;padding:20px;margin:16px 0}
h1,h2{color:#123a59}p{line-height:2}table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:9px;border-bottom:1px solid #dae2ea;text-align:right;white-space:nowrap}
th{background:#edf4fa;position:sticky;top:0}tbody tr:hover{background:#f0f8ff}.scroll{overflow:auto;max-height:520px}.notice{border-right:5px solid #bb800e;background:#fff7e0}
select{font:inherit;min-width:270px;padding:9px}#chart{height:470px}code{direction:ltr;display:inline-block}a{color:#07599a}.meta{font-size:12px;color:#4d6178}</style>
<main><header><h1>پرسش‌گر بازار BRS</h1><p>__DESCRIPTION__</p><p class="meta">شناسه: __ID__ | ثبت: __REGISTERED__ | ارزیابی: __EVALUATED__</p>
<p>فایل‌های همراه: <a href="companies.csv">همه شرکت‌ها و رتبه‌ها</a> · <a href="summary.csv">خلاصه نمادها</a> · <a href="days.csv">همه روزها و شواهد</a> · <a href="result.json">داده کامل و قرارداد</a></p></header>
<section class="notice"><p>__LIMITS__</p><p>روزهای کاندید: __CANDIDATES__؛ دارای ارجاع آرشیو: __REFERENCES__. تغییر آستانه یا دامنه، پرسش تازه محسوب می‌شود.</p></section>
<section><h2>شمار رخدادها و کیفیت پوشش</h2><div class="scroll">__SUMMARY__</div></section>
<section><h2>کندل و شاهد هر روز</h2><label>نماد <select id="symbol">__OPTIONS__</select></label><p>محور قیمت: درصد نسبت به مرجع همان روز؛ نمودار بازده پیوسته یا تعدیل‌شده نیست. قیمت انتهای کندل، آخرین معامله است.</p>
<div id="chart"></div><div class="scroll"><table><thead><tr><th>روز</th><th>وضعیت</th><th>نوع شاهد</th><th>تطبیق کامل تیک</th><th>زمان منفی</th><th>زمان مثبت بعدی</th></tr></thead><tbody id="days"></tbody></table></div></section>
<section><h2>جامعه کامل، از بیشترین ارزش به کمترین</h2><p>رتبه از/تا، اثر هم‌ارزش‌ها و شرکت‌های با ارزش نامعلوم را حفظ می‌کند. ارزش‌ها به میلیارد تومان و قیمت به ریال‌اند.</p>
<div class="scroll"><table><thead><tr><th>نماد</th><th>شرکت</th><th>تعداد سهام</th><th>قیمت: ریال</th><th>تاریخ قیمت</th><th>ارزش: میلیارد تومان</th><th>رتبه از</th><th>رتبه تا</th><th>عضویت</th></tr></thead><tbody>__CAPS__</tbody></table></div></section></main>
<script>__PLOTLY__</script><script id="payload" type="application/json">__PAYLOAD__</script><script>
const d=JSON.parse(document.getElementById('payload').textContent),select=document.getElementById('symbol');
function clock(v){return v==null?'—':String(v).padStart(6,'0').replace(/(..)(..)(..)/,'$1:$2:$3')}
function render(){const rows=d.chart[select.value]||[],valid=rows.filter(r=>r.ref_price_raw>0&&r.open_raw>0&&r.low_raw>0&&r.high_raw>0&&r.close_raw>0),pct=(r,k)=>(r[k]/r.ref_price_raw-1)*100;
Plotly.react('chart',[{type:'candlestick',x:valid.map(r=>r.date),open:valid.map(r=>pct(r,'open_raw')),low:valid.map(r=>pct(r,'low_raw')),high:valid.map(r=>pct(r,'high_raw')),close:valid.map(r=>pct(r,'close_raw')),
text:valid.map(r=>r.jalali+'؛ '+(d.reasons[r.evidence]||r.evidence)),hoverinfo:'text+x+y',increasing:{line:{color:'#147d5b'}},decreasing:{line:{color:'#c04351'}}}],
{font:{family:'Tahoma'},margin:{t:20,b:35,r:60,l:55},yaxis:{title:'درصد از مرجع روز'},xaxis:{rangeslider:{visible:false}},shapes:[d.spec.dip_pct,d.spec.rally_pct].map(y=>({type:'line',xref:'paper',x0:0,x1:1,y0:y,y1:y,line:{color:'#8d9aaf',dash:'dot'}}))},{responsive:true,displaylogo:false});
const body=document.getElementById('days');body.replaceChildren();rows.filter(r=>r.event_possible!==false).forEach(r=>{const tr=document.createElement('tr');
[r.jalali+' / '+r.date,r.event_confirmed===true?'تأییدشده':r.event_possible==null?'داده نامعتبر':'نامعلوم',d.reasons[r.evidence],r.tick_complete==null?'—':r.tick_complete?'بله':'خیر',clock(r.dip_clock),clock(r.rally_clock)].forEach(v=>{const td=document.createElement('td');td.textContent=v==null?'—':String(v);tr.appendChild(td)});body.appendChild(tr)})}
select.addEventListener('change',render);render();</script></html>'''
    values = {'DESCRIPTION':escaped(description),'ID':escaped(result['protocol']['id']),
        'REGISTERED':escaped(result['protocol']['registered_at']),'EVALUATED':escaped(result['evaluated_at']),
        'LIMITS':escaped(limitations),'CANDIDATES':str(result['candidate_days']),'REFERENCES':str(len(result['trade_references'])),
        'SUMMARY':summary_table(result),'OPTIONS':options,'CAPS':cap_rows,'PLOTLY':get_plotlyjs(),'PAYLOAD':payload}
    # جایگزینی یک مرحله‌ای؛ متن داده هرگز دوباره به عنوان الگو تفسیر نمی‌شود.
    import re
    content = re.sub(r'__([A-Z]+)__', lambda m:values[m[1]], template)
    (folder/'report.html').write_text(content,encoding='utf-8')
