/** Capture only the visible Farabixo Industries menu and the market symbols in its tables. */
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {setTimeout as pause} from 'node:timers/promises';

const source = 'https://farabixo.irfarabi.com';
const timeout = {timeoutMs:6500};
const headers = ['نام نماد', 'قیمت', '%پایانی'];

export async function industryMenu(tab) {
  const url = new URL(await tab.url());
  if (url.origin !== source || url.pathname !== '/' || url.username || url.password) throw new Error('Farabixo securities view required');
  const panel = tab.playwright.getByRole('tabpanel', {name:'صنایع',exact:true});
  if (!(await panel.isVisible())) await tab.playwright.getByRole('tab', {name:'صنایع',exact:true}).click(timeout);
  const list = tab.playwright.getByRole('listbox');
  if (!(await list.isVisible())) await panel.getByRole('textbox', {name:'انتخاب کنید',exact:true}).click(timeout);
  const options = list.getByRole('option');
  await options.first().waitFor({state:'visible', ...timeout});
  const entries = await options.evaluateAll(items => items.map(e => ({name:e.innerText.trim(),
    heading:e.getAttribute('aria-disabled') === 'true' || e.hasAttribute('disabled')})));
  if (!entries.length || entries.some(e => !e.name) || new Set(entries.map(e => e.name)).size !== entries.length) throw new Error('Ambiguous Industries menu');
  return entries;
}

export async function industryCategory(tab, name) {
  const pw = tab.playwright;
  const panel = pw.getByRole('tabpanel', {name:'صنایع',exact:true});
  const list = pw.getByRole('listbox');
  const input = panel.getByRole('textbox', {name:'انتخاب کنید',exact:true});
  const selected = await input.evaluate(element => element.value.trim(), undefined, timeout);
  if (selected !== name || await list.isVisible()) {
    if (!(await list.isVisible())) await input.click(timeout);
    await list.getByRole('option', {name,exact:true}).click(timeout);
  }
  let previous = null;
  let retried = false;
  for (let attempt = 0; attempt < 10; attempt++) {
    const observed = await panel.evaluate(el => {
      const [table, ...extra] = el.querySelectorAll('table');
      const selected = el.querySelector('input[placeholder="انتخاب کنید"]')?.value.trim();
      if (!table || extra.length || table.tBodies.length !== 1) {
        return {selected, sourceError:el.innerText.includes('اطلاعاتی دریافت نشد')};
      }
      const headings = Array.from(table.tHead?.rows[0]?.cells ?? [], cell => cell.innerText.trim());
      const rows = Array.from(table.tBodies[0].rows, row => ({cells:row.cells.length,
        label:row.cells[0]?.innerText.trim(), height:row.getBoundingClientRect().height}));
      const wrapper = table.parentElement;
      const fullHeight = Math.abs(wrapper.clientHeight - table.getBoundingClientRect().height) <= 2;
      return {selected, headings, rows, fullHeight};
    }, undefined, timeout);
    if (!retried && observed.selected === name && observed.sourceError) {
      const retry = panel.getByText('تلاش دوباره', {exact:true});
      if (await retry.isVisible()) await retry.click(timeout);
      retried = true;
    }
    const valid = observed.selected === name && observed.fullHeight && JSON.stringify(observed.headings) === JSON.stringify(headers)
      && observed.rows.length > 0 && observed.rows.every(row => row.cells === headers.length && row.label && row.height > 0);
    const labels = valid ? observed.rows.map(row => row.label) : null;
    if (labels && JSON.stringify(labels) === previous) return {name, status:'observed', row_count:labels.length, symbols:labels};
    previous = labels ? JSON.stringify(labels) : null;
    await pause(120);
  }
  return {name, status:'unverified_table', row_count:null, symbols:[]};
}

export async function captureIndustries(tab, onCategory) {
  const menu = await industryMenu(tab);
  const categories = [];
  for (const {name, heading} of menu) {
    if (heading) continue;
    const category = await industryCategory(tab, name);
    categories.push(category);
    onCategory?.(category);
  }
  if (JSON.stringify(await industryMenu(tab)) !== JSON.stringify(menu)) throw new Error('Industries menu changed during capture');
  return {menu, categories};
}

export async function industrySymbolIsin(tab, category, symbol) {
  const panel = tab.playwright.getByRole('tabpanel', {name:'صنایع',exact:true});
  const cell = panel.getByRole('cell', {name:symbol,exact:true});
  if (await cell.count() !== 1) throw new Error('Unique industry symbol cell required');
  await cell.click(timeout);
  return {category, symbol, ...await boardIdentity(tab, symbol, {category})};
}

async function boardIdentity(tab, symbol, {category, expectedIsin} = {}) {
  for (let attempt = 0; attempt < 8; attempt++) {
    const state = await tab.playwright.evaluate(() => ({url:location.href, title:document.title,
      query:document.querySelector('input[placeholder="جست‌و‌جوی نماد یا شرکت"]')?.value ?? '',
      group:Array.from(document.querySelectorAll('[role="tabpanel"] input[placeholder="انتخاب کنید"]'))
        .find(e=>e.getClientRects().length)?.value.trim()}), undefined, timeout);
    const url = new URL(state.url);
    const isin = url.searchParams.get('isin');
    if ((!category || state.group === category) && (!expectedIsin || isin === expectedIsin)
      && url.origin === source && url.pathname === '/' && /^IR[A-Z0-9]{10}$/.test(isin ?? '')
      && (state.query.startsWith(symbol + ' - ') || state.title.startsWith(symbol + ' '))) {
      return {isin, url:source+'/?isin='+isin};
    }
    await pause(150);
  }
  throw new Error('Farabi board did not confirm this symbol');
}

function exactText(label) {
  return new RegExp('^' + label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '$');
}

export async function peerGroup(tab, {sector_code, sector_name, seed_symbol, seed_isin}) {
  const current = new URL(await tab.url());
  if (current.origin !== source || current.pathname !== '/' || !/^IR[A-Z0-9]{10}$/.test(seed_isin)) {
    throw new Error('Farabixo securities view and known seed ISIN required');
  }
  const pw = tab.playwright;
  if (current.searchParams.get('isin') !== seed_isin) {
    const input = pw.getByPlaceholder('جست‌و‌جوی نماد یا شرکت', {exact:true});
    await input.click(timeout);
    await input.fill(seed_symbol.slice(0, -1), timeout);
    await input.pressSequentially(seed_symbol.slice(-1), timeout);
    await pw.locator('span.text-c1.font-bold').filter({hasText:exactText(seed_symbol), visible:true}).click(timeout);
  }
  const seed = await boardIdentity(tab, seed_symbol, {expectedIsin:seed_isin});
  const group = {sector_code, sector_name, seed_symbol, seed_isin, url:seed.url};
  const read = () => pw.evaluate(() => Array.from(document.querySelectorAll('table')).flatMap(table => {
    const headings = Array.from(table.tHead?.rows[0]?.cells ?? [], cell => cell.innerText.trim());
    if (headings.slice(0, 3).join('|') !== 'نماد|لحظه‌ای|پایانی' || !table.getClientRects().length) return [];
    const height = table.getBoundingClientRect().height;
    const rows = Array.from(table.tBodies[0]?.rows ?? [], row => ({label:row.cells[0]?.innerText.trim(),
      complete:row.cells.length === headings.length && row.getBoundingClientRect().height > 0}));
    return [{full:headings.length === 10 && table.tBodies.length === 1
      && Math.abs(table.parentElement.clientHeight - height) <= 2, rows}];
  }), undefined, timeout);
  if (!(await read()).length) await pw.getByRole('button', {name:'هم‌گروه',exact:true}).click(timeout);
  let previous = null;
  for (let attempt = 0; attempt < 10; attempt++) {
    const tables = await read();
    const [table] = tables;
    const labels = tables.length === 1 && table.full && table.rows.every(row => row.complete && row.label)
      ? table.rows.map(row => row.label) : [];
    const snapshot = labels.includes(seed_symbol) ? JSON.stringify(labels) : null;
    if (snapshot && snapshot === previous) return {...group, status:'observed', row_count:labels.length, symbols:labels};
    previous = snapshot;
    await pause(120);
  }
  return {...group, status:'unverified_table', row_count:null, symbols:[]};
}

export async function peerSymbolIsin(tab, {sector_code, sector_name, symbols}, symbol) {
  if (!symbols.includes(symbol)) throw new Error('Symbol was not observed in this peer table');
  const cell = tab.playwright.locator('td.index').filter({hasText:exactText(symbol), visible:true});
  const inPeerTable = await cell.evaluate(e => Array.from(e.closest('table')?.tHead?.rows[0]?.cells ?? [],
    c => c.innerText.trim()).slice(0,3).join('|') === 'نماد|لحظه‌ای|پایانی', undefined, timeout);
  if (!inPeerTable) throw new Error('Exact peer table cell required');
  await cell.click(timeout);
  return {category:`هم‌گروه · ${sector_name || 'صنعت'} (${sector_code})`, symbol, ...await boardIdentity(tab, symbol)};
}

async function savePrivateCapture(day, filename, payload) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) throw new Error('Expected a canonical market day');
  const project = await fs.realpath(fileURLToPath(new URL('..', import.meta.url)));
  const directory = path.join(project, 'data', 'private', 'farabi-industry-candles', day);
  await fs.mkdir(directory, {recursive:true});
  if (path.relative(directory, await fs.realpath(directory)) !== '') throw new Error('Use the private BRS archive');
  const file = path.join(directory, filename);
  const temp = await fs.mkdtemp(path.join(directory, '.capture-'));
  try {
    const staging = path.join(temp, 'capture.json');
    await fs.writeFile(staging, JSON.stringify({...payload, captured_at:new Date().toISOString()}, null, 2), {flag:'wx',mode:0o600});
    await fs.rename(staging,file);
  } finally {
    await fs.unlink(path.join(temp, 'capture.json')).catch(error => {if (error.code !== 'ENOENT') throw error;});
    await fs.rmdir(temp);
  }
  return file;
}

export async function saveIndustryCapture(menu, categories, day) {
  if (menu.filter(e=>!e.heading).map(e=>e.name).join('\n') !== categories.map(c=>c.name).join('\n')) {
    throw new Error('Industry capture does not cover the displayed options');
  }
  return savePrivateCapture(day, 'farabi-industries.json', {version:'farabi-industries-dom-v1',source_url:source+'/',menu,categories});
}

export function saveIndustryBindings(rows, day) {
  return savePrivateCapture(day, 'farabi-isin-resolution.json', {version:'farabi-industry-isin-v1',rows});
}

export function savePeerGroups(groups, day) {
  return savePrivateCapture(day, 'farabi-peer-groups.json', {version:'farabi-peer-groups-dom-v1',groups});
}
