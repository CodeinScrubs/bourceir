import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import {BoardAlerts,validateRule} from '../core/board_alerts.mjs';
import {isBoardUrl,parseBoard,tehranNow} from '../data_collector/farabi_board.mjs';
import {reviewArchive} from '../scripts/farabi_watch_review.mjs';
import {atomic} from '../scripts/farabi_watch.mjs';

const at='2026-10-20T10:00:00.000+03:30';
const quote=(queue=59000000,volume=1000000,extra={})=>({key:'TEST00000001',symbol:'آزمون',market_day:'2026-10-20',
  observed_at:at,last_price:100,volume,sell_queue:queue,buy_queue:0,trade_age_seconds:1,...extra});
const raw=()=>({url:'https://farabixo.irfarabi.com/?isin=TEST00000001',title:'آزمون 100 ▼ 2.91%',
  identity:'آزمون - شرکت آزمون - فرابورس',volume:'حجم معاملات\n1,000,000',last_trade:'زمان آخرین معامله\n1405/7/28 09:59:59',
  limits:'100\nبازه مجاز روزانه\n110',depth:Array.from({length:5},(_,i)=>['1',`${99-i}\n10`,`${100+i}\n${1000+i}`,'1'])});

test('اولین مشاهده هشدار نمی‌سازد و آستانه دلخواه معتبر است',()=>{
  const a=new BoardAlerts();assert.deepEqual(a.observe(quote(),0).events,[]);
  assert.equal(validateRule({queue_start:50000000}).queue_start,50000000);
});
test('افت صف و حجم همراه دو رویداد مجزا با منشأ نامعلوم می‌سازند',()=>{
  const a=new BoardAlerts();a.observe(quote(),0);const r=a.observe(quote(1000000,59000000),500);
  assert.deepEqual(r.events.map(e=>e.kind),['sell_queue_drop','sell_queue_drop_with_volume','volume_burst']);
  assert.equal(r.events[0].queue_decrease,58000000);assert.equal(r.events[0].accepted_for_signals,false);
  assert.match(r.events[0].attribution,/not_separable/);
  assert.equal(a.observe(quote(900000,59100000),1000).events.length,0);
});
test('افت بدون معامله حجم کافی یا لغو قطعی معرفی نمی‌شود',()=>{
  const a=new BoardAlerts();a.observe(quote(),0);const r=a.observe(quote(1000000),500);
  assert.deepEqual(r.events.map(e=>e.kind),['sell_queue_drop']);
  assert.equal(r.events[0].queue_decrease_exceeding_reported_volume,58000000);
});
test('حجم پس از افت می‌تواند هشدار همراه را مستقل فعال کند',()=>{
  const a=new BoardAlerts();a.observe(quote(),0);a.observe(quote(1000000),500);
  assert(a.observe(quote(1000000,3000000),1000).events.some(e=>e.kind==='sell_queue_drop_with_volume'));
});
test('شکاف، روز تازه و کاهش شمارنده مبنا را از نو می‌سازند',()=>{
  for(const [q,ms,reason] of [[quote(1,2000000),4000,'sampling_gap'],[quote(1,2000000,{market_day:'2026-10-21'}),500,'new_session'],[quote(1,999),500,'counter_decreased']]){
    const a=new BoardAlerts();a.observe(quote(),0);const r=a.observe(q,ms);assert.equal(r.baseline_reason,reason);assert.equal(r.events.length,0);
  }
});
test('گم شدن دفتر، صف صفر نیست و زنجیره را قطع می‌کند',()=>{
  const a=new BoardAlerts();a.observe(quote(),0);a.observe(quote(null),500);
  assert.equal(a.observe(quote(1),1000).events.length,0);
});
test('خطای نمونه و تغییر تنظیمات مبنا را پاک می‌کنند',()=>{
  const a=new BoardAlerts();a.observe(quote(),0);a.invalidate('TEST00000001');assert.equal(a.observe(quote(1),500).events.length,0);
  a.configure({queue_start:6000000});assert.equal(a.observe(quote(0),1000).events.length,0);
});
test('تغییر دامنه یا هویت و حجم با ساعت کهنه، هشدار همراه معتبر نمی‌سازند',()=>{
  const a=new BoardAlerts();a.observe(quote(59000000,1000000,{low_limit:100,high_limit:110}),0);
  assert.equal(a.observe(quote(1,5000000,{low_limit:101,high_limit:111}),500).baseline_reason,'price_band_changed');
  const b=new BoardAlerts();b.observe(quote(),0);
  assert.deepEqual(b.observe(quote(1,9000000,{trade_age_seconds:300}),500).events.map(e=>e.kind),['sell_queue_drop']);
});
test('پنجره زمانی و دو نماد مستقل‌اند',()=>{
  const a=new BoardAlerts({window_seconds:1,max_gap_seconds:1});a.observe(quote(),0);a.observe(quote(),500);
  a.observe(quote(2000000),1400);assert.equal(a.observe(quote(1),1900).events.length,0);
  assert.equal(a.observe(quote(1,1000000,{key:'OTHER0000001'}),2100).events.length,0);
});
test('صف خرید و عبور سطح قیمت توصیفی‌اند',()=>{
  const a=new BoardAlerts({sides:['buy'],price_above:102});a.observe(quote(0,1000000,{buy_queue:10000000}),0);
  const r=a.observe(quote(0,1000000,{buy_queue:1,last_price:103}),500);
  assert.deepEqual(r.events.map(e=>e.kind),['buy_queue_drop','price_above']);
});
test('حجم روز از شروع برنامه به عنوان حجم ناگهانی جا زده نمی‌شود',()=>{
  const a=new BoardAlerts();assert.equal(a.observe(quote(0,90000000),0).events.length,0);
  assert.equal(a.observe(quote(0,90000000),500).events.length,0);
});
test('شرایط اختصاصی نماد از پیش‌فرض و نماد دیگر مستقل می‌مانند',()=>{
  const a=new BoardAlerts();a.configureForKey('TEST00000001',{queue_start:60000000});
  a.observe(quote(),0);assert.equal(a.observe(quote(1),500).events.length,0);
  a.configure({queue_start:1000000,queue_remaining:100});
  assert.equal(a.overrides.get('TEST00000001').queue_start,60000000);
  a.configureForKey('TEST00000001',null);a.observe(quote(),1000);
  assert(a.observe(quote(1),1500).events.some(e=>e.kind==='sell_queue_drop'));
});
test('ورودی‌های بولی، کسری، نامتناهی و ترتیب زمانی نامعتبر رد می‌شوند',()=>{
  for(const bad of [{queue_start:true},{min_traded:1.5},{drop_fraction:NaN},{price_above:-2},{sides:['sell','sell']},{unused:1}])assert.throws(()=>validateRule(bad));
  const a=new BoardAlerts();a.observe(quote(),0);assert.throws(()=>a.observe(quote(),0));assert.throws(()=>a.observe(quote(-1),1));
});
test('DOM تابلو با ریال و تاریخ جلسه کنترل می‌شود؛ مظنه پارک‌شده حفظ و برچسب می‌خورد',()=>{
  const q=parseBoard(raw(),at);assert.equal(q.last_price,100);assert.equal(q.sell_queue,1000);assert.equal(q.buy_queue,null);
  assert.equal(q.out_of_band_levels,5);assert.equal(q.exchange_latency_seconds,null);
});
test('شناسه فارابی الزاماً شناسه TSETMC نیست؛ ارقام فارسی پذیرفته می‌شوند',()=>{
  const r=raw();r.volume='حجم معاملات\n۱٬۰۰۰٬۰۰۰';assert.equal(parseBoard(r,at).volume,1000000);
  assert.equal(tehranNow(new Date('2026-10-20T06:30:00Z')),at);
  r.identity='آزمو‌نى - شرکت آزمون - فرابورس';r.title='آزمو‌نى 100 ▼ 2.91%';
  assert.equal(parseBoard(r,at).symbol,'آزمونی');
});
test('نشانی سفارش/میزبان دیگر/تاریخ قبل و مظنه ناقص رد می‌شوند',()=>{
  for(const change of [{url:'https://evil.test/?isin=TEST00000001'},{url:'https://farabixo.irfarabi.com/orders?isin=TEST00000001'},
    {url:'https://farabixo.irfarabi.com/?isin=TEST00000001&buy=1'},
    {url:'https://farabixo.irfarabi.com/?isin=TEST00000001#access_token=TEST_ONLY'},
    {url:'https://user@farabixo.irfarabi.com/?isin=TEST00000001'},{title:'دیگر 100 ▼ 2.91%'},
    {last_trade:'زمان آخرین معامله\n1405/7/27 10:00:00'},{last_trade:'زمان آخرین معامله\n1405/7/28 10:00:01'},
    {volume:'حجم معاملات\n-'},{depth:[]}])assert.throws(()=>parseBoard({...raw(),...change},at));
  const r=raw();r.depth[0][2]='100';assert.throws(()=>parseBoard(r,at));
});
test('صفحات حساب، ورود و بازگشت احراز هویت هرگز تابلو محسوب نمی‌شوند',()=>{
  assert(isBoardUrl(raw().url));
  for(const url of ['https://farabixo.irfarabi.com/','https://farabixo.irfarabi.com/account-turnover',
    'https://farabixo.irfarabi.com/?isin=TEST00000001&code=TEST_ONLY',
    'https://farabixo.irfarabi.com/?isin=TEST00000001#access_token=TEST_ONLY',
    'https://user@farabixo.irfarabi.com/?isin=TEST00000001'])assert.equal(isBoardUrl(url),false);
});
test('دفتر خالی صفر فرض نمی‌شود و ترتیب سطوح باید روشن باشد',()=>{
  const r=raw();r.depth=Array.from({length:5},()=>['-','-','-','-']);assert.equal(parseBoard(r,at).sell_queue,null);
  r.depth[1]=['1','100\n10','101\n10','1'];assert.throws(()=>parseBoard(r,at));
});
test('حجم خلاصه‌شده دقیق فرض نمی‌شود؛ حجم روز و قیمت همچنان قابل مشاهده‌اند',()=>{
  const r=raw();r.depth[0][2]='100\n17M';const q=parseBoard(r,at);
  assert.equal(q.sell_queue,null);assert.equal(q.rows[0].ask.volume,null);
  assert.equal(q.rows[0].ask.volume_display,'17M');assert.equal(q.rounded_volume_levels,1);
  assert.equal(q.volume,1000000);assert.equal(q.last_price,100);
});

async function archiveFixture({badResult=false}={}) {
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'brs-board-test-'));
  const code={};const hash=b=>crypto.createHash('sha256').update(b).digest('hex');
  for(const name of ['core/board_alerts.mjs','data_collector/farabi_board.mjs','scripts/farabi_watch.mjs','dashboard/board_watch.html']){
    const bytes=await fs.readFile(new URL('../'+name,import.meta.url));code[name]=hash(bytes);
    await fs.mkdir(path.dirname(path.join(dir,'code',name)),{recursive:true});await fs.writeFile(path.join(dir,'code',name),bytes);
  }
  const plan={version:'visible-board-alerts-v1',code,rule:validateRule()};
  await fs.writeFile(path.join(dir,'plan.json'),JSON.stringify(plan));
  let seq=0,previous='0'.repeat(64);const records=[];
  const add=(type,payload)=>{const record={seq:++seq,previous_sha256:previous,recorded_at:at,type,payload};
    previous=hash(JSON.stringify(record));records.push({...record,sha256:previous});return seq;};
  add('started',plan);const engine=new BoardAlerts();
  for(let i=0;i<2;i++){
    const input=raw();input.depth[0][2]=`100\n${i?1000000:59000000}`;input.volume='حجم معاملات\n'+(i?'4,000,000':'1,000,000');
    const received=tehranNow(new Date(new Date(at).getTime()+i*500));
    const observation=add('dom_observation',{raw:input,received_at:received,monotonic_ms:i*500});
    const q=parseBoard(input,received),result=engine.observe(q,i*500),stored=structuredClone(result);
    if(i&&badResult)stored.events[0].queue_now=2;
    add('evaluation',{observation_seq:observation,key:q.key,monotonic_ms:i*500,session_open:true,result:stored});
    for(const event of result.events)add('alert',event);
  }add('stopped',{});
  await fs.writeFile(path.join(dir,'events.jsonl'),records.map(r=>JSON.stringify(r)).join('\n')+'\n');return dir;
}
async function removeFixture(dir) {
  assert.equal(path.dirname(path.resolve(dir)),path.resolve(os.tmpdir()));
  assert(path.basename(dir).startsWith('brs-board-test-'));
  await fs.rm(dir,{recursive:true,force:true});
}
test('قفل گذرای ویندوز نوشتن اتمی را به فایل ناقص تبدیل نمی‌کند',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'brs-board-test-'));let calls=0;
  try{
    const target=path.join(dir,'status.json');await fs.writeFile(target,'{"old":true}');
    await atomic(target,{fresh:true},async(a,b)=>{if(++calls<3){const e=new Error('locked');e.code='EPERM';throw e;}await fs.rename(a,b);});
    assert.equal(calls,3);assert.deepEqual(JSON.parse(await fs.readFile(target,'utf8')),{fresh:true});
    await assert.rejects(atomic(target,{bad:true},async()=>{const e=new Error('full');e.code='ENOSPC';throw e;}),/full/);
    assert.deepEqual(JSON.parse(await fs.readFile(target,'utf8')),{fresh:true});
  }finally{await removeFixture(dir);}
});
test('دفتر آفلاین با بایت/کد محفوظ و بازاجرای هشدار بررسی می‌شود',async()=>{
  const dir=await archiveFixture();try{const r=await reviewArchive(dir);assert.equal(r.alerts,3);assert.equal(r.complete,true);assert.equal(r.accepted_for_signals,false);}
  finally{await removeFixture(dir);}
});
test('زنجیره معتبر با نتیجه حسابی غلط پذیرفته نمی‌شود',async()=>{
  const dir=await archiveFixture({badResult:true});try{await assert.rejects(reviewArchive(dir),/بازحساب/);}
  finally{await removeFixture(dir);}
});
test('ویرایش دفتر و کد ناشناخته بدون اجرا رد می‌شوند',async()=>{
  const dir=await archiveFixture();try{
    const file=path.join(dir,'events.jsonl');await fs.writeFile(file,(await fs.readFile(file,'utf8')).replace('59000000','58000000'));
    await assert.rejects(reviewArchive(dir),/زنجیره/);
    await fs.writeFile(path.join(dir,'code/core/board_alerts.mjs'),'throw Error("must never run")');
    await assert.rejects(reviewArchive(dir),/کد محفوظ/);
  }finally{await removeFixture(dir);}
});
