/** فقط مرورگر ایزوله و داده آزمون در tests؛ هیچ ارتباط واقعی با کارگزاری ندارد. */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import {pathToFileURL} from 'node:url';
import {readBoardDom,parseBoard,tehranNow} from '../data_collector/farabi_board.mjs';
import {defaults} from '../core/board_alerts.mjs';
const {chromium}=await import(pathToFileURL(process.env.BRS_PLAYWRIGHT_MODULE).href);
const browser=await chromium.launch({headless:true});
let server,unexpected=0;
try {
  const page=await browser.newPage();
  const now=tehranNow(),parts=new Intl.DateTimeFormat('en-US-u-ca-persian',{timeZone:'Asia/Tehran',year:'numeric',month:'numeric',day:'numeric'}).formatToParts(new Date());
  const date=Object.fromEntries(parts.map(p=>[p.type,p.value]));
  const fixture=`<!doctype html><meta charset="utf-8"><title>آزمون 100 ▼ 2.91%</title><style>body>div>span{display:block}</style>
  <input placeholder="جست‌و‌جوی نماد یا شرکت" value="آزمون - شرکت آزمون - فرابورس">
  <div><span>حجم معاملات</span><span>1,000,000</span></div>
  <div><span>زمان آخرین معامله</span><span>${date.year}/${date.month}/${date.day} ${now.slice(11,19)}</span></div>
  <div><span>100</span><span>بازه مجاز روزانه</span><span>110</span></div>
  <table><thead><tr><th>تعداد</th><th>حجم قیمت</th><th>قیمت حجم</th><th>تعداد</th></tr></thead><tbody>
  ${Array.from({length:5},(_,i)=>`<tr><td>1</td><td>${99-i}<br>10</td><td>${100+i}<br>1000</td><td>1</td></tr>`).join('')}</tbody></table>
  <table><thead><tr><th>دارایی خصوصی</th></tr></thead><tbody><tr><td>DO_NOT_CAPTURE</td></tr></tbody></table>`;
  await page.route('**/*',async route=>{
    if(['https://farabixo.irfarabi.com/?isin=TEST00000001','https://farabixo.irfarabi.com/?isin=TEST00000001&code=TEST_ONLY'].includes(route.request().url()))await route.fulfill({contentType:'text/html',body:fixture});
    else {unexpected++;await route.abort();}
  });
  await page.goto('https://farabixo.irfarabi.com/?isin=TEST00000001');
  const raw=await page.evaluate(readBoardDom);assert(!JSON.stringify(raw).includes('DO_NOT_CAPTURE'));
  assert.equal(parseBoard(raw,tehranNow()).sell_queue,1000);
  await page.goto('https://farabixo.irfarabi.com/?isin=TEST00000001&code=TEST_ONLY');
  assert.deepEqual(await page.evaluate(readBoardDom),{not_a_board:true});
  await page.close();
  const html=await fs.readFile(new URL('../dashboard/board_watch.html',import.meta.url));
  let rule={...defaults},posted=null,overrides={};
  const status=()=>({version:'visible-board-alerts-v1',running:true,updated_at:tehranNow(),pending_login_or_board:posted===null,
    rule,rule_overrides:overrides,boards:posted===null?[]:[{key:'TEST00000001',symbol:'آزمون',last_price:100,volume:1000000,
      sell_queue:1000,buy_queue:null,ok:true,session_open:true,book_changed_at:tehranNow()}],alerts:[],poll_gap_ms:{p95:501}});
  server=http.createServer(async(req,res)=>{
    if(req.url==='/'){res.setHeader('Content-Type','text/html; charset=utf-8');res.end(html);}
    else if(req.url==='/status'){res.setHeader('Content-Type','application/json');res.end(JSON.stringify(status()));}
    else if(req.url==='/rule'){let text='';for await(const c of req)text+=c;posted=JSON.parse(text);rule=posted;res.setHeader('Content-Type','application/json');res.end('{"ok":true}');}
    else if(req.url==='/rule-for'){let text='';for await(const c of req)text+=c;posted=JSON.parse(text);if(posted.rule===null)delete overrides[posted.key];else overrides[posted.key]=posted.rule;res.setHeader('Content-Type','application/json');res.end('{"ok":true}');}
    else if(req.url==='/favicon.ico'){res.writeHead(204);res.end();}
    else{res.writeHead(404);res.end();}
  });await new Promise(r=>server.listen(0,'127.0.0.1',r));
  const ui=await browser.newPage({viewport:{width:1280,height:1000}}),errors=[];
  ui.on('pageerror',e=>errors.push(e.message));ui.on('console',m=>{if(m.type()==='error')errors.push(m.text());});
  await ui.goto(`http://127.0.0.1:${server.address().port}`);
  await ui.locator('#health').filter({hasText:'منتظر ورود'}).waitFor();
  await ui.locator('#queue_start').fill('50000000');await ui.locator('#rule button[type="submit"],#rule button:not([type])').click();
  await ui.locator('#message').filter({hasText:'شرایط ثبت شد'}).waitFor();assert.equal(posted.queue_start,50000000);
  await ui.locator('#scope option[value="TEST00000001"]').waitFor({state:'attached'});
  await ui.locator('#scope').selectOption('TEST00000001');await ui.locator('#queue_start').fill('60000000');
  await ui.locator('#rule button:not([type])').click();
  await ui.locator('#message').filter({hasText:'شرایط ثبت شد'}).waitFor();
  assert.equal(posted.key,'TEST00000001');assert.equal(posted.rule.queue_start,60000000);
  await ui.locator('#reset-rule').click();await ui.locator('#message').filter({hasText:'تنظیم اختصاصی حذف شد'}).waitFor();
  assert.deepEqual(overrides,{});
  await ui.locator('#sound').click();await ui.getByText('صدا فعال است — آزمون دوباره',{exact:true}).waitFor();
  await ui.setViewportSize({width:390,height:844});assert(await ui.locator('h1').isVisible());
  assert.equal(await ui.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  assert.deepEqual(errors,[]);assert.equal(unexpected,0);
  console.log('DOM scope, real parser, settings interaction, audio activation and mobile overflow checks passed; no brokerage network.');
}finally{await browser.close();if(server)await new Promise(r=>server.close(r));}
