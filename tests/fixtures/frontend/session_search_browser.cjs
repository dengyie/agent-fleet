const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const origin = process.argv[2];
(async () => {
  const browser = await chromium.launch({channel: process.env.FLEET_BROWSER_CHANNEL || 'chrome', headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    page.setDefaultTimeout(7000);
    const errors = [], assets = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('response', response => {
      if (response.url().includes('/assets/') && response.status() >= 400) assets.push(response.url());
    });
    const output = process.env.FLEET_SCREENSHOTS || '/tmp/agent-fleet-search-evidence';
    fs.mkdirSync(output, {recursive: true});
    for (const width of [1440, 390]) {
      await page.setViewportSize({width, height: 900});
      await page.goto(origin + '/session-search');
      await page.getByRole('searchbox', {name: '搜索内容', exact: true}).fill('登录');
      await page.getByLabel('机器', {exact: true}).fill('search-node');
      await page.getByLabel('项目', {exact: true}).fill('search-project');
      await page.getByLabel('Agent', {exact: true}).fill('codex');
      await page.getByRole('button', {name: '搜索', exact: true}).click();
      const link = page.locator('.session-search-result a').first();
      await link.waitFor();
      assert.equal(await link.getAttribute('href'), '/session/search-session?sequence=143');
      assert.equal(await page.locator('.session-search-result mark').textContent(), '登录');
      assert.equal(await page.locator('.session-search-result img').count(), 0);
      assert.equal(await page.evaluate(() => window.injected), undefined);
      assert.equal((await page.locator('.session-search-results').textContent()).includes('browsersecretvalue'), false);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      await page.screenshot({path: path.join(output, `search-${width}.png`), fullPage: true});
      await link.click();
      await page.locator('#session-event-143').waitFor();
      assert.equal(await page.locator('#session-event-143').isVisible(), true);
      assert.ok((await page.locator('#session-event-143').textContent()).includes('登录完成'));
    }
    await page.goto(origin + '/session-search');
    await page.getByRole('searchbox', {name: '搜索内容', exact: true}).fill('记录');
    await page.getByRole('button', {name: '搜索', exact: true}).click();
    await page.getByText('本页 50 条结果', {exact: true}).waitFor();
    const first = await page.locator('.session-search-result a').first().getAttribute('href');
    await page.getByRole('button', {name: '下一页', exact: true}).click();
    await page.waitForFunction(first => document.querySelector('.session-search-result a')?.getAttribute('href') !== first, first);
    assert.equal(await page.locator('.session-search-result').count(), 50);
    await page.getByRole('searchbox', {name: '搜索内容', exact: true}).fill('超大');
    await page.getByRole('button', {name: '搜索', exact: true}).click();
    const large = page.locator('.session-search-result a').first();
    await page.waitForFunction(() => document.querySelector('.session-search-result a')?.getAttribute('href')?.includes('sequence=9007199254740993'));
    await large.click();
    await page.locator('#session-event-9007199254740993').waitFor();
    assert.equal(await page.locator('#session-event-9007199254740993 .session-event-seq').textContent(), '#9007199254740993');
    await page.goto(origin + '/session-search');
    await page.getByRole('searchbox', {name: '搜索内容', exact: true}).fill('登录');
    await page.route('**/api/session-search?**', route => route.fulfill({status: 503, json: {ok: false, error: 'search_disabled'}}));
    await page.getByRole('button', {name: '搜索', exact: true}).click();
    await page.getByText('会话搜索尚未启用。', {exact: true}).waitFor();
    assert.equal(await page.locator('.session-search-result').count(), 0);
    assert.deepEqual(errors, []);
    assert.deepEqual(assets, []);
    console.log('Search desktop/mobile, Chinese match, filters, pagination, safe text and sequence links passed');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
