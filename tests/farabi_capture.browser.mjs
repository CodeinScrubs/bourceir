/** آزمون DOM ساختگی با Chromium؛ هیچ درخواست واقعی به کارگزاری ارسال نمی‌شود. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import {pathToFileURL} from 'node:url';
import {captureConnectedPortfolio, capturePortfolio, displayText, origin, portfolioHeaders} from '../scripts/farabi_capture.mjs';
import {captureIndustries, industryCategory, industrySymbolIsin, peerGroup, peerSymbolIsin} from '../scripts/farabi_industries.mjs';

const {chromium} = await import(pathToFileURL(process.env.BRS_PLAYWRIGHT_MODULE).href);
const browser = await chromium.launch({headless:true});
function fixture({localized=false, rounded=false, changed=false, bonus=false}={}) {
  const data = JSON.stringify({localized,rounded,changed,bonus,headers:portfolioHeaders});
  return `<!doctype html><meta charset="utf-8"><title>Isolated BRS capture test</title>
    <button data-gid="portfolio_home_assetsTabSection_showPortfolioVisualizationButton">Summary</button>
    <section id="modal" hidden></section>
    <div id="viewport" class="overflow-auto border-neutral-tertiary" tabindex="0" style="height:192px;overflow:auto;width:900px"><div style="height:1952px;position:relative">
    <table style="position:absolute;border-collapse:collapse;table-layout:fixed;width:880px"><thead></thead><tbody></tbody></table></div></div>
    <style>td,th{height:32px;box-sizing:border-box;padding:0;border:0;white-space:pre-line}</style>
    <script>
      const options=${data};
      const digit = text => options.localized ? String(text).replace(/[0-9]/g,c=>'۰۱۲۳۴۵۶۷۸۹'[c]).replace(/,/g,'٬').replace(/%/g,'٪') : String(text);
      const field = (label,values) => '<div><span>'+label+'</span>'+values.map(v=>'<span>'+digit(v)+'</span>').join('')+'</div>';
      const modal=document.querySelector('#modal'), viewport=document.querySelector('#viewport'), table=document.querySelector('table');
      let power='10', wasDown=false;
      function showModal(){modal.innerHTML=field('تعداد سهام',['60'])+field('ارزش سبد سهام',['6,000'])+field('جمع مبلغ خرید',['0'])+
        field('بازده',['(-∞%)','6,000'])+field('خالص دارایی',[options.rounded?'6,011':'6,010','ریال'])+field('مانده مشتری',['10','ریال'])+
        field('قدرت خرید',[power,'ریال'])+field('بلوکه شده',['0','ریال'])+'<button id="close">بستن</button>';
        modal.hidden=false;document.querySelector('#close').onclick=()=>{modal.hidden=true;modal.innerHTML='';};}
      document.querySelector('button').onclick=showModal;
      function render(){const start=Math.min(52,Math.floor(viewport.scrollTop/32));table.style.top=(start*32)+'px';
        table.querySelector('thead').innerHTML='<tr>'+options.headers.map(h=>'<th>'+h+'</th>').join('')+'</tr>';
        table.querySelector('tbody').innerHTML=Array.from({length:8},(_,i)=>{const n=start+i;
          const values=['T'+n,'1','0','0','0',n%2?'100 (0%)':'(0%) 100','100',n%2?'+100 (+∞%)':'(+∞%) +100'];
          if(options.bonus&&n===59){
            values[0]+='\\nسهام جایزه';values[1]+='\\n2';values[2]+='\\n-';values[3]='30';values[4]+='\\n-';
            values[6]+='\\n200';values[7]='(+∞%)\\n+100\\n(+1,234.50%)\\n+200';
          }
          return '<tr>'+values.map(v=>'<td>'+digit(v)+'</td>').join('')+'</tr>';}).join('');}
      viewport.addEventListener('scroll',()=>{render();if(viewport.scrollTop===0)wasDown=true;if(options.changed&&wasDown&&viewport.scrollTop>200)power='9';});
      viewport.scrollTop=1000;render();
    </script>`;
}

const industryFixture = `<!doctype html><meta charset="utf-8"><title>Industries fixture</title>
<button role="tab" onclick="panel.hidden=false">صنایع</button>
<section id="panel" role="tabpanel" aria-label="صنایع" hidden>
  <input placeholder="انتخاب کنید" onclick="options.hidden=false">
  <div id="options" role="listbox" hidden><div role="option" aria-disabled="true">صندوق‌ها</div>
    <div role="option" onclick="choose()">درآمد ثابت</div></div>
  <div id="status"></div><div id="marketTable"></div>
</section>
<input id="search" placeholder="جست‌و‌جوی نماد یا شرکت">
<button onclick="showPeers()">هم‌گروه</button><div id="peers"></div>
<script>
  const seed = 'IRO3TEST0001';
  let tries = 0;
  function choose(){
    options.hidden=true; panel.querySelector('input').value=' درآمد ثابت ';
    if (!tries) document.getElementById('status').innerHTML='اطلاعاتی دریافت نشد<button onclick="retry()">تلاش دوباره</button>';
  }
  function retry(){
    tries++; document.getElementById('status').textContent='';
    marketTable.innerHTML='<table><thead><tr><th>نام نماد</th><th>قیمت</th><th>%پایانی</th></tr></thead>'+
      '<tbody><tr onclick="board()"><td>نماد آزمایشی</td><td>100</td><td>0</td></tr></tbody></table>';
  }
  function board(){history.replaceState(null,'','/?isin='+seed); search.value='نماد آزمایشی - نام شرکت';}
  function showPeers(){
    peers.innerHTML='<table><thead><tr><th>نماد</th><th>لحظه‌ای</th><th>پایانی</th>'+'<th>۱</th>'.repeat(7)+'</tr></thead><tbody>'+
      ['نماد آزمایشی','هم‌گروه آزمایشی'].map(s=>'<tr><td class="index">'+s+'</td>'+'<td>۱</td>'.repeat(9)+'</tr>').join('')+'</tbody></table>';
    peers.querySelector('tbody tr:last-child').onclick=()=>{
      history.replaceState(null,'','/?isin=IRO3PEER0001');
      setTimeout(()=>search.value='هم‌گروه آزمایشی - نام شرکت',180);
    };
  }
</script>`;

let unexpectedRequests=0, cases=0;
try {
  for (const options of [{}, {localized:true,rounded:true}, {localized:true,changed:true},
    {localized:true,bonus:true,connected:true}]) {
    const context=await browser.newContext({viewport:{width:1200,height:900}});
    const page=await context.newPage();
    page.setDefaultTimeout(6000);
    await page.route('**/*', async route => {
      if (route.request().url() !== origin+'/') {unexpectedRequests++; await route.abort(); return;}
      await route.fulfill({contentType:'text/html',body:fixture(options)});
    });
    await page.goto(origin+'/');
    const tab={url:async()=>page.url(),playwright:page,scroll:async([x,y],direction,pages)=>{
      await page.mouse.move(x,y);await page.mouse.wheel(0,(direction==='up'?-1:1)*pages*900);await page.waitForTimeout(100);
    }};
    const connectedTab = {url:tab.url, playwright:{evaluate:reader => page.evaluate(reader),
      locator:(...args) => page.locator(...args), getByText:(...args) => page.getByText(...args), getByRole:(...args) => page.getByRole(...args)}};
    if (options.changed) await assert.rejects(capturePortfolio(tab),/changed during capture/);
    else {
      const result=options.connected ? await captureConnectedPortfolio(connectedTab) : await capturePortfolio(tab);
      assert.equal(result.rows.length,60);assert.equal(displayText(result.rows[0][0]),'T0');assert.equal(result.summary.broker_return_pct,null);
      assert.equal(result.summary.net_assets_rial,options.rounded?6011:6010);
      const python=[process.env.BRS_TEST_PYTHON,'.venv/Scripts/python.exe','venv/Scripts/python.exe'].find(p=>p&&fs.existsSync(p)) || 'python';
      const parserCheck = "import sys; from data_collector.farabi import parse_portfolio; p=parse_portfolio(sys.stdin.buffer.read()); assert len(p['rows'])==60 and p['summary']['broker_return_pct'] is None";
      const bonusCheck = "; b=p['rows'][-1]; assert b['symbol_fa']=='T۵۹' and b['quantity']==1 and b['bonus_share']['quantity']==2 and b['bonus_share']['broker_return_pct']==1234.5 and p['buy_total_check']['status']=='bounds_only'";
      const parsed=spawnSync(python,['-c',parserCheck+(options.bonus ? bonusCheck : '')],
        {input:JSON.stringify(result),encoding:'utf-8',cwd:path.resolve('.')});
      assert.equal(parsed.status,0,parsed.stderr);
    }
    cases++; await context.close();
  }
  const context = await browser.newContext();
  const page = await context.newPage();
  page.setDefaultTimeout(6000);
  await page.route('**/*', async route => {
    if (route.request().url() !== origin+'/') {unexpectedRequests++; await route.abort(); return;}
    await route.fulfill({contentType:'text/html',body:industryFixture});
  });
  await page.goto(origin+'/');
  const tab = {url:async()=>page.url(), playwright:page};
  const captured = await captureIndustries(tab);
  assert.deepEqual(captured.menu, [{name:'صندوق‌ها',heading:true},{name:'درآمد ثابت',heading:false}]);
  assert.deepEqual(captured.categories, [{name:'درآمد ثابت',status:'observed',row_count:1,symbols:['نماد آزمایشی']}]);
  await industryCategory(tab, 'درآمد ثابت');
  const bound = await industrySymbolIsin(tab, 'درآمد ثابت', 'نماد آزمایشی');
  assert.equal(bound.isin,'IRO3TEST0001');
  const group = await peerGroup(tab, {sector_code:'64',sector_name:'آزمون',seed_symbol:bound.symbol,seed_isin:bound.isin});
  assert.deepEqual(group.symbols,['نماد آزمایشی','هم‌گروه آزمایشی']);
  const peerBinding = await peerSymbolIsin(tab, group, 'هم‌گروه آزمایشی');
  assert.equal(peerBinding.isin,'IRO3PEER0001');
  cases++; await context.close();
  assert.equal(unexpectedRequests,0);
  console.log(JSON.stringify({browser:browser.version(),cases,unexpectedRequests,python_parser_parity:true}));
} finally {await browser.close();}
