import { chromium } from 'playwright';

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext();
const page = await context.newPage();
await page.goto('https://example.com');

// List all methods on page prototype
const proto = Object.getPrototypeOf(page);
const methods = Object.getOwnPropertyNames(proto).filter(m => typeof proto[m] === 'function');
console.log('Page methods:', methods.slice(0, 50)); // First 50 methods

await browser.close();
