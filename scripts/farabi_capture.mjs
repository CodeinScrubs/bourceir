/** Read-only DOM/report capture. Never reads cookies, tokens, storage or app internals. */
import fs from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

export const origin = 'https://farabixo.irfarabi.com';
export const portfolioHeaders = ['نماد', 'تعداد', 'خالص میانگین', 'جمع خرید', 'سر به سر', 'قیمت لحظه‌ای', 'ارزش', 'سود/زیان'];

export function displayText(text) {
  return String(text).replace(/[۰-۹]/g, c => String(c.charCodeAt(0) - 1776))
    .replace(/[٠-٩]/g, c => String(c.charCodeAt(0) - 1632)).replace(/٬/g, ',').replace(/٫/g, '.')
    .replace(/٪/g, '%').replace(/−/g, '-').replace(/[\u200e\u200f\u202a-\u202e\u2066-\u2069]/g, '').trim();
}

export function numeric(text) {
  const normalized = displayText(text);
  if (!/^[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?$/.test(normalized)) throw new Error('Unrecognized numeric field');
  const translated = normalized.replace(/,/g, '');
  const result = Number(translated);
  if (!Number.isFinite(result) || Math.abs(result) > Number.MAX_SAFE_INTEGER) throw new Error('Invalid numeric range');
  return result;
}

export function optionalPercent(text) {
  const match = displayText(text).match(/^\(([+-]?(?:\d+(?:\.\d+)?|∞)|-)%\)$/);
  if (!match) throw new Error('Portfolio performance format changed');
  return match[1].includes('∞') || match[1] === '-' ? null : numeric(match[1]);
}

export function performance(text) {
  const normalized = displayText(text);
  const percent = '(\\([+-]?(?:\\d+(?:\\.\\d+)?|∞|-)%\\))';
  const amount = '([+-]?[\\d,]+)';
  let match = normalized.match(new RegExp('^' + percent + '\\s*' + amount + '$'));
  if (match) return {pct:optionalPercent(match[1]), amount:numeric(match[2])};
  match = normalized.match(new RegExp('^' + amount + '\\s*' + percent + '$'));
  if (match) return {pct:optionalPercent(match[2]), amount:numeric(match[1])};
  throw new Error('Portfolio performance format changed');
}

export function parseAccountTotals(totals) {
  const footer = value => {
    if (!Array.isArray(value)) return numeric(value);
    const candidates = value.filter(v => /^[+-]?[\d,]+$/.test(displayText(v)));
    if (candidates.length !== 1) throw new Error('Ambiguous or missing account total');
    return numeric(candidates[0]);
  };
  return {expected_positions:numeric(totals.expected), summary:{net_assets_rial:footer(totals.net),
    portfolio_value_rial:numeric(totals.value), cash_rial:footer(totals.cash), buying_power_rial:footer(totals.power),
    blocked_rial:footer(totals.blocked), buy_total_rial:numeric(totals.cost), broker_pnl_rial:numeric(totals.pnl),
    broker_return_pct:optionalPercent(totals.pct)}};
}

function symbolKey(value) {
  return displayText(value).replace(/ي/g, 'ی').replace(/ك/g, 'ک');
}

export function verifySnapshot(snapshot) {
  if (!Number.isSafeInteger(snapshot.expected_positions) || snapshot.expected_positions <= 0) throw new Error('A confirmed nonempty portfolio is required');
  if (!Array.isArray(snapshot.rows) || !snapshot.rows.every(row => Array.isArray(row) && row.length === 8 && row.every(v => typeof v === 'string'))) throw new Error('Unexpected portfolio row format');
  if (snapshot.rows.length !== snapshot.expected_positions) throw new Error('Incomplete virtual table: capture stopped');
  if (JSON.stringify(snapshot.headers) !== JSON.stringify(portfolioHeaders)) throw new Error('Portfolio columns changed');
  if (snapshot.rows.some(r => !symbolKey(r[0])) || new Set(snapshot.rows.map(r => symbolKey(r[0]))).size !== snapshot.rows.length) throw new Error('Duplicate or missing holdings');
  for (const key of ['net_assets_rial','portfolio_value_rial','cash_rial','buying_power_rial','blocked_rial','buy_total_rial','broker_pnl_rial']) {
    if (!Number.isSafeInteger(snapshot.summary?.[key])) throw new Error('Invalid account total');
  }
  if (['net_assets_rial','portfolio_value_rial','buying_power_rial','blocked_rial'].some(key=>snapshot.summary[key]<0)) throw new Error('Invalid negative account value');
  if (snapshot.summary.broker_return_pct !== null && !Number.isFinite(snapshot.summary.broker_return_pct)) throw new Error('Invalid portfolio return');
  for (const row of snapshot.rows) {
    if (!Number.isSafeInteger(numeric(row[1])) || numeric(row[1]) <= 0) throw new Error('Invalid holding quantity');
    for (const index of [3,6]) if (!Number.isSafeInteger(numeric(row[index])) || numeric(row[index]) < 0) throw new Error('Invalid holding total');
    for (const index of [2,4]) numeric(row[index]);
    if (!Number.isSafeInteger(performance(row[5]).amount) || performance(row[5]).amount < 0) throw new Error('Invalid holding price');
  }
  for (const [index, key] of [[3, 'buy_total_rial'], [6, 'portfolio_value_rial']]) {
    const total = snapshot.rows.reduce((sum, row) => sum + numeric(row[index]), 0);
    if (!Number.isSafeInteger(total)) throw new Error('Invalid portfolio total range');
    if (Math.abs(total - snapshot.summary[key]) > snapshot.expected_positions) throw new Error('Portfolio total changed or rows are missing');
  }
  const pnl = snapshot.rows.reduce((sum, row) => {
    return sum + performance(row[7]).amount;
  }, 0);
  if (!Number.isSafeInteger(pnl)) throw new Error('Invalid portfolio performance range');
  if (Math.abs(pnl - snapshot.summary.broker_pnl_rial) > snapshot.expected_positions) throw new Error('Portfolio performance total changed');
  if (Math.abs(snapshot.summary.net_assets_rial - snapshot.summary.portfolio_value_rial - snapshot.summary.cash_rial) > 1) throw new Error('Account totals are inconsistent');
  return snapshot;
}

async function readAccountTotals(tab) {
  await tab.playwright.locator('[data-gid="portfolio_home_assetsTabSection_showPortfolioVisualizationButton"]').click();
  await tab.playwright.getByText('تعداد سهام', {exact:true}).waitFor({state:'visible'});
  let totals, previous, lastError;
  try {
  for (let attempt = 0; attempt < 100; attempt++) {
    try {
    const raw = await tab.playwright.evaluate(() => {
      const spans = Array.from(document.querySelectorAll('span'));
      const unique = label => {
        const candidates = spans.filter(e => e.textContent.trim() === label);
        if (candidates.length !== 1) throw new Error('Ambiguous or missing account label: ' + label);
        return Array.from(candidates[0].parentElement.children).map(e => e.textContent.trim()).slice(1);
      };
      const performance = unique('بازده');
      return {expected:unique('تعداد سهام')[0], value:unique('ارزش سبد سهام')[0], cost:unique('جمع مبلغ خرید')[0],
        pnl:performance[1], pct:performance[0], net:unique('خالص دارایی'), cash:unique('مانده مشتری'),
        power:unique('قدرت خرید'), blocked:unique('بلوکه شده')};
    });
    totals = parseAccountTotals(raw);
    const coherent = totals.expected_positions > 0 && Math.abs(totals.summary.portfolio_value_rial + totals.summary.cash_rial - totals.summary.net_assets_rial) <= 1;
    if (coherent && JSON.stringify(totals) === previous) return totals;
    previous = coherent ? JSON.stringify(totals) : null;
    } catch (error) { lastError = error; previous = null; }
    // انتظار محدود برای تکمیل نمایش؛ به رشته انگلیسی یا مقدار فرضی وابسته نیست.
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error('Account summary did not settle' + (lastError ? ': ' + lastError.message : ''));
  } finally { await tab.playwright.getByText('بستن', {exact:true}).click(); }
}

export async function capturePortfolio(tab) {
  if (new URL(await tab.url()).origin !== origin) throw new Error('Only the authorized Farabi origin is supported');
  if (new URL(await tab.url()).pathname !== '/') {
    await tab.playwright.getByRole('link', {name:'اوراق بهادار', exact:true}).click();
    await tab.playwright.getByRole('tab', {name:'سبد سهام', exact:true}).waitFor({state:'visible'});
  }
  const totals = await readAccountTotals(tab);
  const summary = totals.summary;
  const isOpen = await tab.playwright.evaluate(() => Array.from(document.querySelectorAll('th')).some(e => e.innerText === 'خالص میانگین'));
  if (!isOpen) await tab.playwright.getByRole('tab', {name:'سبد سهام', exact:true}).click();
  const gathered = new Map();
  const scan = [];
  let initialized = false;
  let lastTop = -1;
  let stagnant = 0;
  let headers;
  for (let step = 0; step < 300; step++) {
    const chunk = await tab.playwright.evaluate(() => {
      const table = Array.from(document.querySelectorAll('table')).find(t => Array.from(t.querySelectorAll('th')).some(e => e.innerText === 'خالص میانگین'));
      if (!table) return null;
      const container = table.parentElement.parentElement;
      const rect = container.getBoundingClientRect();
      return {headers:Array.from(table.querySelectorAll('th')).map(e => e.innerText).filter(Boolean),
        rows:Array.from(table.querySelectorAll('tbody tr')).map(r => Array.from(r.querySelectorAll('td')).map(e => e.innerText)),
        top:container.scrollTop, height:container.clientHeight, total:container.scrollHeight,
        x:Math.max(20, Math.min(innerWidth-20, rect.left+rect.width/2)),
        y:Math.max(20, Math.min(innerHeight-20, rect.top+rect.height/2)), viewportHeight:innerHeight};
    });
    if (!chunk) throw new Error('Portfolio table is unavailable');
    headers = chunk.headers;
    if (JSON.stringify(headers) !== JSON.stringify(portfolioHeaders)) throw new Error('Portfolio columns changed');
    scan.push({top:chunk.top, height:chunk.height, total:chunk.total});
    if (!(chunk.height > 0 && chunk.viewportHeight > 0 && chunk.total >= chunk.height && chunk.top >= 0)) throw new Error('Invalid virtual table geometry');
    if (!initialized && chunk.top > 0) {
      if (chunk.top === lastTop) stagnant++; else stagnant = 0;
      if (stagnant >= 3) throw new Error('Portfolio table did not rewind to the first row');
      lastTop = chunk.top;
      await tab.scroll([chunk.x,chunk.y], 'up', (chunk.top+chunk.viewportHeight)/chunk.viewportHeight);
      continue;
    }
    if (!initialized) { lastTop = -1; stagnant = 0; }
    initialized = true;
    for (const row of chunk.rows) {
      if (row.length !== 8) throw new Error('Unexpected portfolio row format');
      const key = symbolKey(row[0]);
      const previous = gathered.get(key);
      if (previous && JSON.stringify(previous) !== JSON.stringify(row)) throw new Error('Portfolio changed during capture; retry from a settled state');
      gathered.set(key, row);
    }
    if (gathered.size > totals.expected_positions) throw new Error('Holding count changed during capture');
    if (chunk.top === lastTop) stagnant++; else stagnant = 0;
    lastTop = chunk.top;
    const atEnd = chunk.top + chunk.height >= chunk.total - 2;
    if (atEnd && gathered.size === totals.expected_positions) break;
    if (stagnant >= 3) throw new Error('Scrolling stopped before all holdings were captured');
    await tab.scroll([chunk.x,chunk.y], 'down', chunk.height * .75 / chunk.viewportHeight);
  }
  const snapshot = verifySnapshot({format:'farabi-dom-v1', kind:'portfolio', currency:'IRR',
    source_url:origin+'/', captured_at:new Date().toISOString(), market_date:null,
    expected_positions:totals.expected_positions, summary, headers, rows:Array.from(gathered.values()), scan});
  const finalTotals = await readAccountTotals(tab);
  if (JSON.stringify(finalTotals) !== JSON.stringify(totals)) throw new Error('Account changed during capture; no snapshot will be saved');
  return snapshot;
}

export async function captureReports(tab, download) {
  const files = [];
  for (const [route, header, filename] of [
    ['/account-turnover', 'بدهکار (ریالء)', 'account-turnover.xlsx'],
    ['/orders-history', 'وضعیت سفارش', 'orders-history.xlsx'],
  ]) {
    await tab.goto(origin + route);
    await tab.playwright.getByRole('columnheader', {name:header, exact:true}).waitFor({state:'visible'});
    // Export reflects the selected report range. This script never alters filters or claims full-history coverage.
    files.push(await download(tab, filename));
  }
  return files;
}

async function main() {
  const modulePath = process.env.BRS_PLAYWRIGHT_MODULE;
  const {chromium} = modulePath ? await import(pathToFileURL(modulePath).href) : await import('playwright');
  const output = path.resolve(process.argv[2] || 'data/private/farabi/capture-' + Date.now());
  if (!output.startsWith(path.resolve('data/private') + path.sep)) throw new Error('Use a private output directory under data/private');
  await fs.mkdir(output,{recursive:true});
  const browser = await chromium.launch({headless:false});
  try {
    const context = await browser.newContext({acceptDownloads:true});
    const page = await context.newPage();
    await page.goto(origin);
    console.log('در مرورگر وارد فارابی شوید. سپس در این پنجره Enter بزنید. رمز و نشست ذخیره نمی‌شود.');
    const {createInterface} = await import('node:readline/promises');
    const prompt = createInterface({input:process.stdin, output:process.stdout});
    await prompt.question('');
    prompt.close();
    const adapter = {url:() => page.url(), goto:url => page.goto(url), playwright:page,
      scroll:async ([x,y],direction,pages) => {
        const height = await page.evaluate(() => innerHeight);
        await page.mouse.move(x,y);
        await page.mouse.wheel(0,(direction === 'up' ? -1 : 1)*pages*height);
        await page.waitForTimeout(100);
      }};
    const snapshot = await capturePortfolio(adapter);
    await fs.writeFile(path.join(output,'portfolio-dom.json'),JSON.stringify(snapshot,null,2));
    await captureReports(adapter,async (tab,filename) => {
      const pending = page.waitForEvent('download',{timeout:30000});
      await tab.playwright.getByText('خروجی',{exact:true}).click();
      const result = await pending;
      const target = path.join(output,filename);
      await result.saveAs(target);
      return target;
    });
    console.log('برداشت کامل شد. فایل‌ها در پوشه خصوصی ذخیره شدند. برای واردکردن از صفحه حساب BRS استفاده کنید.');
  } finally {
    await browser.close();
  }
}

if (typeof process !== 'undefined' && process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().catch(error => {console.error('برداشت متوقف شد:', error.message); process.exitCode=1;});
}
