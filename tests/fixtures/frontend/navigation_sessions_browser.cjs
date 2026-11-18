const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const [origin, scenario] = process.argv.slice(2);
(async () => {
  const browser = await chromium.launch({channel: process.env.FLEET_BROWSER_CHANNEL || 'chrome', headless: true});
  const context = await browser.newContext({viewport: {width: 1440, height: 900}});
  const page = await context.newPage();
  page.setDefaultTimeout(5000);
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await context.addInitScript(() => {
    const nativeNow = Date.now.bind(Date);
    let offset = 0;
    Date.now = () => nativeNow() + offset;
    window.advanceFleetTime = () => { offset += 60001; window.dispatchEvent(new Event('focus')); };
  });
  const output = process.env.FLEET_SCREENSHOTS || '/tmp/agent-fleet-navigation-evidence';
  fs.mkdirSync(output, {recursive: true});
  let release = () => {};
  try {
    if (scenario === 'login-loading') {
      let reached;
      const waiting = new Promise(resolve => { reached = resolve; });
      const held = new Promise(resolve => { release = resolve; });
      await page.route('**/api/accounts/options', async route => { reached(); await held; await route.continue(); });
      await page.goto(origin + '/login', {waitUntil: 'domcontentloaded'});
      await waiting;
      assert.equal(await page.locator('#access-state').isVisible(), true, 'login service loading must not leave a blank document');
      assert.match(await page.locator('#access-state').innerText(), /正在加载登录/);
      release();
      await page.getByLabel('账号或邮箱').waitFor();
    } else {
      const login = await context.request.post(origin + '/api/accounts/login', {
        headers: {Origin: origin}, data: {email: 'admin@example.test', password: 'Navigation-password-123!'},
      });
      assert.equal(login.status(), 200);
      await page.goto(origin + '/assistant', {waitUntil: 'domcontentloaded'});
      await page.waitForFunction(() => document.querySelector('[aria-label="发送任务"]') && !document.querySelector('[aria-label="发送任务"]').disabled);
      if (scenario === 'feature-401') {
        let checking;
        const checkStarted = new Promise(resolve => { checking = resolve; });
        const held = new Promise(resolve => { release = resolve; });
        await page.route('**/api/operator/session', async route => { checking(); await held; await route.continue(); }, {times: 1});
        const session = page.waitForResponse(response => response.url().endsWith('/api/operator/session'));
        session.catch(() => {});
        await page.getByLabel('给助手的任务').fill('保留身份校验前的草稿');
        await page.route('**/api/platform/v1/conversations?*', route => route.fulfill({status: 401,
          contentType: 'application/json', body: JSON.stringify({error: 'unauthorized'})}), {times: 1});
        await page.evaluate(async () => {
          const {platformRequest} = await import('/assets/api/client.js');
          await platformRequest('/platform/v1/conversations?limit=50').catch(() => {});
        });
        await checkStarted;
        assert.equal(await page.locator('#access-state').isVisible(), true, 'revalidation must show progress, not a blank screen');
        release();
        assert.equal((await session).status(), 200);
        await page.waitForFunction(() => !document.querySelector('.console-layout').hidden &&
          document.querySelector('[aria-label="发送任务"]') && !document.querySelector('[aria-label="发送任务"]').disabled);
        assert.equal(await page.getByLabel('给助手的任务').inputValue(), '保留身份校验前的草稿');
        assert.equal(new URL(page.url()).pathname, '/assistant', 'a valid session check must not log out the user');
        // A network outage while revalidating must retain credentials and offer recovery.
        await page.route('**/api/operator/session', route => route.abort('failed'));
        await page.evaluate(() => window.dispatchEvent(new Event('fleet-auth-required')));
        await page.getByText('工作空间暂时无法连接，稍后会自动重试。', {exact: true}).waitFor();
        assert.equal(await page.locator('#access-state').isVisible(), true);
        await page.unroute('**/api/operator/session');
        await page.getByRole('button', {name: '重新验证', exact: true}).click();
        await page.waitForFunction(() => !document.querySelector('.console-layout').hidden);
        // Explicit server revocation still ends the session.
        await context.request.post(origin + '/api/accounts/sessions/revoke', {headers: {Origin: origin}, data: {}});
        await page.evaluate(() => window.advanceFleetTime());
        await page.waitForURL('**/login?*');
        await page.getByLabel('账号或邮箱').waitFor();
      } else if (scenario === 'logout-loading') {
        const draft = page.getByLabel('给助手的任务');
        await draft.fill('退出失败后仍保留的草稿');
        await page.route('**/api/accounts/logout', route => route.fulfill({status: 503,
          contentType: 'application/json', body: JSON.stringify({error: 'unavailable'})}), {times: 1});
        await page.locator('#account-menu-trigger').click();
        await page.getByRole('menuitem', {name: '退出登录', exact: true}).click();
        await page.getByText('退出登录失败，请重试', {exact: true}).waitFor();
        assert.equal(await draft.inputValue(), '退出失败后仍保留的草稿');
        assert.equal((await context.request.get(origin + '/api/operator/session')).status(), 200);
        let reached;
        const waiting = new Promise(resolve => { reached = resolve; });
        const held = new Promise(resolve => { release = resolve; });
        await page.route('**/api/accounts/logout', async route => { reached(); await held; await route.continue(); });
        await page.getByRole('menuitem', {name: '退出登录', exact: true}).click();
        await waiting;
        assert.equal(await page.getByRole('menuitem', {name: '正在退出…', exact: true}).isDisabled(), true);
        assert.equal(await page.locator('.console-layout').isVisible(), true);
        release();
        await page.waitForURL('**/login?*');
        await page.getByLabel('账号或邮箱').waitFor();
      } else if (scenario === 'menus') {
        assert.equal(await page.locator('#sidebar [data-theme]').count(), 0);
        const trigger = page.locator('#theme-menu-trigger');
        assert.equal(await trigger.locator('svg').count(), 1);
        await trigger.click();
        await page.getByRole('menuitemradio', {name: '深色', exact: true}).click();
        assert.equal(await page.locator('html').getAttribute('data-resolved-theme'), 'dark');
        await page.reload({waitUntil: 'domcontentloaded'});
        await trigger.waitFor();
        assert.equal(await page.locator('html').getAttribute('data-resolved-theme'), 'dark');
        await trigger.click();
        await page.keyboard.press('End');
        await page.keyboard.press('Enter');
        assert.equal(await page.evaluate(() => localStorage.getItem('agent-fleet-theme')), 'system');
        await page.emulateMedia({colorScheme: 'light'});
        await page.waitForFunction(() => document.documentElement.dataset.resolvedTheme === 'light');
        await page.emulateMedia({colorScheme: 'dark'});
        await page.waitForFunction(() => document.documentElement.dataset.resolvedTheme === 'dark');
        await trigger.click();
        await page.keyboard.press('Escape');
        assert.equal(await trigger.getAttribute('aria-expanded'), 'false');
        assert.equal(await trigger.evaluate(node => node === document.activeElement), true);
        await trigger.click();
        await page.locator('#main-content').click({position: {x: 20, y: 20}});
        assert.equal(await trigger.getAttribute('aria-expanded'), 'false');
        const history = await page.locator('#sidebar-adjustable-lists').boundingBox();
        const workspace = await page.locator('.console-nav').boundingBox();
        assert.ok(history.y < workspace.y && history.height > 100);
        await page.locator('#account-menu-trigger').click();
        await page.getByRole('menuitem', {name: '账号与访问', exact: true}).click();
        await page.locator('.settings-dialog[open]').waitFor();
        await page.keyboard.press('Escape');
        await page.locator('.settings-dialog').waitFor({state: 'detached'});
        await page.locator('.sidebar-footer [data-nav="settings"]').click();
        await page.getByRole('link', {name: '外观', exact: true}).click();
        const choice = page.getByLabel('配色方案');
        assert.equal(await choice.inputValue(), 'system');
        await choice.selectOption('light');
        await page.keyboard.press('Escape');
        await page.locator('.settings-dialog').waitFor({state: 'detached'});
        await trigger.click();
        assert.equal(await page.getByRole('menuitemradio', {name: '浅色', exact: true}).getAttribute('aria-checked'), 'true');
        await page.keyboard.press('Escape');
        await page.screenshot({path: path.join(output, 'navigation-desktop.png'), fullPage: true});
        await page.setViewportSize({width: 390, height: 844});
        await trigger.click();
        const menu = await page.locator('#theme-menu').boundingBox();
        assert.ok(menu.x >= 0 && menu.x + menu.width <= 390);
        await page.keyboard.press('Escape');
        await page.getByRole('button', {name: '打开导航', exact: true}).click();
        await page.locator('#account-menu-trigger').click();
        assert.equal(await page.getByRole('menuitem', {name: '退出登录', exact: true}).isVisible(), true);
        await page.screenshot({path: path.join(output, 'navigation-mobile.png'), fullPage: true});
        await page.keyboard.press('Escape');
        await page.keyboard.press('Escape');
        assert.equal(await page.locator('#mobile-menu').getAttribute('aria-expanded'), 'false');
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      }
    }
    assert.deepEqual(errors, []);
    console.log('PASS: ' + scenario);
  } finally { release(); await context.close(); await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
