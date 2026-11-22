const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const [origin, viewport] = process.argv.slice(2);

(async () => {
  const mobile = viewport === 'mobile';
  const browser = await chromium.launch({channel: process.env.FLEET_BROWSER_CHANNEL || 'chrome', headless: true});
  try {
    const page = await browser.newPage({viewport: mobile ? {width: 390, height: 844} : {width: 1440, height: 1000}});
    page.setDefaultTimeout(10000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    for (const route of ['/', '/assistant', '/assistant?view=computer', '/assistant?view=sessions',
      '/muse', '/monitoring', '/plugins', '/session-search', '/usage', '/settings', '/settings/account']) {
      await page.goto(origin + route);
      await page.locator('.console-layout:not([hidden])').waitFor();
      await page.locator('#route-view').waitFor();
      await page.waitForTimeout(120);
      const dimensions = await page.evaluate(() => ({
        viewport: innerWidth,
        document: document.documentElement.scrollWidth,
        content: document.querySelector('.console-content').scrollWidth,
        contentWidth: document.querySelector('.console-content').clientWidth,
      }));
      assert.ok(dimensions.document <= dimensions.viewport + 1, route + ' document overflow: ' + JSON.stringify(dimensions));
      assert.ok(dimensions.content <= dimensions.contentWidth + 1, route + ' content overflow: ' + JSON.stringify(dimensions));
      if (route === '/usage') {
        assert.ok(await page.locator('.usage-filters').isVisible());
        if (mobile) {
          assert.equal(await page.locator('.usage-filters').evaluate(node => getComputedStyle(node).gridTemplateColumns.split(' ').length), 1);
        }
      }
      if (route === '/monitoring' && mobile) {
        assert.equal(await page.locator('.monitoring-summary').evaluate(node => getComputedStyle(node).gridTemplateColumns.split(' ').length), 1);
      }
      if (process.env.FLEET_SCREENSHOTS && ['/', '/assistant', '/assistant?view=computer',
        '/assistant?view=sessions', '/monitoring', '/usage', '/plugins'].includes(route)) {
        await fs.mkdir(process.env.FLEET_SCREENSHOTS, {recursive: true});
        await page.screenshot({path: path.join(process.env.FLEET_SCREENSHOTS, route.replace(/[^a-z]/gi, '_') + '-' + viewport + '.png'), fullPage: true});
      }
    }
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
