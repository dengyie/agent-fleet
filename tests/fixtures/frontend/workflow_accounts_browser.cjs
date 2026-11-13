const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const origin = process.argv[2];
const sessionCheck = process.argv[3] || 'current';

(async () => {
  const browser = await chromium.launch({channel: process.env.FLEET_BROWSER_CHANNEL || 'chrome', headless: true});
  try {
    const context = await browser.newContext();
    await context.addInitScript(() => {
      const now = Date.now.bind(Date); let shift = 0;
      Date.now = () => now() + shift;
      window.advanceSessionCheck = () => { shift += 60001; window.dispatchEvent(new Event('focus')); };
    });
    const page = await context.newPage(); page.setDefaultTimeout(10000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    assert.equal((await context.request.get(origin + '/api/platform/v1/workflows')).status(), 401);
    await page.goto(origin + '/assistant', {waitUntil: 'domcontentloaded'});
    await page.waitForURL('**/login?**', {waitUntil: 'domcontentloaded'});
    await page.getByLabel('账号或邮箱', {exact: true}).fill('first@example.test');
    await page.getByLabel('密码', {exact: true}).fill('Workflow-password-123!');
    await page.getByRole('button', {name: '登录', exact: true}).click();
    await page.waitForURL('**/assistant', {waitUntil: 'domcontentloaded'});
    await page.getByRole('tab', {name: '流程', exact: true}).click();
    const panel = page.getByRole('region', {name: '工作流', exact: true});
    await panel.locator('[data-workflow-id="workflow-user-a"]').waitFor();
    await page.route('**/api/platform/v1/workflows/workflow-user-a/pause', async route => {
      assert.equal((await route.fetch()).status(), 202);
      await route.abort('failed');
    });
    await panel.getByRole('button', {name: '暂停流程', exact: true}).click();
    await panel.getByText('结果待确认，请重试上次操作。', {exact: true}).waitFor();
    assert.equal(await panel.getByRole('button', {name: '重试上次操作', exact: true}).count(), 1);
    const anonymous = await browser.newContext();
    assert.equal((await anonymous.request.get(origin + '/api/platform/v1/workflows/workflow-user-a')).status(), 401);
    await anonymous.close();
    const accountSwitchNavigations = [];
    page.on('framenavigated', frame => {
      if (frame === page.mainFrame()) accountSwitchNavigations.push(new URL(frame.url()).pathname);
    });
    let releaseStale = () => {};
    if (sessionCheck !== 'current') {
      let staleFetched;
      const staleReady = new Promise(resolve => { staleFetched = resolve; });
      const staleRelease = new Promise(resolve => { releaseStale = resolve; });
      const featureResponse = sessionCheck === 'held-feature-unauthorized';
      await page.route(featureResponse ? '**/api/platform/v1/conversations?limit=50' : '**/api/operator/session', async route => {
        const oldResponse = await route.fetch();
        staleFetched();
        await staleRelease;
        if (sessionCheck === 'held-unauthorized' || featureResponse) {
          await route.fulfill({status: 401, contentType: 'application/json', body: JSON.stringify({error: 'unauthorized'})});
        } else await route.fulfill({response: oldResponse});
      }, {times: 1});
      if (featureResponse) {
        await page.evaluate(() => {
          window.heldFeatureRequest = import('/assets/api/platform.js')
            .then(api => api.getConversations(50)).catch(() => {});
        });
      } else await page.evaluate(() => window.advanceSessionCheck());
      await staleReady;
    }
    assert.equal((await context.request.post(origin + '/api/accounts/logout', {data: {}, headers: {Origin: origin}})).status(), 200);
    assert.equal((await context.request.post(origin + '/api/accounts/login', {data: {email: 'second@example.test', password: 'Workflow-password-123!'}, headers: {Origin: origin}})).status(), 200);
    await Promise.all([
      page.waitForEvent('domcontentloaded'),
      page.evaluate(() => window.advanceSessionCheck()).then(() => releaseStale())
    ]);
    await page.getByText('就绪', {exact: true}).waitFor();
    await page.getByRole('tab', {name: '流程', exact: true}).click();
    await panel.locator('[data-workflow-id="workflow-user-b"]').waitFor();
    assert.equal(await panel.locator('[data-workflow-id="workflow-user-a"]').count(), 0);
    assert.equal(await panel.getByRole('button', {name: '重试上次操作', exact: true}).count(), 0);
    assert.equal((await context.request.get(origin + '/api/platform/v1/workflows/workflow-user-a')).status(), 404);
    const foreignWrite = await context.request.post(origin + '/api/platform/v1/workflows/workflow-user-a/cancel',
      {data: {expected_revision: 1, client_token: 'foreign-cancel'}, headers: {Origin: origin}});
    assert.equal(foreignWrite.status(), 404);
    await page.reload({waitUntil: 'domcontentloaded'});
    await page.getByRole('tab', {name: '流程', exact: true}).click();
    await panel.locator('[data-workflow-id="workflow-user-b"][aria-pressed="true"]').waitFor();
    assert.equal(await panel.getByRole('button', {name: '重试上次操作', exact: true}).count(), 0);
    assert.equal((await context.request.post(origin + '/api/accounts/logout', {data: {}, headers: {Origin: origin}})).status(), 200);
    assert.equal((await context.request.post(origin + '/api/accounts/login', {data: {email: 'basic@example.test', password: 'Workflow-password-123!'}, headers: {Origin: origin}})).status(), 200);
    await Promise.all([page.waitForEvent('domcontentloaded'), page.evaluate(() => window.advanceSessionCheck())]);
    await page.getByText('就绪', {exact: true}).waitFor();
    assert.equal(await page.getByRole('tab', {name: '流程', exact: true}).count(), 0, 'ordinary account roles retain the existing operator boundary');
    assert.equal((await context.request.get(origin + '/api/platform/v1/workflows')).status(), 403);
    assert.ok(!accountSwitchNavigations.includes('/login'), 'stale session responses must not redirect the new account to login');
    assert.deepEqual(errors, []);
    await context.close();
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
