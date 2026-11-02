import { chromium } from 'playwright';

// Try with video recording setup in context options
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ video: 'all' });  // Enable video recording
const page = await context.newPage();
await page.goto('https://example.com');

const video = page.video();
console.log('Video from page.video() with video:all:', video);
if (video) {
  console.log('Video start type:', typeof video.start);
  console.log('Video stop type:', typeof video.stop);
}

await page.waitForTimeout(2000);

await browser.close();
