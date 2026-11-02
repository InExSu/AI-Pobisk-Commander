import { chromium } from 'playwright';

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({});  // Try with empty options
const page = await context.newPage();
await page.goto('https://example.com');

const video = page.video();
console.log('Video from page.video():', video);

// Try context.video()
const contextVideo = context.video();
console.log('Video from context.video():', contextVideo);

await browser.close();
