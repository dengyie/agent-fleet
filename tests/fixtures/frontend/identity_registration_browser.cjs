const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs/promises');
const path = require('node:path');
const [origin, mode, viewport] = process.argv.slice(2);
const prefix = origin + '/api/platform/v1/integrations/fleet';
const password = 'safe-password-123!';
const ownerId = mode === 'exact-ids' ? '\ufefftenant' : 'tenant';
const integrationId = mode === 'exact-ids' ? '\ufeffbrowser-created' : 'browser-created';
const templateIds = mode === 'exact-ids' ? [' fixture ', '\ufeffnative'] : ['fixture'];

(async () => {
  const browser = await chromium.launch({channel: process.env.FLEET_BROWSER_CHANNEL || 'chrome', headless: true});
  try {
    const context = await browser.newContext({viewport: viewport === 'mobile' ? {width: 390, height: 844} : {width: 1440, height: 1000}});
    await context.addInitScript(() => {
      const clock = Date.now.bind(Date); let shift = 0;
      Date.now = () => clock() + shift;
      window.checkNewAccount = () => { shift += 60001; window.dispatchEvent(new Event('focus')); };
    });
    assert.equal((await context.request.post(origin + '/api/accounts/login', {
      data: {email: 'admin@example.test', password}, headers: {Origin: origin}})).status(), 200);
    const page = await context.newPage(); page.setDefaultTimeout(10000);
    const errors = [], posted = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => {
      if (request.url() === prefix + '/register') posted.push(JSON.parse(request.postData()));
    });
    await page.goto(origin + '/account', {waitUntil: 'domcontentloaded'});
    const panel = page.getByRole('region', {name: 'Fleet 集成身份管理', exact: true});
    await panel.getByRole('button', {name: '登记新身份', exact: true}).click();
    const dialog = page.getByRole('dialog', {name: '登记 Fleet 集成身份', exact: true});
    await dialog.getByLabel('归属身份', {exact: true}).fill(ownerId);
    await dialog.getByLabel('集成身份标识', {exact: true}).fill(integrationId);
    await dialog.getByLabel('授权模板（每行一个）', {exact: true}).fill(templateIds.join('\n'));
    await dialog.getByLabel('授权工作区（每行一个）', {exact: true}).fill('ready');
    assert.equal(await dialog.locator('fieldset').evaluate(element => getComputedStyle(element).display), 'grid');
    if (viewport === 'mobile') await dialog.getByLabel('查看工作流', {exact: true}).uncheck();
    if (process.env.FLEET_SCREENSHOTS) {
      await fs.mkdir(process.env.FLEET_SCREENSHOTS, {recursive: true});
      await page.screenshot({path: path.join(process.env.FLEET_SCREENSHOTS, 'identity-registration-form-' + mode + '-' + viewport + '.png')});
    }
    if (mode === 'hash-close') {
      await page.evaluate(() => {
        const digest = crypto.subtle.digest.bind(crypto.subtle);
        crypto.subtle.digest = (...args) => new Promise(resolve => { window.releaseHash = () => resolve(digest(...args)); });
      });
      await dialog.getByRole('button', {name: '生成令牌并登记', exact: true}).click();
      await page.waitForFunction(() => typeof window.releaseHash === 'function');
      await page.keyboard.press('Escape'); await dialog.waitFor({state: 'detached'});
      await page.evaluate(() => window.releaseHash());
      await panel.getByRole('button', {name: '重新读取身份', exact: true}).click();
      assert.equal(posted.length, 0);
      await panel.getByText('已读取 1 条身份登记', {exact: true}).waitFor().catch(async error => {
        error.message += ' PANEL=' + await panel.textContent(); throw error;
      });
    } else if (mode === 'lifecycle') {
      let held, arrived; const incoming = new Promise(resolve => { arrived = resolve; });
      await page.route(prefix + '/register', route => { held = route; arrived(); });
      await dialog.getByRole('button', {name: '生成令牌并登记', exact: true}).click(); await incoming;
      assert.equal((await context.request.post(origin + '/api/accounts/logout', {data: {}, headers: {Origin: origin}})).status(), 200);
      assert.equal((await context.request.post(origin + '/api/accounts/login', {
        data: {email: 'ordinary@example.test', password}, headers: {Origin: origin}})).status(), 200);
      await Promise.all([page.waitForEvent('domcontentloaded'), page.evaluate(() => window.checkNewAccount())]);
      await page.getByText('ordinary@example.test · 普通用户', {exact: true}).waitFor();
      await held.abort('failed');
      assert.equal(await page.getByRole('dialog').count(), 0); assert.equal(await panel.count(), 0);
      assert.equal(posted.length, 1);
      assert.equal((await context.request.get(prefix)).status(), 403);
    } else {
      if (mode === 'retry') {
        await page.route(prefix + '/register', async route => { assert.equal((await route.fetch()).status(), 200); await route.abort('failed'); });
      }
      await dialog.getByRole('button', {name: '生成令牌并登记', exact: true}).click();
      await dialog.getByLabel('访问令牌', {exact: true}).waitFor({state: 'visible'});
      const token = await dialog.getByLabel('访问令牌', {exact: true}).inputValue();
      assert.match(token, /^fleet_[a-f0-9]{64}$/);
      if (mode === 'retry') {
        await dialog.getByText('登记结果待核对。再次提交将沿用同一身份、权限和令牌。', {exact: true}).waitFor();
        assert.equal(posted.length, 1);
        assert.equal(await dialog.getByLabel('集成身份标识', {exact: true}).isDisabled(), true);
        await page.unroute(prefix + '/register');
        await dialog.getByRole('button', {name: '核对并重试原登记', exact: true}).click();
        assert.equal(await dialog.getByLabel('访问令牌', {exact: true}).inputValue(), token);
      }
      await dialog.getByText('身份已登记，请保存访问令牌。', {exact: true}).waitFor();
      assert.equal(await dialog.getByLabel('访问令牌', {exact: true}).evaluate(element => {
        const rect = element.getBoundingClientRect();
        return rect.top >= 0 && rect.bottom <= innerHeight &&
          document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2) === element;
      }), true, 'The created token must be visible and accessible in the dialog');
      assert.equal(posted.length, mode === 'retry' ? 2 : 1);
      for (const request of posted) {
        assert.equal(request.identity.token_digest, crypto.createHash('sha256').update(token).digest('hex'));
        assert.equal(JSON.stringify(request).includes(token), false);
        assert.equal(Object.keys(request.identity).length, 9);
        assert.equal(request.identity.owner_id, ownerId); assert.equal(request.identity.integration_id, integrationId);
        assert.deepEqual(request.identity.template_ids, templateIds);
        if (viewport === 'mobile') assert.deepEqual(request.identity.scopes, ['workflow.create_from_template', 'workflow.list_artifacts']);
      }
      if (posted.length === 2) assert.deepEqual(posted[0], posted[1]);
      assert.equal(await page.evaluate(token => Object.values(localStorage).concat(Object.values(sessionStorage)).some(value => value.includes(token)), token), false);
      const response = await context.request.post(origin + '/api/platform/mcp', {headers: {
        Authorization: 'Bearer ' + token, Accept: 'application/json, text/event-stream',
        'MCP-Protocol-Version': '2026-07-28', 'Mcp-Method': 'tools/list'},
        data: {jsonrpc: '2.0', id: 'browser-created', method: 'tools/list', params: {_meta: {'io.modelcontextprotocol/protocolVersion': '2026-07-28'}}}});
      assert.equal(response.status(), 200);
      const tools = (await response.json()).result.tools;
      assert.equal(tools.length, viewport === 'mobile' ? 2 : 3);
      if (viewport === 'mobile') assert.deepEqual(tools.map(tool => tool.name), ['workflow.create_from_template', 'workflow.list_artifacts']);
      if (process.env.FLEET_SCREENSHOTS) await page.screenshot({path: path.join(process.env.FLEET_SCREENSHOTS,
        'identity-registration-result-' + mode + '-' + viewport + '.png'), mask: [dialog.getByLabel('访问令牌', {exact: true})]});
      await dialog.getByRole('button', {name: '关闭', exact: true}).click(); await dialog.waitFor({state: 'detached'});
      assert.equal(await panel.getByRole('region', {name: 'Fleet 身份详情'}).locator('h3').textContent(), integrationId);
      await page.reload({waitUntil: 'domcontentloaded'});
      await panel.locator('[data-integration-id=' + JSON.stringify(integrationId) + ']').click();
      assert.equal(await panel.getByRole('region', {name: 'Fleet 身份详情'}).locator('h3').textContent(), integrationId);
      assert.equal((await panel.textContent()).includes(token), false);
    }
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false);
    assert.deepEqual(errors, []); await context.close();
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
