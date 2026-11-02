import { chromium } from 'playwright';

// Test different recordVideo options
const browser = await chromium.launch({ headless: true });

try {
  const context = await browser.newContext({ recordVideo: true });
  console.log('recordVideo: true - OK');
  await browser.close();
} catch(e) {
  console.log('recordVideo: true - ERROR:', e.message);
}

try {
  const context = await browser.newContext({ recordVideo: { enabled: true } });
  console.log('recordVideo: { enabled: true } - OK');
  await browser.close();
} catch(e) {
  console.log('recordVideo: { enabled: true } - ERROR:', e.message);
}

try {
  const context = await browser.newContext({ recordVideo: { path: '/tmp/test.webm' } });
  console.log('recordVideo: { path: ... } - OK');
  const page = context.pages()[0];
  const video = page.video();
  console.log('Video object:', video);
  await browser.close();
} catch(e) {
  console.log('recordVideo: { path: ... } - ERROR:', e.message);
}
