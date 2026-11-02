import { chromium } from 'playwright';
async function main() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  const pages = await context.pages();
  console.log('Number of pages:', pages.length);
  console.log('Browser version:', browser.version());
  await browser.close();
}
main().catch(e => console.error('Error:', e));
