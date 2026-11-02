import { chromium } from 'playwright';

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext();
const page = await context.newPage();
await page.goto('https://example.com');

// Check the video method
console.log('page.video type:', typeof page.video);
console.log('page.video length:', page.video.length);
try {
  console.log('page.video:', page.video.toString().substring(0, 200));
} catch(e) {
  console.error('Error:', e.message);
}

await browser.close();
