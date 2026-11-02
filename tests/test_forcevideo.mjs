import { chromium } from 'playwright';

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext();
const page = await context.newPage();
await page.goto('https://example.com');

// Check _forceVideo method
console.log('_forceVideo type:', typeof page._forceVideo);
try {
  console.log('_forceVideo:', page._forceVideo.toString().substring(0, 300));
} catch(e) {
  console.error('Error:', e.message);
}

// Check video method
console.log('video type:', typeof page.video);
try {
  console.log('video:', page.video.toString().substring(0, 300));
} catch(e) {
  console.error('Error:', e.message);
}

await browser.close();
