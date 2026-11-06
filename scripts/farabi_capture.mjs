/** Read-only DOM/report capture. Never reads cookies, tokens, storage or app internals. */
import fs from 'node:fs/promises';
import path from 'node:path';
import { performance as clock } from 'node:perf_hooks';
import { setTimeout as pause } from 'node:timers/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';

export const origin = 'https://farabixo.irfarabi.com';
export const portfolioHeaders = ['نماد', 'تعداد', 'خالص میانگین', 'جمع خرید', 'سر به سر', 'قیمت لحظه‌ای', 'ارزش', 'سود/زیان'];
const bonusLabel = 'سهام جایزه';
const bonusFormat = 'farabi-dom-v2';
const actionTimeoutMs = 6000;
const decimalPattern = String.raw`[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?`;
const percentPattern = `(?:${decimalPattern}|[+-]?∞|-)`;
const numericField = new RegExp(`^${decimalPattern}$`);
const percentField = new RegExp(`^\\((${percentPattern})%\\)$`);
const displayPercent = `(\\(${percentPattern}%\\))`;
const displayAmount = '([+-]?[\\d,]+)';
const percentFirst = new RegExp(`^${displayPercent}\\s*${displayAmount}$`);
const amountFirst = new RegExp(`^${displayAmount}\\s*${displayPercent}$`);

export function displayText(text) {
  return String(text).replace(/[۰-۹]/g, c => String(c.charCodeAt(0) - 1776))
    .replace(/[٠-٩]/g, c => String(c.charCodeAt(0) - 1632)).replace(/٬/g, ',').replace(/٫/g, '.')
    .replace(/٪/g, '%').replace(/−/g, '-').replace(/[\u200e\u200f\u202a-\u202e\u2066-\u2069]/g, '').trim();
}

export function numeric(text) {
  const normalized = displayText(text);
  if (!numericField.test(normalized)) throw new Error('Unrecognized numeric field');
  const translated = normalized.replace(/,/g, '');
  const result = Number(translated);
  if (!Number.isFinite(result) || Math.abs(result) > Number.MAX_SAFE_INTEGER) throw new Error('Invalid numeric range');
  return result;
}

export function optionalPercent(text) {
  const match = displayText(text).match(percentField);
  if (!match) throw new Error('Portfolio performance format changed');
  return match[1].includes('∞') || match[1] === '-' ? null : numeric(match[1]);
}

export function performance(text) {
  const normalized = displayText(text);
  let match = normalized.match(percentFirst);
  if (match) return {pct:optionalPercent(match[1]), amount:numeric(match[2])};
  match = normalized.match(amountFirst);
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
  return value.replace(/[يى]/g, 'ی').replace(/ك/g, 'ک').replace(/ة/g, 'ه')
    .replace(/\s+/g, ' ').trim().replace(/\u200c/g, '');
}

function displayLines(value) {
  return displayText(value).split(/\r?\n/).map(line => line.trim());
}

function splitPortfolioRow(values, allowBonus) {
  const [symbol, ...labels] = values[0].trim().split(/\r?\n/).map(line => line.trim());
  if (!labels.length) return {primary:values, bonus:null};
  if (!allowBonus || labels.length !== 1 || labels[0] !== bonusLabel) throw new Error('Unexpected holding label');
  const primary = [...values];
  primary[0] = symbol;
  const extra = new Map();
  for (const index of [1, 2, 4, 6]) {
    const parts = displayLines(values[index]);
    if (parts.length !== 2) throw new Error('Bonus share columns changed');
    primary[index] = parts[0];
    extra.set(index, parts[1]);
  }
  const pnl = displayText(values[7]).split(/\s+/);
  if (extra.get(2) !== '-' || extra.get(4) !== '-' || pnl.length !== 4) throw new Error('Bonus share columns changed');
  primary[7] = pnl.slice(0, 2).join(' ');
  const bonus = {quantity:numeric(extra.get(1)), value:numeric(extra.get(6)), pnl:performance(pnl.slice(2).join(' ')).amount};
  if (!Number.isSafeInteger(bonus.quantity) || bonus.quantity <= 0 || !Number.isSafeInteger(bonus.value) || bonus.value < 0 || !Number.isSafeInteger(bonus.pnl)) {
    throw new Error('Invalid bonus share fields');
  }
  return {primary, bonus};
}

export function verifySnapshot(snapshot) {
  if (!Number.isSafeInteger(snapshot.expected_positions) || snapshot.expected_positions <= 0) throw new Error('A confirmed nonempty portfolio is required');
  if (!Array.isArray(snapshot.rows) || !snapshot.rows.every(row => Array.isArray(row) && row.length === 8 && row.every(v => typeof v === 'string'))) throw new Error('Unexpected portfolio row format');
  if (snapshot.rows.length !== snapshot.expected_positions) throw new Error('Incomplete virtual table: capture stopped');
  if (JSON.stringify(snapshot.headers) !== JSON.stringify(portfolioHeaders)) throw new Error('Portfolio columns changed');
  const holdings = snapshot.rows.map(row => splitPortfolioRow(row, snapshot.format === bonusFormat));
  const keys = holdings.map(({primary}) => symbolKey(primary[0]));
  if (keys.some(key => !key) || new Set(keys).size !== holdings.length) throw new Error('Duplicate or missing holdings');
  for (const key of ['net_assets_rial','portfolio_value_rial','cash_rial','buying_power_rial','blocked_rial','buy_total_rial','broker_pnl_rial']) {
    if (!Number.isSafeInteger(snapshot.summary?.[key])) throw new Error('Invalid account total');
  }
  if (['net_assets_rial','portfolio_value_rial','buying_power_rial','blocked_rial','buy_total_rial'].some(key=>snapshot.summary[key]<0)) throw new Error('Invalid negative account value');
  if (snapshot.summary.broker_return_pct !== null && !Number.isFinite(snapshot.summary.broker_return_pct)) throw new Error('Invalid portfolio return');
  let displayedCost = 0, ordinaryCost = 0, value = 0, pnl = 0;
  for (const {primary:row, bonus} of holdings) {
    const quantity = numeric(row[1]), cost = numeric(row[3]), rowValue = numeric(row[6]);
    const rowPnl = performance(row[7]).amount;
    if (!Number.isSafeInteger(quantity) || quantity < 0 || (quantity === 0 && (!bonus || rowValue !== 0))) throw new Error('Invalid holding quantity');
    if ([cost, rowValue].some(v => !Number.isSafeInteger(v) || v < 0)) throw new Error('Invalid holding total');
    for (const index of [2,4]) numeric(row[index]);
    const price = performance(row[5]).amount;
    if (!Number.isSafeInteger(price) || price < 0 || !Number.isSafeInteger(rowPnl)) throw new Error('Invalid holding price or performance');
    displayedCost += cost;
    if (!bonus) ordinaryCost += cost;
    value += rowValue;
    pnl += rowPnl;
  }
  if (![displayedCost, ordinaryCost, value, pnl].every(Number.isSafeInteger)) throw new Error('Invalid portfolio total range');
  // On bonus rows Farabi displays the combined buy cost, while the summary excludes the bonus cost.
  const tolerance = snapshot.expected_positions;
  const totalCost = snapshot.summary.buy_total_rial;
  if (ordinaryCost - totalCost > tolerance || totalCost - displayedCost > tolerance) throw new Error('Portfolio buy total is outside displayed bounds');
  if (Math.abs(value - snapshot.summary.portfolio_value_rial) > tolerance) throw new Error('Portfolio value changed or rows are missing');
  if (Math.abs(pnl - snapshot.summary.broker_pnl_rial) > tolerance) throw new Error('Portfolio performance total changed');
  if (Math.abs(snapshot.summary.net_assets_rial - snapshot.summary.portfolio_value_rial - snapshot.summary.cash_rial) > 1) throw new Error('Account totals are inconsistent');
  return snapshot;
}

async function readAccountTotals(tab, actionOptions, evaluate) {
  const close = tab.playwright.getByText('بستن', {exact:true});
  await tab.playwright.locator('[data-gid="portfolio_home_assetsTabSection_showPortfolioVisualizationButton"]').click(actionOptions);
  try {
    await tab.playwright.getByText('تعداد سهام', {exact:true}).waitFor({state:'visible', ...actionOptions});
    let previous, lastError;
    const deadline = clock.now() + 12_000;
    while (clock.now() < deadline) {
      try {
        const raw = await evaluate(() => {
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
        const totals = parseAccountTotals(raw);
        const coherent = Number.isSafeInteger(totals.expected_positions) && totals.expected_positions > 0
          && Math.abs(totals.summary.portfolio_value_rial + totals.summary.cash_rial - totals.summary.net_assets_rial) <= 1;
        if (coherent && JSON.stringify(totals) === previous) return totals;
        previous = coherent ? JSON.stringify(totals) : null;
      } catch (error) { lastError = error; previous = null; }
      // Allow the summary to settle before taking the next independent reading.
      await pause(100);
    }
    throw new Error('Account summary did not settle', {cause:lastError});
  } finally {
    if (await close.isVisible()) await close.click(actionOptions);
  }
}

export async function capturePortfolio(tab, {actionOptions = {timeout:actionTimeoutMs}, evaluate = reader => tab.playwright.evaluate(reader)} = {}) {
  const url = new URL(await tab.url());
  if (url.origin !== origin || url.username || url.password) throw new Error('Only the authorized Farabi origin is supported');
  if (url.pathname !== '/') {
    await tab.playwright.getByRole('link', {name:'اوراق بهادار', exact:true}).click(actionOptions);
    await tab.playwright.getByRole('tab', {name:'سبد سهام', exact:true}).waitFor({state:'visible', ...actionOptions});
  }
  const totals = await readAccountTotals(tab, actionOptions, evaluate);
  const summary = totals.summary;
  const isOpen = await evaluate(() => Array.from(document.querySelectorAll('th')).some(e => e.innerText === 'خالص میانگین'));
  if (!isOpen) await tab.playwright.getByRole('tab', {name:'سبد سهام', exact:true}).click(actionOptions);
  const gathered = new Map();
  const scan = [];
  let initialized = false;
  let lastTop = -1;
  let stagnant = 0;
  let headers;
  let completed = false;
  const deadline = clock.now() + 120_000;
  for (let step = 0; step < 300 && clock.now() < deadline; step++) {
    const chunk = await evaluate(() => {
      const tables = Array.from(document.querySelectorAll('table')).filter(t => Array.from(t.querySelectorAll('th')).some(e => e.innerText === 'خالص میانگین'));
      if (tables.length !== 1) return null;
      const [table] = tables;
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
      await tab.scroll([chunk.x,chunk.y], 'up', (chunk.top+chunk.viewportHeight)/chunk.viewportHeight, chunk.top);
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
    if (atEnd && gathered.size === totals.expected_positions) {completed = true; break;}
    if (stagnant >= 3) throw new Error('Scrolling stopped before all holdings were captured');
    await tab.scroll([chunk.x,chunk.y], 'down', chunk.height * .75 / chunk.viewportHeight, chunk.top);
  }
  if (!completed) throw new Error('Incomplete virtual table: capture stopped');
  const snapshot = verifySnapshot({format:bonusFormat, kind:'portfolio', currency:'IRR',
    source_url:origin+'/', captured_at:new Date().toISOString(), market_date:null,
    expected_positions:totals.expected_positions, summary, headers, rows:Array.from(gathered.values()), scan});
  const finalTotals = await readAccountTotals(tab, actionOptions, evaluate);
  if (JSON.stringify(finalTotals) !== JSON.stringify(totals)) throw new Error('Account changed during capture; no snapshot will be saved');
  return snapshot;
}

/** Capture from a temporary securities tab in a browser already connected to Codex. */
export async function captureConnectedPortfolio(tab) {
  const url = new URL(await tab.url());
  if (url.origin !== origin || url.pathname !== '/' || url.username || url.password || url.searchParams.has('virtualPortfolio')) {
    throw new Error('Open the real Farabi securities view in the authorized browser');
  }
  const pw = tab.playwright;
  const actionOptions = {timeoutMs:actionTimeoutMs};
  const grid = pw.locator('div.overflow-auto.border-neutral-tertiary').filter({has:pw.getByText(portfolioHeaders[2], {exact:true})});
  const scroll = async (_point, direction, _pages, before) => {
    const rewinding = direction === 'up';
    await grid.press(rewinding ? 'Home' : 'PageDown', actionOptions);
    let previous = before;
    for (let attempt = 0; attempt < 8; attempt++) {
      const top = await grid.evaluate(element => element.scrollTop, undefined, actionOptions);
      if (rewinding ? top === 0 : top > before && top === previous) return;
      previous = top;
      await pause(50);
    }
    throw new Error('Portfolio scroll did not settle');
  };
  const adapter = {url:() => tab.url(), playwright:pw,
    scroll};
  return capturePortfolio(adapter, {actionOptions, evaluate:reader => pw.evaluate(reader, undefined, actionOptions)});
}

async function writePortfolioSnapshot(output, snapshot) {
  const filename = path.join(output, 'portfolio-dom.json');
  const temporary = filename + '.tmp';
  await fs.writeFile(temporary, JSON.stringify(snapshot, null, 2), {mode:0o600, flag:'wx'});
  await fs.rename(temporary, filename);
  return filename;
}

/** Persist only a verified snapshot. Return metadata, so account values need not enter chat logs. */
export async function saveConnectedPortfolio(tab) {
  const snapshot = await captureConnectedPortfolio(tab);
  const project = await fs.realpath(fileURLToPath(new URL('..', import.meta.url)));
  const archive = path.join(project, 'data', 'private', 'farabi');
  await fs.mkdir(archive, {recursive:true});
  if (path.relative(archive, await fs.realpath(archive)) !== '') throw new Error('Farabi archive is outside its private directory');
  const output = await fs.mkdtemp(path.join(archive, 'connected-'));
  const filename = await writePortfolioSnapshot(output, snapshot);
  return {filename, captured_at:snapshot.captured_at, positions:snapshot.rows.length,
    bonus_positions:snapshot.rows.filter(row => splitPortfolioRow(row, true).bonus !== null).length};
}

function captureErrorType(error) {
  return error?.name === 'TimeoutError' ? 'timeout' :
    error?.name === 'TargetClosedError' ? 'browser_closed' : 'other';
}

export async function writeCaptureStatus(output, status) {
  const temporary = path.join(output, 'capture-status.json.tmp');
  await fs.writeFile(temporary, JSON.stringify(status, null, 2), {mode:0o600});
  await fs.rename(temporary, path.join(output, 'capture-status.json'));
}

export async function captureReports(tab, download, onProgress = async () => {}) {
  const files = [], reports = {};
  for (const [key, route, header, filename] of [
    ['account_turnover', '/account-turnover', 'بدهکار (ریالء)', 'account-turnover.xlsx'],
    ['orders_history', '/orders-history', 'وضعیت سفارش', 'orders-history.xlsx'],
  ]) {
    let phase = 'navigate';
    let progressFailed = false;
    const progress = async event => {
      try { await onProgress(event); }
      catch (error) { progressFailed = true; throw error; }
    };
    await progress({report:key, status:'running', phase, error_type:null});
    try {
      await tab.goto(origin + route);
      phase = 'table';
      await progress({report:key, status:'running', phase, error_type:null});
      await tab.playwright.getByRole('columnheader', {name:header, exact:true}).waitFor({state:'visible'});
      phase = 'download';
      await progress({report:key, status:'running', phase, error_type:null});
      // Export reflects the selected report range; filters are never changed.
      files.push(await download(tab, filename));
      reports[key] = {status:'saved', phase:'complete', error_type:null};
    } catch (error) {
      if (progressFailed) throw error;
      // Never persist exception text, page content, credentials, or account data.
      reports[key] = {status:'failed', phase, error_type:captureErrorType(error)};
    }
    await progress({report:key, ...reports[key]});
  }
  return {files, reports};
}

async function main() {
  const modulePath = process.env.BRS_PLAYWRIGHT_MODULE;
  const {chromium} = modulePath ? await import(pathToFileURL(modulePath).href) : await import('playwright');
  const output = path.resolve(process.argv[2] || 'data/private/farabi/capture-' + Date.now());
  if (!output.startsWith(path.resolve('data/private') + path.sep)) throw new Error('Use a private output directory under data/private');
  await fs.mkdir(output,{recursive:true});
  const status = {version:'farabi-capture-status-v1', portfolio:{status:'pending'},
    reports:{account_turnover:{status:'pending'}, orders_history:{status:'pending'}},
    historical_coverage_verified:false, real_orders_submitted:false};
  await writeCaptureStatus(output, status);
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
    status.portfolio = {status:'running', phase:'capture_and_verify'};
    await writeCaptureStatus(output, status);
    try {
      const snapshot = await capturePortfolio(adapter);
      await writePortfolioSnapshot(output, snapshot);
      status.portfolio = {status:'saved', phase:'complete'};
    } catch (error) {
      status.portfolio = {status:'failed', phase:'capture_and_verify', error_type:captureErrorType(error)};
      await writeCaptureStatus(output, status);
      throw error;
    }
    await writeCaptureStatus(output, status);
    const captured = await captureReports(adapter,async (tab,filename) => {
      const [result] = await Promise.all([
        page.waitForEvent('download',{timeout:30000}),
        tab.playwright.getByText('خروجی',{exact:true}).click(),
      ]);
      const target = path.join(output,filename);
      await result.saveAs(target);
      return target;
    }, async ({report, ...progress}) => {
      status.reports[report] = progress;
      await writeCaptureStatus(output, status);
    });
    if (Object.values(captured.reports).some(report => report.status !== 'saved')) {
      console.error('برداشت ناقص است؛ وضعیت هر خروجی در capture-status.json پوشه خصوصی ثبت شد.');
      process.exitCode = 1;
    } else {
      console.log('فایل‌ها در پوشه خصوصی ذخیره شدند؛ بازه گزارش‌ها را جداگانه بررسی کنید.');
    }
  } finally {
    await browser.close();
  }
}

if (typeof process !== 'undefined' && process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().catch(() => {console.error('برداشت متوقف شد؛ وضعیت را در پوشه خصوصی بررسی کنید.'); process.exitCode=1;});
}
