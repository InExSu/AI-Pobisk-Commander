import { chromium } from 'playwright';

const url = process.argv[2] || 'https://www.igrasu.ru/games/U-X/V/velo_Storm_Spiders/';

console.log('Target URL:', url);

const browser = await chromium.launch({ headless: false, args: ['--no-sandbox', '--disable-web-security'] });
console.log('Browser launched');

const context = await browser.newContext({ viewport: { width: 1280, height: 720 } });
const page = await context.newPage();

console.log('Navigating to game...');
await page.goto(url, { waitUntil: 'networkidle' });
console.log('Page loaded');

await page.waitForTimeout(5000);

await browser.close();
console.log('Done');
