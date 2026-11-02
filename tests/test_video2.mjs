import { chromium } from 'playwright';

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ persist: false });
const page = await context.newPage();
await page.goto('https://example.com');

// Get the video object - this should be lazy now
const video = page.video();
console.log('Video object after navigation:', video);
if (video) {
  console.log('Video start type:', typeof video.start);
  console.log('Video stop type:', typeof video.stop);
  console.log('Video path type:', typeof video.path);
}

await page.waitForTimeout(2000);

await browser.close();
