const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const [origin, mode, viewport] = process.argv.slice(2);
const prefix = origin + '/api/platform/v1/integrations/mcp';
const password = 'safe-password-123!';

async function capture(page, name) {
  if (!process.env.FLEET_SCREENSHOTS) return;
  await fs.mkdir(process.env.FLEET_SCREENSHOTS, {recursive: true});
  await page.screenshot({path: path.join(process.env.FLEET_SCREENSHOTS, name + '.png'), fullPage: true});
}

(async () => {
  const browser = await chromium.launch({channel: process.env.FLEET_BROWSER_CHANNEL || 'chrome', headless: true});
  try {
    const context = await browser.newContext({viewport: viewport === 'mobile' ? {width: 390, height: 844} : {width: 1440, height: 1000}});
    await context.addInitScript(() => {
      const clock = Date.now.bind(Date); let shift = 0;
      Date.now = () => clock() + shift;
      window.checkNewAccount = () => { shift += 60001; window.dispatchEvent(new Event('focus')); };
    });
    const login = await context.request.post(origin + '/api/accounts/login', {data: {email: 'admin@example.test', password}, headers: {Origin: origin}});
    assert.equal(login.status(), 200);
    const page = await context.newPage(); page.setDefaultTimeout(10000);
    const errors = []; const requests = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => { if (request.url().startsWith(prefix)) requests.push({url: request.url(), method: request.method()}); });
    await page.goto(origin + '/account', {waitUntil: 'domcontentloaded'});
    await page.getByRole('heading', {name: '个人资料', exact: true}).waitFor();
    const panel = page.getByRole('region', {name: 'MCP 集成管理', exact: true});
    if (mode === 'hidden') {
      assert.equal(await panel.count(), 0); assert.equal(requests.length, 0);
      assert.equal((await context.request.get(prefix)).status(), 404);
    } else if (mode === 'empty') {
      await panel.getByText('尚无连接登记', {exact: true}).waitFor();
      assert.equal(await panel.getByRole('button', {name: '加载更多连接', exact: true}).isVisible(), false);
    } else {
      const inspector = panel.getByRole('region', {name: 'MCP 连接详情', exact: true});
      const row = id => panel.locator('[data-connector-id]').filter({has: page.getByText(id, {exact: true})});
      await row('.').waitFor();
      await row('.').click(); await inspector.getByRole('heading', {name: '.', exact: true}).waitFor();
      await row('b-unattached').click(); await inspector.getByText('本进程未附着', {exact: true}).waitFor();
      await inspector.getByText('暂不提供工具', {exact: true}).waitFor();
      await row('d-restored').click(); await inspector.getByText('恢复后禁用', {exact: true}).waitFor();
      assert.equal(await inspector.getByRole('button', {name: '撤销连接', exact: true}).count(), 0);
      await panel.getByRole('button', {name: '加载更多连接', exact: true}).click();
      await row('weather').click();
      await inspector.getByText('待审版本 3', {exact: true}).waitFor();
      await inspector.locator('pre').filter({hasText: '<img src=x onerror='}).waitFor();
      assert.equal(await inspector.locator('img,a,script').count(), 0);
      assert.equal(await page.evaluate(() => window.mcpXss), undefined);
      const text = await panel.textContent();
      for (const privateText of ['secret://mcp', 'PRIVATE_PATH', 'PRIVATE_QUERY', 'secret-token', 'config_snapshot']) assert.equal(text.includes(privateText), false);
      const approve = inspector.getByRole('button', {name: '批准待审版本', exact: true});
      if (mode === 'review') {
        await capture(page, 'mcp-admin-pending-' + viewport);
        await approve.click(); const dialog = page.getByRole('dialog', {name: '批准待审版本', exact: true});
        assert.equal(await dialog.getByRole('button', {name: '确认批准', exact: true}).isDisabled(), true);
        await capture(page, 'mcp-admin-confirm-' + viewport);
        await page.keyboard.press('Escape'); await dialog.waitFor({state: 'detached'});
        assert.equal(await approve.evaluate(element => element === document.activeElement), true);
        await approve.click(); await dialog.getByLabel('已核对待审定义', {exact: true}).check();
        await dialog.getByRole('button', {name: '确认批准', exact: true}).click();
        await panel.getByText('待审版本已批准', {exact: true}).waitFor();
        assert.equal(await inspector.getByRole('button', {name: '批准待审版本', exact: true}).count(), 0);
        await inspector.getByText('已批准版本 3', {exact: true}).waitFor();
        await inspector.getByRole('button', {name: '撤销连接', exact: true}).click();
        const revoke = page.getByRole('dialog', {name: '撤销连接', exact: true});
        await revoke.getByText('撤销不可恢复。', {exact: false}).waitFor();
        await revoke.getByRole('button', {name: '确认撤销', exact: true}).click();
        await panel.getByText('连接已撤销', {exact: true}).waitFor();
        await page.reload({waitUntil: 'domcontentloaded'});
        await panel.getByRole('button', {name: '加载更多连接', exact: true}).click();
        await row('weather').click(); await inspector.getByText('已撤销', {exact: true}).waitFor();
        assert.equal(await inspector.getByRole('button', {name: '撤销连接', exact: true}).count(), 0);
      } else if (mode === 'recovery') {
        const admitPattern = '**/integrations/mcp/admit?**';
        await page.route(admitPattern, async route => { assert.equal((await route.fetch()).status(), 200); await route.abort('failed'); });
        await approve.click(); const dialog = page.getByRole('dialog');
        await dialog.getByLabel('已核对待审定义', {exact: true}).check();
        await dialog.getByRole('button', {name: '确认批准', exact: true}).click();
        await panel.getByText('操作结果待核对，请重新读取目录。请勿重复提交。', {exact: true}).waitFor();
        assert.equal(await inspector.getByRole('button').count(), 0);
        assert.equal(requests.filter(value => value.method === 'POST').length, 1);
        await page.unroute(admitPattern); await panel.getByRole('button', {name: '重新读取目录', exact: true}).click();
        await inspector.getByText('已批准版本 3', {exact: true}).waitFor();
        assert.equal(requests.filter(value => value.method === 'POST').length, 1);
        const revokePattern = '**/integrations/mcp/revoke?**';
        await page.route(revokePattern, async route => {
          const body = route.request().postDataJSON();
          const response = await context.request.post(prefix + '/revoke?connector_id=weather', {data: body, headers: {Origin: origin}});
          assert.equal(response.status(), 200); await route.continue();
        });
        await inspector.getByRole('button', {name: '撤销连接', exact: true}).click();
        await page.getByRole('dialog').getByRole('button', {name: '确认撤销', exact: true}).click();
        await panel.getByText('目录状态已变化，已读取当前定义，请重新审阅', {exact: true}).waitFor();
        await inspector.getByText('已撤销', {exact: true}).waitFor();
        assert.equal(requests.filter(value => value.method === 'POST').length, 2);
        assert.equal(await inspector.getByRole('button').count(), 0);
        await page.unroute(revokePattern);
      } else if (mode === 'lifecycle') {
        await approve.click();
        await page.evaluate(() => window.dispatchEvent(new CustomEvent('fleet:navigate', {detail: {path: '/assistant'}})));
        await page.waitForURL('**/assistant'); assert.equal(await page.getByRole('dialog').count(), 0);
        await page.locator('a[data-nav="account"]').click();
        await inspector.getByText('待审版本 3', {exact: true}).waitFor();
        assert.equal(requests.filter(value => value.method === 'POST').length, 0);
        let held, signalRead;
        const arrived = new Promise(resolve => { signalRead = resolve; });
        const readPattern = '**/integrations/mcp/record?connector_id=weather';
        await page.route(readPattern, route => { held = route; signalRead(); });
        await panel.getByRole('button', {name: '重新读取目录', exact: true}).click(); await arrived;
        await page.locator('a[data-nav="assistant"]').click(); await page.waitForURL('**/assistant');
        await held.abort('failed'); await page.unroute(readPattern);
        await page.locator('a[data-nav="account"]').click();
        await inspector.getByText('待审版本 3', {exact: true}).waitFor();
        assert.equal(requests.filter(value => value.method === 'POST').length, 0);
        assert.equal((await context.request.post(origin + '/api/accounts/logout', {data: {}, headers: {Origin: origin}})).status(), 200);
        assert.equal((await context.request.post(origin + '/api/accounts/login', {data: {email: 'ordinary@example.test', password}, headers: {Origin: origin}})).status(), 200);
        await Promise.all([page.waitForEvent('domcontentloaded'), page.evaluate(() => window.checkNewAccount())]);
        await page.getByText('ordinary@example.test · 普通用户', {exact: true}).waitFor();
        assert.equal(await panel.count(), 0);
        assert.equal((await context.request.get(prefix)).status(), 403);
        assert.equal((await context.request.post(prefix + '/revoke?connector_id=weather', {data: {expected_epoch: 4}, headers: {Origin: origin}})).status(), 403);
      }
      await capture(page, 'mcp-admin-' + mode + '-' + viewport);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false, 'page must fit viewport');
    }
    assert.deepEqual(errors, []); await context.close();
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
