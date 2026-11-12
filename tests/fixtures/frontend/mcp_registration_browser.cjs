const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const [origin, mode, viewport] = process.argv.slice(2);
const prefix = origin + '/api/platform/v1/integrations/mcp';
const password = 'safe-password-123!';
const owner = mode === 'exact-owner' ? '\ufefftenant' : 'owner';

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
      const now = Date.now.bind(Date); let shift = 0;
      Date.now = () => now() + shift;
      window.checkNewAccount = () => { shift += 60001; window.dispatchEvent(new Event('focus')); };
    });
    assert.equal((await context.request.post(origin + '/api/accounts/login', {
      data: {email: 'admin@example.test', password}, headers: {Origin: origin}})).status(), 200);
    const page = await context.newPage(); page.setDefaultTimeout(10000);
    const errors = [], posts = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => {
      if (request.url().startsWith(prefix) && request.method() === 'POST') posts.push({url: request.url(), body: request.postDataJSON()});
    });
    await page.goto(origin + '/account', {waitUntil: 'domcontentloaded'});
    const panel = page.getByRole('region', {name: 'MCP 集成管理', exact: true});
    await panel.getByRole('button', {name: '登记 MCP 连接', exact: true}).click();
    const dialog = page.getByRole('dialog', {name: '登记 MCP 连接', exact: true});
    const fields = {'归属身份': owner, '工作区标识': 'workspace', '连接标识': 'browser-mcp',
      '服务地址': 'https://mcp.example.test/mcp', '凭据引用': 'env://MCP_TEST_TOKEN', '选定工具（每行一个）': 'weather'};
    for (const [name, value] of Object.entries(fields)) await dialog.getByLabel(name, {exact: true}).fill(value);
    assert.equal(await dialog.getByLabel('选定工具（每行一个）', {exact: true}).evaluate(element => {
      const rect = element.getBoundingClientRect();
      return rect.top >= 0 && rect.bottom <= innerHeight &&
        document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2) === element;
    }), true, 'focused tool input must be visible and editable above dialog actions');
    await capture(page, 'mcp-registration-form-' + mode + '-' + viewport);
    if (mode === 'cancel' || mode === 'lifecycle') {
      let held, arrived; const incoming = new Promise(resolve => { arrived = resolve; });
      await page.route(prefix + '/register', route => { held = route; arrived(); });
      await dialog.getByRole('button', {name: '登记连接', exact: true}).click(); await incoming;
      if (mode === 'cancel') {
        await page.keyboard.press('Escape'); await dialog.waitFor({state: 'detached'});
        await held.abort('failed'); await page.unroute(prefix + '/register');
        await panel.getByRole('button', {name: '重新读取目录', exact: true}).click();
        await panel.getByText('尚无连接登记', {exact: true}).waitFor();
      } else {
        assert.equal((await context.request.post(origin + '/api/accounts/logout', {data: {}, headers: {Origin: origin}})).status(), 200);
        assert.equal((await context.request.post(origin + '/api/accounts/login', {data: {email: 'ordinary@example.test', password}, headers: {Origin: origin}})).status(), 200);
        await Promise.all([page.waitForEvent('domcontentloaded'), page.evaluate(() => window.checkNewAccount())]);
        await page.getByText('ordinary@example.test · 普通用户', {exact: true}).waitFor();
        await held.abort('failed'); assert.equal(await dialog.count(), 0); assert.equal(await panel.count(), 0);
      }
      assert.equal(posts.length, 1);
    } else {
      if (mode === 'retry') await page.route(prefix + '/register', async route => {
        assert.equal((await route.fetch()).status(), 200); await route.abort('failed');
      });
      await dialog.getByRole('button', {name: '登记连接', exact: true}).click();
      if (mode === 'retry') {
        await dialog.getByText('登记结果待核对。再次提交将使用同一连接配置。', {exact: true}).waitFor();
        assert.equal(await dialog.getByLabel('连接标识', {exact: true}).isDisabled(), true);
        assert.equal(posts.length, 1);
        await page.unroute(prefix + '/register');
        await dialog.getByRole('button', {name: '核对并重试原登记', exact: true}).click();
      }
      const repeated = mode === 'disabled-repeat' || mode === 'active-repeat';
      await dialog.getByText(mode === 'disabled-repeat' ? '原登记已禁用，重复登记不会重新启用。' :
        mode === 'active-repeat' ? '原连接登记已核对，保留已有工具版本。' :
        '连接已登记。关闭窗口后可显式发现工具。', {exact: true}).waitFor();
      const registrations = posts.filter(value => value.url === prefix + '/register');
      assert.equal(registrations.length, mode === 'retry' ? 2 : 1);
      assert.equal(registrations[0].body.connector.owner_id, owner);
      if (mode === 'retry') assert.deepEqual(registrations[0].body, registrations[1].body);
      assert.equal(posts.filter(value => value.url.includes('/discover?')).length, 0, 'registration must not discover');
      await dialog.getByRole('button', {name: '关闭', exact: true}).click(); await dialog.waitFor({state: 'detached'});
      const inspector = panel.getByRole('region', {name: 'MCP 连接详情', exact: true});
      if (repeated) {
        await inspector.getByText(mode === 'disabled-repeat' ? '已撤销' : '已批准', {exact: true}).waitFor();
        assert.equal(await inspector.getByRole('button', {name: '发现工具', exact: true}).count(), 0);
        assert.equal(posts.filter(value => value.url.includes('/discover?')).length, 0);
        await capture(page, 'mcp-registration-final-' + mode + '-' + viewport);
      } else {
      await inspector.getByText('待发现', {exact: true}).waitFor();
      if (mode === 'discovery-recovery') await page.route('**/integrations/mcp/discover?**', async route => {
        assert.equal((await route.fetch()).status(), 200); await route.abort('failed');
      });
      await inspector.getByRole('button', {name: '发现工具', exact: true}).click();
      if (mode === 'discovery-recovery') {
        await panel.getByText('发现结果待核对，请重新读取目录。请勿重复提交。', {exact: true}).waitFor();
        await page.unroute('**/integrations/mcp/discover?**');
        await panel.getByRole('button', {name: '重新读取目录', exact: true}).click();
      }
      await inspector.getByText('已批准', {exact: true}).waitFor();
      await inspector.getByText('可提供已批准工具；未检测远端健康', {exact: true}).waitFor();
      assert.equal(posts.filter(value => value.url.includes('/discover?')).length, 1);
      await page.reload({waitUntil: 'domcontentloaded'});
      await panel.locator('[data-connector-id="browser-mcp"]').click();
      await inspector.getByText('已批准版本 1', {exact: true}).waitFor();
      assert.equal(posts.filter(value => value.url.includes('/discover?')).length, 1, 'reload must not rediscover');
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      await capture(page, 'mcp-registration-final-' + mode + '-' + viewport);
      }
    }
    assert.deepEqual(errors, []); await context.close();
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
