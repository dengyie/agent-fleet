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
    await page.goto(origin + '/session/cursor-session');
    await page.getByText('Cursor message 0', {exact:true}).waitFor();
    assert.equal(await page.locator('.session-timeline > li').count(), 100);
    assert.equal(await page.locator('.session-timeline > li').evaluateAll(rows => new Set(rows.map(r => r.id)).size), 100);
    await page.getByRole('button', {name:'下一页', exact:true}).click();
    await page.getByText('Cursor message 101', {exact:true}).waitFor();
    assert.equal(await page.getByRole('button', {name:'下一页', exact:true}).isDisabled(), true);
    const late = await page.request.post(origin + '/api/session-events', {
      headers:{'X-Agent-Fleet-Token':'browser-ingest'}, data:[{
        schema_version:1, event_id:'cursor-late', session_id:'cursor-session', machine_id:'m1',
        process_group_id:'g1', stream_id:'late-stream', sequence:1, kind:'assistant_message',
        capture_quality:'structured', source:'hooks', emitted_at:'2026-11-17T00:00:01Z',
        payload:{text:'Late stream message', is_complete:true}
      }]
    });
    assert.equal(late.status(), 200);
    assert.deepEqual((await late.json()).rejected, []);
    await page.getByText('Late stream message', {exact:true}).waitFor();
    // Returning to a page re-reads its durable cursor; source sequence is never
    // used as a pagination offset, including after reload and deep links.
    await page.getByRole('button', {name:'上一页', exact:true}).click();
    await page.getByText('Cursor message 0', {exact:true}).waitFor();
    await page.getByRole('button', {name:'下一页', exact:true}).click();
    await page.getByText('Late stream message', {exact:true}).waitFor();
    await page.reload();
    await page.getByText('Cursor message 0', {exact:true}).waitFor();
    await page.getByRole('button', {name:'下一页', exact:true}).click();
    await page.getByText('Late stream message', {exact:true}).waitFor();
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exit(1);});
