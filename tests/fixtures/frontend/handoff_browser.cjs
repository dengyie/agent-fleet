const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const [origin] = process.argv.slice(2);
(async () => {
  const browser = await chromium.launch({channel: process.env.FLEET_BROWSER_CHANNEL || 'chrome', headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    for (const width of [1440, 390]) {
      await page.setViewportSize({width, height: 1000});
      await page.goto(origin + '/session/source-session');
      const panel = page.locator('[data-handoff-panel]');
      await panel.getByRole('button', {name:'预览接力包', exact:true}).click();
      await panel.getByRole('button', {name:'确认创建接力任务', exact:true}).waitFor();
      assert.ok(!(await panel.textContent()).includes('must-not-leak'));
      await panel.locator('textarea').fill('Continue testing at width ' + width);
      assert.ok((await panel.locator('[data-handoff-final-prompt]').textContent()).includes('Repair agent dispatch'));
      const sent = [];
      let failures = 1;
      await page.route('**/api/sessions/source-session/handoff', async route => {
        if (route.request().method() !== 'POST') { await route.continue(); return; }
        sent.push(route.request().postDataJSON());
        if (failures-- > 0) {
          await route.fetch();
          await route.abort('failed');
          return;
        }
        await route.continue();
      });
      await panel.getByRole('button', {name:'确认创建接力任务', exact:true}).click();
      await panel.getByRole('button', {name:'使用原凭据重试', exact:true}).waitFor();
      assert.equal(await panel.locator('textarea').isDisabled(), true);
      await panel.getByRole('button', {name:'使用原凭据重试', exact:true}).click();
      await panel.getByRole('link', {name:/打开新任务/}).waitFor();
      assert.deepEqual(sent[0], sent[1]);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      const target = await panel.getByRole('link', {name:/打开新任务/}).getAttribute('href');
      await page.goto(origin + target);
      await page.getByText(/待人工确认/).waitFor();
      await page.unroute('**/api/sessions/source-session/handoff');
    }
    await page.goto(origin + '/session/unmanaged-session');
    const panel = page.locator('[data-handoff-panel]');
    await panel.getByRole('button', {name:'预览接力包', exact:true}).click();
    await panel.getByText(/当前会话仅可预览/).waitFor();
    assert.equal(await panel.getByRole('button', {name:'确认创建接力任务', exact:true}).count(), 0);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
