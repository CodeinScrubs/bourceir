/** فقط DOM عمومی تابلو. هیچ storage، کوکی، API حساب یا داخلی برنامه خوانده نمی‌شود. */
export const origin='https://farabixo.irfarabi.com';
export function tehranNow(date=new Date()) {
  const parts=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Tehran',year:'numeric',month:'2-digit',day:'2-digit',
    hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).formatToParts(date);
  const p=Object.fromEntries(parts.map(x=>[x.type,x.value]));
  return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}:${p.second}.${String(date.getUTCMilliseconds()).padStart(3,'0')}+03:30`;
}
export function readBoardDom() {
  const norm=s=>s.replace(/\s+/g,' ').trim();
  const field=label=>{
    const es=Array.from(document.querySelectorAll('span')).filter(e=>norm(e.innerText)===label);
    return es.length===1?es[0].parentElement.innerText:null;
  };
  const ts=Array.from(document.querySelectorAll('table')).filter(t=>norm(t.querySelector('thead')?.innerText||'')==='تعداد حجم قیمت قیمت حجم تعداد');
  return {url:location.href,title:document.title,
    identity:document.querySelector('input[placeholder="جست‌و‌جوی نماد یا شرکت"]')?.value??null,
    volume:field('حجم معاملات'),last_trade:field('زمان آخرین معامله'),limits:field('بازه مجاز روزانه'),
    depth:ts.length===1?Array.from(ts[0].querySelectorAll('tbody tr')).map(r=>Array.from(r.querySelectorAll('td')).map(e=>e.innerText)):null};
}
export function normalized(text) {
  return String(text).replace(/[۰-۹]/g,c=>String(c.charCodeAt(0)-1776)).replace(/[٠-٩]/g,c=>String(c.charCodeAt(0)-1632))
    .replace(/٬/g,',').replace(/[\u200e\u200f\u202a-\u202e\u2066-\u2069]/g,'').replace(/[يى]/g,'ی').replace(/ك/g,'ک').replace(/ة/g,'ه').replace(/\s+/g,' ').trim();
}
function num(s) {
  const t=normalized(s);
  if(!/^(?:\d+|\d{1,3}(?:,\d{3})+)$/.test(t))throw new Error('عدد صحیح تابلو شناخته‌شده نیست');
  const n=Number(t.replaceAll(',',''));if(!Number.isSafeInteger(n))throw new Error('عدد تابلو خارج از حد است');return n;
}
function pair(s) { const p=normalized(s).split(/\s+/);if(p.length!==2)throw new Error('قیمت/حجم مظنه مبهم است');return p.map(num); }
export function parseBoard(raw, receivedAt) {
  const at=new Date(receivedAt);
  if(!Number.isFinite(at.getTime()) || tehranNow(at)!==receivedAt)throw new Error('زمان دریافت باید تهران و استاندارد باشد');
  const u=new URL(raw.url);
  if(u.origin!==origin || u.pathname!=='/' || [...u.searchParams.keys()].some(k=>k!=='isin')
    || u.searchParams.getAll('isin').length!==1 || !/^[A-Z0-9]{12}$/.test(u.searchParams.get('isin')||''))
    throw new Error('نشانی تابلوی فارابی نامعتبر است');
  // همان قرارداد core.text.normalize_symbol: حروف فارسی، فاصله یکسان و حذف نیم‌فاصله.
  const symbol=normalized(raw.identity||'').split(' - ')[0].replace(/\u200c/g,'');
  if(!symbol || !normalized(raw.identity).includes(' - '))throw new Error('هویت تابلو کامل نیست');
  const title=normalized(raw.title||'').replace(/\u200c/g,'');const prefix=symbol+' ';
  if(!title.startsWith(prefix))throw new Error('هویت عنوان با تابلو برابر نیست');
  const priceText=title.slice(prefix.length).match(/^([\d,]+)\s+[▲▼−-]?\s*[\d.]+%$/);
  if(!priceText)throw new Error('قیمت عنوان تابلو شناخته‌شده نیست');
  const last=num(priceText[1]);if(last<=0)throw new Error('قیمت نامعلوم است');
  const v=normalized(raw.volume||'').match(/^حجم معاملات\s+([\d,]+)$/);
  if(!v)throw new Error('حجم معاملات در DOM موجود نیست');
  const limits=normalized(raw.limits||'').match(/^([\d,]+)\s+بازه مجاز روزانه\s+([\d,]+)$/);
  if(!limits)throw new Error('دامنه مجاز موجود نیست');
  const low=num(limits[1]),high=num(limits[2]);
  if(low<=0||high<low||last<low||last>high)throw new Error('قیمت و دامنه مجاز ناسازگارند');
  const stamp=normalized(raw.last_trade||'').match(/^زمان آخرین معامله\s+(\d{4})\/(\d{1,2})\/(\d{1,2})\s+(\d{2}:\d{2}:\d{2})$/);
  if(!stamp)throw new Error('تاریخ جلسه در تابلو معلوم نیست');
  const today=new Intl.DateTimeFormat('en-US-u-ca-persian',{timeZone:'Asia/Tehran',year:'numeric',month:'numeric',day:'numeric'}).formatToParts(at);
  const p=Object.fromEntries(today.map(x=>[x.type,x.value]));
  if(Number(p.year)!==Number(stamp[1])||Number(p.month)!==Number(stamp[2])||Number(p.day)!==Number(stamp[3]))
    throw new Error('تابلو متعلق به جلسه امروز نیست');
  const tradeStamp=receivedAt.slice(0,10)+'T'+stamp[4]+'+03:30';
  const age=(at-new Date(tradeStamp))/1000;
  if(!Number.isFinite(age)||age<0||!/^([01]\d|2[0-3]):[0-5]\d:[0-5]\d$/.test(stamp[4]))throw new Error('ساعت معامله نامعتبر یا آینده است');
  if(!Array.isArray(raw.depth)||raw.depth.length!==5)throw new Error('پنج سطر دفتر موجود نیست');
  const rows=raw.depth.map(row=>{
    if(!Array.isArray(row)||row.length!==4||row.some(s=>typeof s!=='string'))throw new Error('ستون‌های دفتر تغییر کرده‌اند');
    const side=(cell,count)=>{
      if(!cell.trim()||/^(?:[-–—]|0)$/.test(cell.trim())) {
        if(count.trim()&&!/^(?:[-–—]|0)$/.test(count.trim()))throw new Error('مظنه ناقص است');return null;
      }
      const [price,volume]=pair(cell),n=num(count);
      if(price<=0||volume<=0||n<=0)throw new Error('مظنه مثبت لازم است');
      return {price,volume,count_display:n,within_band:price>=low&&price<=high};
    };return {bid:side(row[1],row[0]),ask:side(row[2],row[3])};
  });
  for(const side of ['bid','ask']) {
    let prev=null,ended=false;
    for(const row of rows){const x=row[side];if(!x){ended=true;continue;}
      if(ended||prev!==null&&(side==='bid'?x.price>=prev:x.price<=prev))throw new Error('ترتیب مظنه‌ها معتبر نیست');prev=x.price;}
  }
  if(rows[0].bid&&rows[0].ask&&rows[0].bid.price>rows[0].ask.price)throw new Error('دفتر متقاطع است');
  const queue=(side,bound)=>!rows[0][side]||!rows[0][side].within_band?null:rows[0][side].price===bound?rows[0][side].volume:0;
  return {key:u.searchParams.get('isin'),symbol,url:u.href,market_day:receivedAt.slice(0,10),observed_at:receivedAt,
    price_unit:'Rial',last_price:last,volume:num(v[1]),low_limit:low,high_limit:high,
    sell_queue:queue('ask',low),buy_queue:queue('bid',high),rows,trade_timestamp:tradeStamp,trade_age_seconds:age,
    independent_book_clock_verified:false,identity_mapping_independently_verified:false,exchange_latency_seconds:null,research_universe_member:null,
    out_of_band_levels:rows.reduce((n,row)=>n+['bid','ask'].filter(s=>row[s]&&!row[s].within_band).length,0)};
}
