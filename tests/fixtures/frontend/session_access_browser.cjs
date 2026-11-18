const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const [origin, scenario, oldSid, sid] = process.argv.slice(2);
(async () => {
  const browser = await chromium.launch({channel: process.env.FLEET_BROWSER_CHANNEL || 'chrome', headless:true});
  try {
    for (const width of scenario === 'long_receipt' ? [1440] : [1440, 390]) {
      const page = await browser.newPage({viewport:{width, height:1000}});
      page.setDefaultTimeout(8000);
      const errors = [];
      page.on('pageerror', e => errors.push(e.message));
      if (scenario === 'authority') {
        await page.goto(origin + '/session/' + oldSid);
        await page.locator('.session-badges').waitFor();
        assert.equal(await page.getByRole('button', {name:'暂停会话', exact:true}).count(), 0);
        assert.match(await page.locator('.session-badges').innerText(), /已撤销/);
        assert.equal(await page.locator('[data-role="follow-up"]').count(), 0);
        assert.equal(await page.locator('.session-question button').first().isDisabled(), true);
        await page.goto(origin + '/session/' + sid);
        await page.locator('.session-badges').waitFor();
        assert.equal(await page.getByRole('button', {name:'暂停会话', exact:true}).isEnabled(), true);
        assert.equal(await page.locator('[data-role="follow-up"]').count(), 1);
      } else {
        await page.goto(origin + '/session/' + sid);
        await page.getByRole('button', {name:'下一页', exact:true}).click();
        await page.getByText('Access event 103', {exact:true}).waitFor();
        let posts = 0, eventReads = 0;
        let receiptReads = 0;
        let releaseReads, delayedReads;
        if (scenario === 'admission_feedback') {
          delayedReads = new Promise(resolve => { releaseReads = resolve; });
          await page.route('**/api/supervisor/receipts/*', async route => {
            await delayedReads;
            await route.continue();
          });
        }
        let releaseFirst, firstLoaded, firstFinished;
        if (scenario === 'resume_inflight') {
          let markLoaded, markFinished;
          firstLoaded = new Promise(resolve => { markLoaded = resolve; });
          firstFinished = new Promise(resolve => { markFinished = resolve; });
          const release = new Promise(resolve => { releaseFirst = resolve; });
          await page.route('**/api/supervisor/receipts/*', async route => {
            receiptReads++;
            if (receiptReads !== 1) { await route.continue(); return; }
            const response = await route.fetch();
            markLoaded();
            await release;
            await route.fulfill({response});
            markFinished();
          });
        }
        if (['receipt_retry', 'refresh_failure', 'long_receipt'].includes(scenario)) {
          await page.route('**/api/supervisor/receipts/*', async route => {
            receiptReads++;
            if (scenario === 'long_receipt' && receiptReads <= 14) {
              const response = await route.fetch();
              const body = await response.json();
              body.receipt.status = 'executing';
              await route.fulfill({response, json:body});
            } else if (receiptReads === 1) await route.fulfill({status:503, json:{error:'temporarily_unavailable'}});
            else await route.continue();
          });
        }
        page.on('request', req => { if (req.method() === 'POST' && /\/control$/.test(req.url())) posts++; });
        await page.route('**/api/sessions/' + sid + '/events?*', async route => {
          eventReads++;
          if (delayedReads) await delayedReads;
          if (scenario === 'refresh_failure') await route.fulfill({status:503, json:{error:'session_unavailable'}});
          else await route.continue();
        });
        const refreshed = page.waitForResponse(r => r.url().includes('/events?'));
        refreshed.catch(() => {}); // Observed below; teardown may precede a delayed response.
        await page.getByRole('button', {name:'暂停会话', exact:true}).click();
        try {
          await page.waitForFunction(() => /已入队|控制回执/.test(document.body.innerText), null, {timeout:3000});
        } finally { if (releaseReads) releaseReads(); }
        if (['resume', 'receipt_retry', 'long_receipt', 'resume_inflight'].includes(scenario)) {
          if (scenario === 'resume_inflight') {
            await firstLoaded;
            await page.evaluate(() => window.dispatchEvent(new CustomEvent('fleet:navigate', {detail:{path:'/monitoring'}})));
            await page.waitForURL('**/monitoring');
            await page.request.post(origin + '/api/supervisor/poll', {headers:{'X-Supervisor-Credential':'m1:browser-receipt'}, data:{}});
            await page.evaluate(sid => window.dispatchEvent(new CustomEvent('fleet:navigate', {detail:{path:'/session/' + sid}})), sid);
            await page.waitForFunction(() => document.body.innerText.includes('控制回执：delivered'));
            releaseFirst();
            await firstFinished;
            await page.evaluate(() => new Promise(resolve => setTimeout(resolve, 100)));
            assert.match(await page.locator('body').innerText(), /控制回执：delivered/, 'old same-command response cannot regress the resumed receipt');
          } else if (scenario === 'resume') {
          await page.evaluate(() => window.dispatchEvent(new CustomEvent('fleet:navigate', {detail:{path:'/settings/general'}})));
          await page.getByRole('button', {name:'关闭设置', exact:true}).click();
          await page.evaluate(() => window.dispatchEvent(new CustomEvent('fleet:navigate', {detail:{path:'/monitoring'}})));
          await page.waitForURL('**/monitoring');
          await page.evaluate(sid => window.dispatchEvent(new CustomEvent('fleet:navigate', {detail:{path:'/session/' + sid}})), sid);
          }
          await page.request.post(origin + '/api/supervisor/poll', {headers:{'X-Supervisor-Credential':'m1:browser-receipt'}, data:{}});
          await page.waitForFunction(() => document.body.innerText.includes('控制回执：delivered'), null, {timeout:35000});
        } else {
          // Wait for the post-admission read to finish, including the error branch.
          await page.waitForFunction(() => !document.body.innerText.includes('正在入队'));
          await refreshed;
          await page.evaluate(() => new Promise(resolve => setTimeout(resolve, 100)));
          assert.ok(eventReads > 0);
          assert.ok(await page.getByText('Access event 103', {exact:true}).count(), 'keep the current event page');
          assert.equal(await page.getByText('Access event 1', {exact:true}).count(), 0);
          assert.equal(await page.getByRole('button', {name:'下一页', exact:true}).isDisabled(), true);
          assert.match(await page.locator('body').innerText(), /已入队|控制回执/);
          assert.equal(await page.getByText('控制失败', {exact:true}).count(), 0);
        }
        assert.equal(posts, 1, 'one user action admits one command');
      }
      assert.deepEqual(errors, []);
      await page.screenshot({path:'/tmp/fleet-access-' + scenario + '-' + width + '.png', fullPage:true});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false,
        JSON.stringify({size:await page.evaluate(() => ({viewport:innerWidth, document:document.documentElement.scrollWidth, body:document.body.scrollWidth})), nodes:await page.locator('body *').evaluateAll(nodes => nodes.filter(n => {
          const r = n.getBoundingClientRect(); return r.width > 0 && (r.right > innerWidth || n.scrollWidth > n.clientWidth + 1);
        }).slice(0, 12).map(n => ({tag:n.tagName, cls:n.className, text:n.innerText?.slice(0,80), scroll:n.scrollWidth, client:n.clientWidth}))) }));
      await page.close();
    }
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exit(1);});
