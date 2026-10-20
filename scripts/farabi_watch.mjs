/** پایش مستقل DOM با نشست موقت؛ بدون API، اطلاعات حساب، ذخیره نشست یا سفارش. */
import fs from 'node:fs/promises';
import path from 'node:path';
import http from 'node:http';
import crypto from 'node:crypto';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {BoardAlerts,defaults,validateRule,version} from '../core/board_alerts.mjs';
import {origin,readBoardDom,parseBoard,tehranNow} from '../data_collector/farabi_board.mjs';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const privateRoot=path.resolve(root,'data/private');
export async function atomic(file,value,rename=fs.rename) {
  const temp=file+'.tmp';await fs.writeFile(temp,JSON.stringify(value,null,2),'utf8');
  // خواننده کوتاه‌عمر ویندوز می‌تواند rename را موقتاً قفل کند؛ جایگزینی غیراتمی مجاز نیست.
  for(const delay of [0,10,30,60,120,240]){
    if(delay)await new Promise(r=>setTimeout(r,delay));
    try{await rename(temp,file);return;}
    catch(e){if(!['EPERM','EBUSY','EACCES'].includes(e.code)||delay===240)throw e;}
  }
}
export async function main() {
  const modulePath=process.env.BRS_PLAYWRIGHT_MODULE;
  const {chromium}=modulePath?await import(pathToFileURL(modulePath).href):await import('playwright');
  const out=path.resolve(process.argv[2]||path.join(privateRoot,'farabi-board','watch-'+tehranNow().replace(/[:.]/g,'-')));
  if(!out.startsWith(privateRoot+path.sep))throw new Error('پوشه پایش باید زیر data/private باشد');
  try {
    const previous=JSON.parse(await fs.readFile(path.join(privateRoot,'farabi-board','current.json'),'utf8'));
    const u=new URL(previous.url);
    if(u.origin===`http://127.0.0.1:${u.port}`&&u.pathname==='/'&&!u.search&&!u.hash){
      const r=await fetch(u.origin+'/status',{signal:AbortSignal.timeout(800)}),s=await r.json();
      if(s.version===version&&s.running&&s.pid===previous.pid&&s.started_at===previous.started_at){console.log('پایش از قبل روشن است: '+u.origin);return;}
    }
  }catch{/* نبودن یا توقف سرویس قبلی، اجازه کشتن PID قدیمی نیست. */}
  await fs.mkdir(out,{recursive:true});
  let saved={rule:validateRule(defaults),overrides:{}};
  try{
    const parsed=JSON.parse(await fs.readFile(path.join(privateRoot,'farabi-board','rules.json'),'utf8'));
    if(!parsed||Object.keys(parsed).sort().join(',')!=='overrides,rule'||!parsed.rule||typeof parsed.rule!=='object'
      ||Object.keys(parsed.rule).sort().join(',')!==Object.keys(defaults).sort().join(',')
      ||!parsed.overrides||Array.isArray(parsed.overrides)||typeof parsed.overrides!=='object'||Object.keys(parsed.overrides).length>200)
      throw new Error('تنظیمات محفوظ نمادها نامعتبر است');
    saved={rule:validateRule(parsed.rule),overrides:{}};
    for(const [key,value]of Object.entries(parsed.overrides)){
      if(!/^[A-Z0-9]{12}$/.test(key))throw new Error('شناسه تنظیمات محفوظ نامعتبر است');saved.overrides[key]=validateRule(value);
    }
  }catch(e){if(e.code!=='ENOENT')throw e;}
  const codeFiles=['core/board_alerts.mjs','data_collector/farabi_board.mjs','scripts/farabi_watch.mjs','dashboard/board_watch.html'];
  const code={};for(const file of codeFiles){const bytes=await fs.readFile(path.join(root,file));code[file]=crypto.createHash('sha256').update(bytes).digest('hex');
    const target=path.join(out,'code',file);await fs.mkdir(path.dirname(target),{recursive:true});await fs.writeFile(target,bytes,{flag:'wx'});}
  const plan={version,started_at:tehranNow(),interval_ms:500,max_tabs:10,code,rule:saved.rule,rule_overrides:saved.overrides,
    source:'farabi-visible-dom',independent_book_clock_verified:false,accepted_for_signals:false};
  await fs.writeFile(path.join(out,'plan.json'),JSON.stringify(plan,null,2),{flag:'wx'});
  let seq=0,previous='0'.repeat(64),rule=plan.rule,engine=new BoardAlerts(rule);
  for(const [key,value]of Object.entries(saved.overrides))engine.configureForKey(key,value);
  async function journal(type,payload) {
    const record={seq:++seq,previous_sha256:previous,recorded_at:tehranNow(),type,payload};
    const encoded=JSON.stringify(record);previous=crypto.createHash('sha256').update(encoded).digest('hex');
    await fs.appendFile(path.join(out,'events.jsonl'),JSON.stringify({...record,sha256:previous})+'\n');
    return seq;
  }
  async function invalidate(key,reason){engine.invalidate(key);await journal('baseline_invalidated',{key,reason});}
  async function saveRules(){await fs.mkdir(path.join(privateRoot,'farabi-board'),{recursive:true});
    await atomic(path.join(privateRoot,'farabi-board','rules.json'),{rule,overrides:Object.fromEntries(engine.overrides)});}
  const browser=await chromium.launch({headless:false});
  let context;
  try{context=await browser.newContext({acceptDownloads:false,timezoneId:'Asia/Tehran'});}
  catch(e){await browser.close().catch(()=>{});throw e;}
  const rows=new Map(),pageKeys=new Map(),identities=new Map(),symbolKeys=new Map(),durations=[],gaps=[],pendingReads=new Map();
  let status={version,pid:process.pid,running:true,started_at:plan.started_at,updated_at:tehranNow(),rule,boards:[],alerts:[],
    rule_overrides:saved.overrides,archive:out,independent_book_clock_verified:false,exchange_latency_seconds:null,accepted_for_signals:false};
  let server,stopped=false,localUrl,cycles=0,previousPoll=null;
  const body=await fs.readFile(path.join(root,'dashboard/board_watch.html'));
  let mutation=Promise.resolve();
  function serialize(fn){const p=mutation.then(fn);mutation=p.catch(()=>{});return p;}
  async function addBoard(url) {
    const u=new URL(url);
    if(u.origin!==origin||u.pathname!=='/'||[...u.searchParams.keys()].some(k=>k!=='isin')||u.searchParams.getAll('isin').length!==1
      ||!/^[A-Z0-9]{12}$/.test(u.searchParams.get('isin')||''))throw new Error('لینک تابلو را از خود فارابی کپی کنید');
    if(context.pages().filter(p=>p.url().startsWith(origin+'/')).length>=10)throw new Error('حداکثر ده تابلو در هر نشست');
    const page=await context.newPage();await page.goto(u.href,{waitUntil:'domcontentloaded',timeout:15000});
  }
  try {
  server=http.createServer(async(req,res)=>{
    const allowedHost=`127.0.0.1:${server.address().port}`;
    if(req.headers.host!==allowedHost){res.writeHead(403);res.end();return;}
    res.setHeader('Cache-Control','no-store');res.setHeader('X-Content-Type-Options','nosniff');
    res.setHeader('Content-Security-Policy',"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'none'; frame-ancestors 'none'; form-action 'self'");
    try {
      if(req.method==='GET'&&req.url==='/'){res.setHeader('Content-Type','text/html; charset=utf-8');res.end(body);return;}
      if(req.method==='GET'&&req.url==='/status'){res.setHeader('Content-Type','application/json; charset=utf-8');res.end(JSON.stringify(status));return;}
      if(req.method==='POST'&&['/rule','/rule-for','/watch','/stop'].includes(req.url)) {
        if(req.headers.origin!==localUrl||req.headers['content-type']!=='application/json')throw new Error('درخواست فقط از پنل محلی مجاز است');
        let bytes=0,text='';for await(const chunk of req){bytes+=chunk.length;if(bytes>8192)throw new Error('درخواست بزرگ است');text+=chunk;}
        const input=JSON.parse(text);
        if(req.url==='/stop')stopped=true;
        else if(req.url==='/rule')await serialize(async()=>{
          const next=validateRule(input);await journal('rule_changed',{rule:next,baselines_reset:true});rule=next;engine.configure(next);status.rule=next;await saveRules();
        });
        else if(req.url==='/rule-for')await serialize(async()=>{
          if(!input||Object.keys(input).sort().join(',')!=='key,rule'||!identities.has(input.key))throw new Error('ابتدا تابلوی این نماد باید دریافت شود');
          if(!engine.overrides.has(input.key)&&input.rule!==null&&engine.overrides.size>=200)throw new Error('حداکثر دویست تنظیم اختصاصی محفوظ مجاز است');
          const next=input.rule===null?null:validateRule(input.rule);
          await journal('rule_changed',{key:input.key,rule:next,baselines_reset:true});engine.configureForKey(input.key,next);
          status.rule_overrides=Object.fromEntries(engine.overrides);await saveRules();
        });
        else {
          if(!input||Object.keys(input).join(',')!=='url'||typeof input.url!=='string')throw new Error('یک لینک تابلو لازم است');
          // بازکردن فقط لینک عمومی تابلو؛ هرگز پارامتر سفارش یا مسیر حساب مجاز نیست.
          await addBoard(input.url);
        }
        res.setHeader('Content-Type','application/json; charset=utf-8');res.end(JSON.stringify({ok:true}));return;
      }
      res.writeHead(404);res.end();
    }catch(e){res.writeHead(400,{'Content-Type':'application/json; charset=utf-8'});res.end(JSON.stringify({error:e.message}));}
  });
  await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve);});
  localUrl=`http://127.0.0.1:${server.address().port}`;status.url=localUrl;
  const startPage=await context.newPage();await startPage.goto(origin,{waitUntil:'domcontentloaded',timeout:30000});
  const panel=await context.newPage();await panel.goto(localUrl);
  await journal('started',plan);
  await fs.mkdir(path.join(privateRoot,'farabi-board'),{recursive:true});
  await atomic(path.join(privateRoot,'farabi-board','current.json'),{pid:process.pid,started_at:plan.started_at,url:localUrl,archive:out,root});
  console.log('پایش فقط‌خواندنی آماده است. در مرورگر موقت وارد فارابی شوید و تابلوهای دلخواه را باز کنید.');
  console.log(localUrl);
  const stop=()=>{stopped=true;};process.on('SIGINT',stop);process.on('SIGTERM',stop);browser.on('disconnected',stop);
  async function poll() {
    const begin=performance.now(),at=tehranNow();
    if(previousPoll!==null){gaps.push(begin-previousPoll);if(gaps.length>2000)gaps.shift();}previousPoll=begin;
    const pages=context.pages().filter(p=>p.url().startsWith(origin+'/')).slice(0,10);
    const reads=await Promise.allSettled(pages.map(async page=>{
      if(pendingReads.has(page))throw new Error('خواندن قبلی این تب هنوز تمام نشده است');
      const t=performance.now(),job=page.evaluate(readBoardDom).then(raw=>({page,raw,read_ms:performance.now()-t,
        received_at:tehranNow(),monotonic_ms:performance.now()}));
      pendingReads.set(page,job);job.then(()=>pendingReads.delete(page),()=>pendingReads.delete(page));
      let timer;try{return await Promise.race([job,new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('DOM read timeout')),1500);})]);}
      finally{clearTimeout(timer);}
    }));
    const seen=new Set();
    const keys=reads.filter(r=>r.status==='fulfilled').map(r=>{try{return new URL(r.value.raw.url).searchParams.get('isin');}catch{return null;}});
    const duplicates=new Set(keys.filter((key,i)=>key&&keys.indexOf(key)!==i));
    for(let i=0;i<reads.length;i++) {
      const result=reads[i],page=pages[i],oldKey=pageKeys.get(page);
      if(result.status==='rejected') {
        status.last_error={at,reason:'خواندن DOM ناموفق بود'};
        if(oldKey){await invalidate(oldKey,'read_error');seen.add(oldKey);rows.set(oldKey,{...rows.get(oldKey),ok:false,error:'خواندن DOM ناموفق بود',observed_at:at});}
        await journal('read_error',{page_index:i,at,error_type:result.reason?.name||'Error'});continue;
      }
      const {raw,read_ms,received_at,monotonic_ms}=result.value;
      const observationSeq=await journal('dom_observation',{page_index:i,received_at,read_ms,monotonic_ms,raw});
      let q;
      try {
        q=parseBoard(raw,received_at);
        if(duplicates.has(q.key))throw new Error('دو تب یک نماد باز است؛ یکی را ببندید');
        if(identities.has(q.key)&&identities.get(q.key)!==q.symbol||symbolKeys.has(q.symbol)&&symbolKeys.get(q.symbol)!==q.key)
          throw new Error('هویت نماد/لینک در این نشست تعارض دارد؛ پس از تکمیل بارگذاری بررسی و پایش را دوباره راه‌اندازی کنید');
        identities.set(q.key,q.symbol);symbolKeys.set(q.symbol,q.key);
        if(oldKey&&oldKey!==q.key){await invalidate(oldKey,'navigation');rows.delete(oldKey);}
        pageKeys.set(page,q.key);seen.add(q.key);
        const previous=rows.get(q.key),book=JSON.stringify(q.rows),bookChanged=!previous||previous.book_hash!==book;
        const readGap=previous?(new Date(q.observed_at)-new Date(previous.observed_at))/1000:null;
        const time=q.observed_at.slice(11,19),weekday=new Date(q.market_day+'T12:00:00+03:30').getUTCDay();
        const sessionOpen=[6,0,1,2,3].includes(weekday)&&time>='09:00:00'&&time<'12:30:00';
        let outcome={events:[],reset:true,baseline_reason:'outside_regular_session'};
        if(sessionOpen)outcome=engine.observe(q,monotonic_ms);else await invalidate(q.key,'outside_regular_session');
        await journal('evaluation',{observation_seq:observationSeq,key:q.key,monotonic_ms,session_open:sessionOpen,result:outcome});
        rows.set(q.key,{...q,ok:true,book_hash:book,book_changed_at:bookChanged?q.observed_at:previous.book_changed_at,
          book_change_count:(previous?.book_change_count||0)+(bookChanged?1:0),read_ms,read_gap_seconds:readGap,
          session_open:sessionOpen,baseline_reason:outcome.baseline_reason});
        for(const event of outcome.events){await journal('alert',event);status.alerts.push({...event,id:seq});if(status.alerts.length>100)status.alerts.shift();console.log('\x07'+event.kind+' '+event.symbol);}
      }catch(e){
        status.last_error={at:received_at,reason:e.message};
        const key=q?.key||oldKey;
        if(key){await invalidate(key,'parse_rejected');seen.add(key);rows.set(key,{...(rows.get(key)||{}),ok:false,error:e.message,observed_at:received_at});}
        await journal('parse_rejected',{page_index:i,reason:e.message});
      }
    }
    for(const [key]of rows)if(!seen.has(key)){await invalidate(key,'tab_closed_or_not_observed');rows.delete(key);}
    for(const page of pageKeys.keys())if(page.isClosed())pageKeys.delete(page);
    durations.push(performance.now()-begin);if(durations.length>2000)durations.shift();
    const summary=arr=>{if(!arr.length)return null;const sorted=[...arr].sort((a,b)=>a-b);return {p50:sorted[Math.floor((sorted.length-1)*.5)],p95:sorted[Math.floor((sorted.length-1)*.95)],max:sorted.at(-1)};};
    status={...status,updated_at:tehranNow(),cycles:++cycles,rule,rule_overrides:Object.fromEntries(engine.overrides),boards:[...rows.values()].map(({book_hash,...q})=>q),
      valid_board_count:[...rows.values()].filter(q=>q.ok).length,pending_login_or_board:![...rows.values()].some(q=>q.ok),
      extra_tabs_ignored:Math.max(0,context.pages().filter(p=>p.url().startsWith(origin+'/')).length-10),
      cycle_ms:summary(durations),poll_gap_ms:summary(gaps)};
    await atomic(path.join(out,'status.json'),status);
  }
    const lifetimeStart=performance.now();
    while(!stopped&&performance.now()-lifetimeStart<12*3600000){const t=performance.now();await serialize(poll);await new Promise(r=>setTimeout(r,Math.max(10,500-(performance.now()-t))));}
  } finally {
    status={...status,running:false,updated_at:tehranNow()};
    try{await journal('stopped',{});await atomic(path.join(out,'status.json'),status);}
    finally{await browser.close().catch(()=>{});if(server?.listening)await new Promise(r=>server.close(r));}
  }
}
if(process.argv[1]&&import.meta.url===pathToFileURL(path.resolve(process.argv[1])).href)
  main().catch(e=>{console.error('پایش متوقف شد:',e.message);process.exitCode=1;});
