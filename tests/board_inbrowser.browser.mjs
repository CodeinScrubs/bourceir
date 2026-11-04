/** Isolated browser fixture: no connection to a broker or real account. */
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
import {build} from '../scripts/build_board_inbrowser.mjs';

const {chromium}=await import(pathToFileURL(process.env.BRS_PLAYWRIGHT_MODULE).href);
const browser=await chromium.launch({headless:true});
let unexpected=0;
try{
  const page=await browser.newPage({viewport:{width:1280,height:900}});
  // A fixed market-session clock exists only in this intercepted test page.
  await page.addInitScript(()=>{const NativeDate=Date,base=NativeDate.parse('2026-10-20T10:00:00+03:30'),start=performance.now();
    globalThis.Date=class extends NativeDate{constructor(...args){super(...(args.length?args:[base+performance.now()-start]));}
      static now(){return base+performance.now()-start;}};});
  const stamp='1405/7/28 10:00:00';
  const fixture=`<!doctype html><html><head><meta charset="utf-8"><title>آزمون 100 ▼ 2.91%</title><style>body>div>span{display:block}</style></head><body>
    <input placeholder="جست‌و‌جوی نماد یا شرکت" value="آزمون - شرکت آزمون - فرابورس">
    <div><span>حجم معاملات</span><span id="volume">1,000,000</span></div>
    <div><span>زمان آخرین معامله</span><span id="trade-time">${stamp}</span></div>
    <div><span>100</span><span>بازه مجاز روزانه</span><span>110</span></div>
    <table><thead><tr><th>تعداد</th><th>حجم قیمت</th><th>قیمت حجم</th><th>تعداد</th></tr></thead><tbody>
    <tr><td>1</td><td>100<br>1000</td><td id="ask">100<br>20,000,000</td><td>1</td></tr>
    ${Array.from({length:4},(_,i)=>`<tr><td>0</td><td>0<br>0</td><td>${101+i}<br>1000</td><td>1</td></tr>`).join('')}
    </tbody></table><div id="account">DO_NOT_CAPTURE_ACCOUNT</div></body></html>`;
  await page.route('**/*',async route=>{
    if(route.request().url()==='https://farabixo.irfarabi.com/?isin=TEST00000001')await route.fulfill({contentType:'text/html',body:fixture});
    else {unexpected++;await route.abort();}
  });
  await page.goto('https://farabixo.irfarabi.com/?isin=TEST00000001');
  const bundle=await build();
  await page.evaluate(()=>{const old=document.createElement('div');old.setAttribute('data-brs-board-monitor','old');document.body.append(old);});
  await assert.rejects(page.evaluate(bundle.expression),/یک پایش تابلو باز است/);
  await page.locator('[data-brs-board-monitor="old"]').evaluate(el=>el.remove());
  const installed=await page.evaluate(bundle.expression);assert.equal(installed.status,'installed');
  const host=page.locator('[data-brs-board-monitor="visible-pressure-v5"]');
  try{await host.locator('#health').filter({hasText:'خواندن تابلو برقرار'}).waitFor({timeout:4000});}
  catch(e){console.error('fixture monitor:',await host.locator('#health').textContent());throw e;}
  assert.equal(await host.locator('#symbol').innerText(),'آزمون');
  assert.equal((await page.evaluate(bundle.expression)).status,'already_installed');
  assert.equal(await host.count(),1);
  assert.equal(await host.locator('#min_queue').inputValue(),'1000000');
  assert(!(await host.innerText()).includes('DO_NOT_CAPTURE_ACCOUNT'));
  await host.locator('#collapse').click();assert.equal(await host.locator('#body').isVisible(),false);
  await host.locator('#collapse').click();assert.equal(await host.locator('#body').isVisible(),true);
  await page.evaluate(()=>{document.getElementById('volume').textContent='1,300,000';
    document.getElementById('ask').innerHTML='100<br>12,000,000';});
  await host.locator('#events').filter({hasText:'کاهش صف فروش همراه حجم معاملات'}).waitFor({timeout:4000});
  const state=await page.evaluate(()=>window.__BRS_VISIBLE_PRESSURE_MONITOR_V5__.state);
  assert.equal(state.symbol,'آزمون');assert.equal(state.last_sell_queue,12000000);
  assert(state.events>=2);assert.equal(state.accepted_for_signals,false);assert.equal(state.order_actions,false);
  const snapshot=await page.evaluate(()=>window.__BRS_VISIBLE_PRESSURE_MONITOR_V5__.snapshot());
  assert(snapshot.events.some(event=>event.kind==='sell_queue_weakening_with_trades'));
  await page.evaluate(()=>{document.getElementById('trade-time').textContent='1405/7/28 09:59:00';});
  await host.locator('#health').filter({hasText:'آخرین معامله قدیمی است'}).waitFor({timeout:4000});
  await page.evaluate(()=>{document.getElementById('volume').textContent='-';});
  await host.locator('#health').filter({hasText:'داده قابل‌اعتماد نیست'}).waitFor({timeout:4000});
  assert.equal(await host.locator('#quote').textContent(),'');
  await page.evaluate(()=>{document.getElementById('volume').textContent='1,300,000';
    document.getElementById('trade-time').textContent='1405/7/28 10:00:00';});
  await host.locator('#health').filter({hasText:'خواندن تابلو برقرار'}).waitFor({timeout:4000});
  await host.locator('summary').click();await host.locator('#min_queue').fill('2500000');
  await host.locator('#save').click();await host.locator('#settings').filter({hasText:'شرایط اعمال شد'}).waitFor();
  await host.locator('#close').click();assert.equal(await host.count(),0);
  assert.equal(await page.evaluate(()=>typeof window.__BRS_VISIBLE_PRESSURE_MONITOR_V5__),'undefined');
  assert.equal(unexpected,0);
  console.log('IAB monitor: DOM-only scope, event, settings, deduplication and cleanup passed in an isolated fixture.');
}finally{await browser.close();}
