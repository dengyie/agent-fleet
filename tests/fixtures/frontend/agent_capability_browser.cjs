const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const [origin, encoded] = process.argv.slice(2);
(async () => {
  const browser = await chromium.launch({channel:process.env.FLEET_BROWSER_CHANNEL || 'chrome', headless:true});
  try {
    for (const [index, width] of [390, 1440].entries()) {
      const sid = JSON.parse(encoded)[index];
      const page = await browser.newPage({viewport:{width, height:1000}});
      page.setDefaultTimeout(4000);
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      let writes = 0;
      page.on('request', request => {
        if (request.method() === 'POST' && request.url().endsWith('/messages')) writes++;
      });
      await page.goto(origin + '/session/' + sid);
      await page.locator('.session-badges').waitFor();
      await page.locator('[data-role="follow-up"]').fill('继续原生会话 ' + width);
      const response = page.waitForResponse(r => r.request().method() === 'POST' && r.url().endsWith('/messages'));
      await page.getByRole('button', {name:'入队追加回合', exact:true}).click();
      assert.equal((await response).status(), 202);
      assert.equal(writes, 1);
      assert.deepEqual(errors, []);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      await page.close();
    }
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exit(1);});
