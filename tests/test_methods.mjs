import { chromium } from 'playwright';

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext();
const page = await context.newPage();
await page.goto('https://example.com');

// Check available methods for video recording
const methods = Object.getOwnPropertyNames(Object.getPrototypeOf(page))
  .filter(m => typeof page[m] === 'function' && m.startsWith('record') || m.startsWith('video'));
console.log('Video-related methods:', methods);

// Try trace instead
await page.tracing.start({ path: 'trace.json', screenshots: true, snapshots: true });
await page.goto('https://example.com');
await page.waitForTimeout(1000);
await page.tracing.stop();
await browser.close();
