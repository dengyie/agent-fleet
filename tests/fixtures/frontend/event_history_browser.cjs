const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const [origin, conversation, state] = process.argv.slice(2);
(async () => {
  const browser = await chromium.launch({channel: process.env.FLEET_BROWSER_CHANNEL || 'chrome', headless: true});
  try {
    const page = await browser.newPage();
    page.setDefaultTimeout(6000);
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.goto(origin + '/conversation/' + conversation, {waitUntil: 'domcontentloaded'});
    await page.locator('[data-command-id="pagination-104"]').waitFor({state: 'attached'});
    if (state === 'running') {
      assert.equal((await page.request.post(origin + '/_fixture/finish')).status(), 200);
    }
    await page.waitForFunction(() => document.querySelector('.assistant-run-status')?.textContent === '已完成');
    await page.waitForFunction(() => document.querySelector('.assistant-events')?.textContent.includes('运行结束'));
    assert.equal(await page.locator('.assistant-events tool-call-badge').count(), 105);
    await page.reload({waitUntil: 'domcontentloaded'});
    await page.locator('[data-command-id="pagination-104"]').waitFor({state: 'attached'});
    assert.equal(await page.locator('.assistant-events tool-call-badge').count(), 105);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
