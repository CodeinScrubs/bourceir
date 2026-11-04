import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import {capturePortfolio, captureReports, numeric, optionalPercent, parseAccountTotals, performance, portfolioHeaders, verifySnapshot, writeCaptureStatus} from '../scripts/farabi_capture.mjs';

// Synthetic fixtures remain under tests and never enter the market/account databases.
function snapshot() {
  return {expected_positions:2, headers:[...portfolioHeaders],
    rows:[['A','1','1','100','1','(0%) 1','100','(+0%) +0'],
          ['B','2','2','200','2','(0%) 2','200','(+0%) +0']],
    summary:{buy_total_rial:300, portfolio_value_rial:300, broker_pnl_rial:0, net_assets_rial:310, cash_rial:10,
      buying_power_rial:10, blocked_rial:0, broker_return_pct:null}};
}

test('numeric fields support Persian/Arabic digits and reject unknown or unsafe values', () => {
  assert.equal(numeric('−۱۲٬۳۴۵'), -12345);
  assert.equal(numeric('١٢٣.٥'), 123.5);
  for (const value of ['∞','',null,'12 ریال','9e8','9007199254740992','1,2','1,,000']) assert.throws(() => numeric(value));
});

test('capture requires all unique rows and independent control totals', () => {
  assert.equal(verifySnapshot(snapshot()).rows.length,2);
  for (const mutate of [s => s.rows.pop(), s => s.rows[1]=s.rows[0],
    s => s.headers.reverse(), s => s.summary.portfolio_value_rial+=3,
    s => s.summary.net_assets_rial+=3, s => s.summary.broker_pnl_rial+=3, s => s.summary.buy_total_rial+=3,
    s => s.summary.portfolio_value_rial=NaN,
    s => s.expected_positions=0]) {
    const source=snapshot(); mutate(source); assert.throws(() => verifySnapshot(source));
  }
  const rounded=snapshot(); rounded.summary.portfolio_value_rial-=1; rounded.summary.net_assets_rial-=1;
  assert.doesNotThrow(() => verifySnapshot(rounded));
});

test('unknown percentages and localized performance are parsed without inventing a return', () => {
  for (const value of ['(+∞%)','(−∞٪)','(-%)']) assert.equal(optionalPercent(value), null);
  assert.equal(optionalPercent('(−۱۲٫۵٪)'), -12.5);
  assert.deepEqual(performance('\u2066(−۱۲٫۵٪) −۱٬۰۰۰\u2069'), {pct:-12.5, amount:-1000});
  assert.deepEqual(performance('+۱٬۰۰۰ (+∞٪)'), {pct:null, amount:1000});
  for (const value of ['(+∞%) garbage','unknown','(+1..2%)']) assert.throws(() => optionalPercent(value));
});

test('localized rows remain reconciled and zero table costs never bypass controls', () => {
  const source = snapshot();
  source.rows[0][7] = '(+۰٪) +۰';
  assert.doesNotThrow(() => verifySnapshot(source));
  source.rows.forEach(row => {row[3] = '0';});
  assert.throws(() => verifySnapshot(source), /total/);
  const duplicate = snapshot(); duplicate.rows[0][0] = 'ک'; duplicate.rows[1][0] = 'ك';
  assert.throws(() => verifySnapshot(duplicate), /Duplicate/);
});

function simulatedTab({infinite=false, rounded=false, initialTop=800, stuck=false, changed=false}={}) {
  let top = initialTop, upCalls = 0, summaryCalls = 0;
  const chain = {click:async () => {}, waitFor:async () => {}, locator:() => chain, getByText:() => chain};
  const totals = {expected:'60', value:'6,000', cash:['10','ریال'], net:[rounded ? '6,011' : '6,010','ریال'],
    power:['10'], blocked:['0'], cost:infinite ? '0' : '6,000', pnl:infinite ? '6,000' : '0', pct:infinite ? '(−∞٪)' : '(۰٪)'};
  return {url:async () => 'https://farabixo.irfarabi.com/',
    playwright:{locator:() => chain, getByText:() => chain, getByRole:() => chain,
      evaluate:async callback => {
        const code = String(callback);
        if (code.includes('const spans')) {
          summaryCalls++;
          return {...totals, power:changed && summaryCalls > 2 ? ['9'] : totals.power};
        }
        if (!code.includes('const table =')) return true;
        const start = Math.floor(top / 20);
        return {headers:portfolioHeaders, rows:Array.from({length:Math.min(4,60-start)}, (_, i) =>
          ['S'+(start+i),'1','100',infinite ? '0' : '100','100','(۰٪) ۱۰۰','۱۰۰',infinite ? '(+∞٪) +۱۰۰' : '(۰٪) +۰']),
          top, height:80, total:1200, x:100,y:100,viewportHeight:800};
      }},
    scroll:async (point,direction) => {
      if (direction === 'up') {upCalls++; if (!stuck) top=Math.max(0,top-100);}
      else top=Math.min(1120,top+60);
    },
    upCalls:() => upCalls};
}

test('capture rewinds repeatedly before collecting and supports infinite summary return', async () => {
  const tab = simulatedTab({infinite:true});
  const result = await capturePortfolio(tab);
  assert.equal(result.rows.length,60);
  assert.equal(result.summary.broker_return_pct,null);
  assert.ok(tab.upCalls() > 1);
  assert.equal(result.rows[0][0],'S0');
});

test('account rounding tolerance is consistent during loading and final validation', async () => {
  const result = await capturePortfolio(simulatedTab({rounded:true, initialTop:0}));
  assert.equal(result.summary.net_assets_rial,6011);
  assert.throws(() => parseAccountTotals({expected:'1', value:'1',cost:'1',pnl:'0',pct:'(0%)',
    net:['1','2'],cash:['0'],power:['0'],blocked:['0']}), /Ambiguous/);
});

test('stuck rewinds and changes between captures fail without returning a snapshot', async () => {
  await assert.rejects(capturePortfolio(simulatedTab({stuck:true})), /rewind/);
  await assert.rejects(capturePortfolio(simulatedTab({changed:true})), /changed during capture/);
});

test('a failed first report preserves a safe diagnostic and does not prevent the second report', async () => {
  let route = '';
  const visits = [], progress = [], downloads = [];
  const tab = {goto:async url => {route = new URL(url).pathname; visits.push(route);},
    playwright:{getByRole:() => ({waitFor:async () => {
      if (route === '/account-turnover') {
        const error = new Error('private token or account text must never be copied');
        error.name = 'TimeoutError';
        throw error;
      }
    }})}};
  const result = await captureReports(tab, async (_, filename) => {downloads.push(filename); return filename;},
    async event => progress.push(event));
  assert.deepEqual(visits, ['/account-turnover', '/orders-history']);
  assert.deepEqual(downloads, ['orders-history.xlsx']);
  assert.deepEqual(result.reports.account_turnover, {status:'failed', phase:'table', error_type:'timeout'});
  assert.deepEqual(result.reports.orders_history, {status:'saved', phase:'complete', error_type:null});
  assert.doesNotMatch(JSON.stringify(progress), /private|token|account text/);
});

test('a diagnostic persistence failure aborts rather than silently proceeding', async () => {
  let visited = false;
  const tab = {goto:async () => {visited = true;}};
  await assert.rejects(captureReports(tab, async () => {}, async () => {throw new Error('disk full');}));
  assert.equal(visited, false);
});

test('a later diagnostic persistence failure also aborts without opening another report', async () => {
  const visits = [];
  const tab = {goto:async url => visits.push(new URL(url).pathname)};
  let updates = 0;
  await assert.rejects(captureReports(tab, async () => {}, async () => {
    if (++updates === 2) throw new Error('disk full');
  }));
  assert.deepEqual(visits, ['/account-turnover']);
});

test('status is replaceable on Windows without leaving a temporary file', async t => {
  const parent = path.resolve(os.tmpdir());
  const directory = await fs.mkdtemp(path.join(parent, 'brs-capture-status-'));
  assert.equal(path.dirname(directory), parent);
  t.after(async () => fs.rm(directory, {recursive:true, force:true}));
  await writeCaptureStatus(directory, {status:'running'});
  await writeCaptureStatus(directory, {status:'failed', phase:'table', error_type:'timeout'});
  assert.deepEqual(JSON.parse(await fs.readFile(path.join(directory, 'capture-status.json'), 'utf8')),
    {status:'failed', phase:'table', error_type:'timeout'});
  await assert.rejects(fs.access(path.join(directory, 'capture-status.json.tmp')));
});
