const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const [origin] = process.argv.slice(2);
(async () => {
  const browser = await chromium.launch({channel: process.env.FLEET_BROWSER_CHANNEL || 'chrome', headless: true});
  try {
    const page = await browser.newPage(); page.setDefaultTimeout(8000);
    const errors = [], pages = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => { const url = new URL(request.url()); if (url.pathname.endsWith('/conversations/history')) pages.push(url.searchParams.get('before_sequence')); });
    await page.goto(origin + '/conversation/history', {waitUntil: 'domcontentloaded'});
    await page.waitForFunction(() => document.querySelectorAll('.assistant-message.user').length === 20);
    assert.match(await page.locator('.assistant-history-status').textContent(), /较早.*回合/);
    assert.match(await page.locator('.assistant-message.user').first().textContent(), /用户25 /);
    await page.getByRole('button', {name: '加载更早消息', exact: true}).click();
    await page.waitForFunction(() => document.querySelectorAll('.assistant-message.user').length === 40);
    await page.getByRole('button', {name: '加载更早消息', exact: true}).click();
    await page.waitForFunction(() => document.querySelectorAll('.assistant-message.user').length === 45);
    await page.waitForFunction(() => document.querySelector('.assistant-earlier-history').hidden);
    assert.match(await page.locator('.assistant-message.user').first().textContent(), /用户0 /);
    assert.deepEqual(pages.slice(0, 3), [null, '26', '6']);
    // Completing a new Run refreshes the latest page without dropping loaded older rows.
    await page.getByRole('textbox', {name: '给助手的任务'}).fill('最新任务');
    const submitted = page.waitForResponse(response => response.url().endsWith('/turns') && response.status() === 202);
    await page.getByRole('button', {name: '发送任务'}).click();
    await submitted;
    assert.equal((await page.request.post(origin + '/_fixture/finish-history')).status(), 200);
    await page.waitForFunction(() => document.querySelector('.assistant-messages').textContent.includes('最新完成'));
    assert.equal(await page.locator('.assistant-message.user').count(), 46);
    assert.match(await page.locator('.assistant-message.user').first().textContent(), /用户0 /);
    await page.reload({waitUntil: 'domcontentloaded'});
    await page.waitForFunction(() => document.querySelectorAll('.assistant-message.user').length === 20);
    assert.match(await page.locator('.assistant-messages').textContent(), /最新完成/);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
