import { chromium } from 'playwright';

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext();
const page = await context.newPage();
await page.goto('https://example.com');

// Get the video object
const video = page.video();
console.log('Video object:', video);
console.log('Video methods:', Object.getOwnPropertyNames(Object.getPrototypeOf(video)));
console.log('Video start:', typeof video.start);
console.log('Video stop:', typeof video.stop);
console.log('Video path:', typeof video.path);

// Try to start recording
await page.waitForTimeout(2000);
// video.start({ path: 'test.webm' });
// await page.waitForTimeout(3000);
// video.stop();

await browser.close();
