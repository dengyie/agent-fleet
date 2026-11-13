const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const fs = require('node:fs');
const origin = process.argv[2];
(async () => {
  const browser = await chromium.launch({channel: process.env.FLEET_BROWSER_CHANNEL || 'chrome', headless: true});
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    fs.mkdirSync('/tmp/agent-fleet-management-evidence', {recursive: true});
    for (const width of [390, 1440]) {
      await page.setViewportSize({width, height: 1000});
      await page.goto(origin + '/session/questions-' + width, {waitUntil: 'domcontentloaded'});
      await page.locator('.session-agent-state').filter({hasText:'等待用户回答'}).waitFor();
      for (const kind of ['single', 'multi', 'text', 'confirm']) {
        const card = page.locator('[data-request-id="' + kind + '"]');
        if (kind === 'text') await card.getByLabel('自定义回答').fill('继续执行');
        else await card.getByRole('button', {name:'选项甲', exact:true}).click();
        if (kind === 'multi') {
          await card.getByRole('button', {name:'选项乙', exact:true}).click();
          await card.getByRole('button', {name:'确认 2 项'}).click();
        }
        if (kind === 'text') await card.getByRole('button', {name:'提交回答'}).click();
        await card.getByRole('status').filter({hasText:'queued'}).waitFor();
        const command = await card.locator('p.meta').last().innerText();
        const delivery = await page.request.post(origin + '/api/supervisor/poll', {
          headers:{'X-Supervisor-Credential':'m1:browser-receipt'}, data:{}
        });
        assert.equal(delivery.status(), 200);
        await card.getByRole('status').filter({hasText:'delivered'}).waitFor();
        for (const status of ['executing', 'succeeded']) {
          const response = await page.request.post(origin + '/api/supervisor/receipts', {
            headers:{'X-Supervisor-Credential':'m1:browser-receipt'}, data:{command_id:command, status}
          });
          assert.equal(response.status(), 200);
          await card.getByRole('status').filter({hasText:status}).waitFor();
        }
        assert.equal(await card.getByRole('button').first().isDisabled(), true);
      }
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      await page.screenshot({path:`/tmp/agent-fleet-management-evidence/questions-${width}.png`, fullPage:true});
      await page.goto(origin + '/session/questions-' + width + '?sequence=5', {waitUntil: 'domcontentloaded'});
      await page.locator('#session-event-5.session-event-match').waitFor();
      await page.waitForTimeout(100);
      assert.equal(await page.locator('#session-event-5').evaluate(node => document.activeElement === node), true);
    }
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exit(1);});
