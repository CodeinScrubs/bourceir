/** آزمون DOM ساختگی با Chromium؛ هیچ درخواست واقعی به کارگزاری ارسال نمی‌شود. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import {pathToFileURL} from 'node:url';
import {capturePortfolio, displayText, origin, portfolioHeaders} from '../scripts/farabi_capture.mjs';

const {chromium} = await import(pathToFileURL(process.env.BRS_PLAYWRIGHT_MODULE).href);
const browser = await chromium.launch({headless:true});
function fixture({localized=false, rounded=false, changed=false}={}) {
  const data = JSON.stringify({localized,rounded,changed,headers:portfolioHeaders});
  return `<!doctype html><meta charset="utf-8"><title>Isolated BRS capture test</title>
    <button data-gid="portfolio_home_assetsTabSection_showPortfolioVisualizationButton">Summary</button>
    <section id="modal" hidden></section>
    <div id="viewport" style="height:192px;overflow:auto;width:900px"><div style="height:1952px;position:relative">
    <table style="position:absolute;border-collapse:collapse;table-layout:fixed;width:880px"><thead></thead><tbody></tbody></table></div></div>
    <style>td,th{height:32px;box-sizing:border-box;padding:0;border:0}</style>
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
          return '<tr>'+values.map(v=>'<td>'+digit(v)+'</td>').join('')+'</tr>';}).join('');}
      viewport.addEventListener('scroll',()=>{render();if(viewport.scrollTop===0)wasDown=true;if(options.changed&&wasDown&&viewport.scrollTop>200)power='9';});
      viewport.scrollTop=1000;render();
    </script>`;
}

let unexpectedRequests=0, cases=0;
try {
  for (const options of [{}, {localized:true,rounded:true}, {localized:true,changed:true}]) {
    const context=await browser.newContext({viewport:{width:1200,height:900}});
    const page=await context.newPage();
    await page.route('**/*', async route => {
      if (route.request().url() !== origin+'/') {unexpectedRequests++; await route.abort(); return;}
      await route.fulfill({contentType:'text/html',body:fixture(options)});
    });
    await page.goto(origin+'/');
    const tab={url:async()=>page.url(),playwright:page,scroll:async([x,y],direction,pages)=>{
      await page.mouse.move(x,y);await page.mouse.wheel(0,(direction==='up'?-1:1)*pages*900);await page.waitForTimeout(100);
    }};
    if (options.changed) await assert.rejects(capturePortfolio(tab),/changed during capture/);
    else {
      const result=await capturePortfolio(tab);
      assert.equal(result.rows.length,60);assert.equal(displayText(result.rows[0][0]),'T0');assert.equal(result.summary.broker_return_pct,null);
      assert.equal(result.summary.net_assets_rial,options.rounded?6011:6010);
      const python=[process.env.BRS_TEST_PYTHON,'.venv/Scripts/python.exe','venv/Scripts/python.exe'].find(p=>p&&fs.existsSync(p)) || 'python';
      const parsed=spawnSync(python,['-c',"import sys; from data_collector.farabi import parse_portfolio; p=parse_portfolio(sys.stdin.buffer.read()); assert len(p['rows'])==60 and p['summary']['broker_return_pct'] is None"],
        {input:JSON.stringify(result),encoding:'utf-8',cwd:path.resolve('.')});
      assert.equal(parsed.status,0,parsed.stderr);
    }
    cases++; await context.close();
  }
  assert.equal(unexpectedRequests,0);
  console.log(JSON.stringify({browser:browser.version(),cases,unexpectedRequests,python_parser_parity:true}));
} finally {await browser.close();}
