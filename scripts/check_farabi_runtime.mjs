/** Offline installation smoke check. It does not open a website or access an account. */
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const modulePath = process.argv[2];
if (!modulePath) throw new Error('Provide the installed Playwright index.mjs path');
const { chromium } = await import(pathToFileURL(path.resolve(modulePath)).href);
const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage();
  await page.setContent('<title>BRS installation check</title><p>Local browser ready</p>');
  if (await page.title() !== 'BRS installation check') throw new Error('Chromium page check failed');
  console.log('Offline Chromium launch passed:', browser.version());
} finally {
  await browser.close();
}
