import { chromium } from 'playwright';

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext();
const page = await context.newPage();
await page.goto('https://example.com');

// Start tracing with screenshots
await page.tracing.start({ path: 'trace.json', screenshots: true, snapshots: true });
console.log('Tracing started');

await page.waitForTimeout(3000);
console.log('Waited 3 seconds');

// Stop tracing
await page.tracing.stop();
console.log('Tracing stopped');

// Save the trace
await context.close();
browser.close();

console.log('Trace saved to trace.json');
