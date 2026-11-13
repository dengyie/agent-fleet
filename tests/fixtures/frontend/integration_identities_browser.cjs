const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const [origin, mode, viewport] = process.argv.slice(2);
const prefix = origin + '/api/platform/v1/integrations/fleet';
const password = 'safe-password-123!';
const selector = '?owner_id=' + encodeURIComponent('z 租户 / ?#') + '&integration_id=' + encodeURIComponent('../记录?x#');

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
    assert.equal((await context.request.post(origin + '/api/accounts/login', {data: {email: 'admin@example.test', password}, headers: {Origin: origin}})).status(), 200);
    const page = await context.newPage(); page.setDefaultTimeout(10000);
    // Hold optional provider requests to expose accidental eager loading.
    // Authenticated account navigation must work without those services.
    let captchaRequests = 0;
    if (mode === 'lifecycle') {
      await page.route(/^https:\/\/(?:js\.hcaptcha\.com|challenges\.cloudflare\.com)\//, () => { captchaRequests++; });
    }
    const errors = [], requests = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => { if (request.url().startsWith(prefix)) requests.push({url: request.url(), method: request.method()}); });
    await page.goto(origin + '/settings/integrations', {waitUntil: 'domcontentloaded'});
    await page.getByRole('navigation', {name: '设置分类', exact: true}).waitFor();
    const panel = page.getByRole('region', {name: 'Fleet 集成身份管理', exact: true});
    if (mode === 'hidden') {
      assert.equal(await panel.count(), 0); assert.equal(requests.length, 0);
      assert.equal((await context.request.get(prefix)).status(), 404);
    } else if (mode === 'empty' || mode === 'both') {
      await panel.getByText('尚无身份登记', {exact: true}).waitFor();
      assert.equal(await panel.getByRole('button', {name: '加载更多身份', exact: true}).isVisible(), false);
      if (mode === 'both') {
        const mcp = page.getByRole('region', {name: 'MCP 集成管理', exact: true});
        await mcp.getByText('尚无连接登记', {exact: true}).waitFor();
        await page.getByRole('link', {name: '返回应用', exact: true}).click(); await page.waitForURL('**/assistant', {waitUntil: 'domcontentloaded'});
        await page.evaluate(() => window.dispatchEvent(new CustomEvent('fleet:navigate', {detail: {path: '/settings/integrations'}})));
        await panel.getByText('尚无身份登记', {exact: true}).waitFor();
        await mcp.getByText('尚无连接登记', {exact: true}).waitFor();
        assert.equal(requests.length, 2);
      }
    } else {
      const inspector = panel.getByRole('region', {name: 'Fleet 身份详情', exact: true});
      const row = id => panel.locator('[data-integration-id]').filter({has: page.getByText(id, {exact: true})});
      await row('.').click();
      await inspector.getByText('无工具权限', {exact: true}).waitFor();
      await inspector.locator('pre').filter({hasText: '<img src=x onerror='}).waitFor();
      assert.equal(await inspector.locator('img,a,script').count(), 0);
      assert.equal(await page.evaluate(() => window.identityXss), undefined);
      await row('b-restored').click(); await inspector.getByText('恢复后禁用', {exact: true}).waitFor();
      assert.equal(await inspector.getByRole('button', {name: '撤销身份', exact: true}).count(), 0);
      await row('c-revoked').click(); await inspector.getByText('已撤销', {exact: true}).waitFor();
      assert.equal(await inspector.getByRole('button', {name: '撤销身份', exact: true}).count(), 0);
      await panel.getByRole('button', {name: '加载更多身份', exact: true}).click();
      await row('../记录?x#').click(); await inspector.getByRole('heading', {name: '../记录?x#', exact: true}).waitFor();
      await inspector.getByText('z 租户 / ?#', {exact: true}).waitFor();
      for (const privateText of ['token_digest', 'definition_digest', 'admin-fixture-private-token', 'definition_snapshot', 'namespace']) {
        assert.equal((await panel.textContent()).includes(privateText), false);
      }
      const revoke = inspector.getByRole('button', {name: '撤销身份', exact: true});
      const confirm = async () => {
        await revoke.click(); const dialog = page.getByRole('dialog', {name: '撤销 Fleet 集成身份', exact: true});
        await dialog.getByLabel('已核对身份及全部授权范围', {exact: true}).check();
        await dialog.getByRole('button', {name: '确认撤销身份', exact: true}).click();
      };
      if (mode === 'review') {
        await capture(page, 'fleet-identity-detail-' + viewport);
        await revoke.click(); const dialog = page.getByRole('dialog');
        assert.equal(await dialog.getByRole('button', {name: '确认撤销身份', exact: true}).isDisabled(), true);
        await dialog.getByText('撤销不可恢复。', {exact: false}).waitFor();
        await capture(page, 'fleet-identity-confirm-' + viewport);
        await page.keyboard.press('Escape'); await dialog.waitFor({state: 'detached'});
        assert.equal(await revoke.evaluate(element => element === document.activeElement), true);
        await confirm(); await panel.getByText('撤销已确认，已读取当前状态', {exact: true}).waitFor();
        assert.equal(await revoke.count(), 0);
        await page.reload({waitUntil: 'domcontentloaded'});
        await panel.getByRole('button', {name: '加载更多身份', exact: true}).click();
        await row('../记录?x#').click(); await inspector.getByText('已撤销', {exact: true}).waitFor();
      } else if (mode === 'recovery') {
        const pattern = '**/integrations/fleet/revoke?**';
        await page.route(pattern, async route => { assert.equal((await route.fetch()).status(), 200); await route.abort('failed'); });
        await confirm(); await panel.getByText('撤销结果待核对，请重新读取身份。请勿重复提交。', {exact: true}).waitFor();
        assert.equal(await inspector.getByRole('button').count(), 0);
        assert.equal(requests.filter(item => item.method === 'POST').length, 1);
        await page.unroute(pattern); await panel.getByRole('button', {name: '重新读取身份', exact: true}).click();
        await inspector.getByText('已撤销', {exact: true}).waitFor();
        assert.equal(requests.filter(item => item.method === 'POST').length, 1);
        await panel.getByRole('button', {name: '加载更多身份', exact: true}).click();
        await row('integration').click(); await inspector.getByRole('heading', {name: 'integration', exact: true}).waitFor();
        await page.route(pattern, async route => {
          assert.equal((await context.request.post(prefix + '/revoke?owner_id=tenant&integration_id=integration',
            {data: {expected_state: 'active'}, headers: {Origin: origin}})).status(), 200);
          await route.continue();
        });
        await confirm(); await panel.getByText('身份状态已变化，已读取当前事实，请重新核对', {exact: true}).waitFor();
        await inspector.getByText('已撤销', {exact: true}).waitFor();
        assert.equal(requests.filter(item => item.method === 'POST').length, 2);
        await page.unroute(pattern);
      } else if (mode === 'lifecycle') {
        await revoke.click();
        await page.evaluate(() => window.dispatchEvent(new CustomEvent('fleet:navigate', {detail: {path: '/assistant'}})));
        await page.waitForURL('**/assistant', {waitUntil: 'domcontentloaded'}); assert.equal(await page.getByRole('dialog').count(), 0);
        await page.evaluate(() => window.dispatchEvent(new CustomEvent('fleet:navigate', {detail: {path: '/settings/integrations'}}))); await inspector.getByRole('heading', {name: '../记录?x#', exact: true}).waitFor();
        let held, signalRead; const arrived = new Promise(resolve => { signalRead = resolve; });
        const readPattern = '**/integrations/fleet/record?**';
        await page.route(readPattern, route => { held = route; signalRead(); });
        await panel.getByRole('button', {name: '重新读取身份', exact: true}).click(); await arrived;
        await page.getByRole('link', {name: '返回应用', exact: true}).click(); await page.waitForURL('**/assistant', {waitUntil: 'domcontentloaded'});
        await held.abort('failed'); await page.unroute(readPattern);
        await page.evaluate(() => window.dispatchEvent(new CustomEvent('fleet:navigate', {detail: {path: '/settings/integrations'}}))); await inspector.getByRole('heading', {name: '../记录?x#', exact: true}).waitFor();
        assert.equal(requests.filter(item => item.method === 'POST').length, 0);
        assert.equal((await context.request.post(origin + '/api/accounts/logout', {data: {}, headers: {Origin: origin}})).status(), 200);
        assert.equal((await context.request.post(origin + '/api/accounts/login', {data: {email: 'ordinary@example.test', password}, headers: {Origin: origin}})).status(), 200);
        await Promise.all([page.waitForEvent('domcontentloaded'), page.evaluate(() => window.checkNewAccount())]);
        await page.getByText('ordinary@example.test · 普通用户', {exact: true}).waitFor();
        assert.equal(await panel.count(), 0);
        assert.equal((await context.request.get(prefix)).status(), 403);
        assert.equal((await context.request.post(prefix + '/revoke' + selector, {data: {expected_state: 'active'}, headers: {Origin: origin}})).status(), 403);
      }
      if (mode === 'lifecycle') assert.equal(captchaRequests, 0);
      await capture(page, 'fleet-identity-' + mode + '-' + viewport);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false);
    }
    assert.deepEqual(errors, []); await context.close();
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
