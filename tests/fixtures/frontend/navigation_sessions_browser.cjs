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
      if (scenario === 'sidebar-motion') {
        const sidebar = page.locator('#sidebar');
        const main = page.locator('.console-main');
        const opened = await sidebar.boundingBox();
        const close = page.getByRole('button', {name: '收起导航', exact: true});
        const initialButton = await close.boundingBox();
        await close.evaluate(node => { window.originalNavigationToggle = node; node.click(); });
        const midpoint = await page.evaluate(() => {
          const panel = document.querySelector('#sidebar');
          const main = document.querySelector('.console-main');
          panel.getBoundingClientRect();
          const panelMotion = panel.getAnimations();
          const mainMotion = main.getAnimations();
          for (const animation of [...panelMotion, ...mainMotion]) {
            animation.pause(); animation.currentTime = animation.effect.getTiming().duration / 2;
          }
          return {panelAnimations: panelMotion.length, mainAnimations: mainMotion.length,
            panelX: panel.getBoundingClientRect().x, mainX: main.getBoundingClientRect().x};
        });
        assert.ok(midpoint.panelAnimations > 0, 'desktop sidebar must slide instead of jumping directly to hidden');
        assert.ok(midpoint.mainAnimations > 0, 'main content must move with the sidebar');
        assert.ok(midpoint.panelX < 0 && midpoint.panelX > -opened.width);
        assert.ok(midpoint.mainX > 0 && midpoint.mainX < opened.width);
        const settle = () => page.evaluate(async () => {
          const nodes = ['#sidebar', '.console-main', '.console-topbar', '#navigation-toggle', '#navigation-toggle svg', '#sidebar-backdrop'];
          const animations = nodes.flatMap(selector => document.querySelector(selector)?.getAnimations() || []);
          for (const animation of animations) animation.finish();
          await Promise.all(animations.map(animation => animation.finished));
        });
        await settle();
        const toggle = page.locator('#navigation-toggle');
        assert.equal(await toggle.evaluate(node => node === window.originalNavigationToggle), true,
          'collapse and expand use the same persistent control');
        const closedButton = await toggle.boundingBox();
        assert.equal(initialButton.y, closedButton.y, 'toggle stays on the same vertical baseline');
        assert.equal(initialButton.width, closedButton.width);
        assert.equal(initialButton.height, closedButton.height);
        assert.ok(Math.abs(initialButton.x - (opened.width - 48)) < 1);
        assert.ok(Math.abs(closedButton.x - 14) < 1);
        assert.equal(await toggle.evaluate(node => node === document.activeElement), true);
        assert.equal(await sidebar.evaluate(node => node.inert), true);
        assert.equal(await toggle.getAttribute('aria-expanded'), 'false');
        await toggle.click();
        await settle();
        assert.equal(await toggle.getAttribute('aria-expanded'), 'true');
        assert.equal(await sidebar.evaluate(node => node.inert), false);
        assert.ok(Math.abs((await main.boundingBox()).x - opened.width) < 1);
        // Reversing during a transition must remain continuous and end expanded.
        const reversal = await page.evaluate(() => {
          const panel = document.querySelector('#sidebar');
          const toggle = document.querySelector('#navigation-toggle');
          toggle.click();
          panel.getBoundingClientRect();
          for (const node of [panel, document.querySelector('.console-main')]) {
            for (const animation of node.getAnimations()) { animation.pause(); animation.currentTime = 80; }
          }
          const before = panel.getBoundingClientRect().x;
          toggle.click();
          // Sample the reversal in the same browser task. A separate protocol
          // round trip lets the new animation advance under CI scheduling load.
          const after = panel.getBoundingClientRect().x;
          return {before, after, animations: panel.getAnimations().length};
        });
        assert.ok(reversal.before < 0 && reversal.before > -opened.width);
        assert.ok(reversal.animations > 0, 'reversal must continue animating');
        assert.ok(Math.abs(reversal.after - reversal.before) < 1, 'rapid reversal must not jump to an endpoint');
        await settle();
        assert.equal(await toggle.getAttribute('aria-expanded'), 'true');
        await page.screenshot({path: path.join(output, 'sidebar-expanded.png'), fullPage: true});
        await toggle.click(); await settle();
        await page.screenshot({path: path.join(output, 'sidebar-collapsed.png'), fullPage: true});
        await page.reload({waitUntil: 'domcontentloaded'});
        await page.getByLabel('给助手的任务').waitFor();
        assert.equal(await toggle.getAttribute('aria-expanded'), 'false', 'desktop preference survives reload');
        await page.setViewportSize({width: 390, height: 844}); await settle();
        await toggle.click();
        const mobileMotion = await page.evaluate(() => {
          const backdrop = document.querySelector('#sidebar-backdrop');
          backdrop.getBoundingClientRect();
          const animations = backdrop.getAnimations();
          for (const animation of animations) {
            animation.pause(); animation.currentTime = animation.effect.getTiming().duration / 2;
          }
          return {count: animations.length, opacity: Number(getComputedStyle(backdrop).opacity)};
        });
        assert.ok(mobileMotion.count > 0 && mobileMotion.opacity > 0 && mobileMotion.opacity < 1,
          'mobile backdrop fades with the drawer');
        await settle(); await page.keyboard.press('Escape'); await settle();
        await page.setViewportSize({width: 1440, height: 900}); await settle();
        await page.emulateMedia({reducedMotion: 'reduce'});
        await toggle.click(); await settle();
        assert.ok(await sidebar.evaluate(node => getComputedStyle(node).transitionDuration.split(',')
          .every(value => parseFloat(value) < .001)), 'reduced motion removes perceptible animation');
        for (const width of [900, 768, 767, 390]) {
          await page.setViewportSize({width, height: 844});
          await settle();
          assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
        }
        const mobileClosed = await toggle.boundingBox();
        await toggle.click(); await settle();
        const mobileOpen = await toggle.boundingBox();
        assert.equal(mobileClosed.y, mobileOpen.y);
        assert.equal(await toggle.getAttribute('aria-expanded'), 'true');
        await page.keyboard.press('Shift+Tab');
        assert.equal(await toggle.evaluate(node => node === document.activeElement), true);
        await page.keyboard.press('Shift+Tab');
        assert.equal(await page.locator('#account-menu-trigger').evaluate(node => node === document.activeElement), true);
        await page.keyboard.press('Tab');
        assert.equal(await toggle.evaluate(node => node === document.activeElement), true);
        await page.keyboard.press('Escape'); await settle();
        assert.equal(await toggle.getAttribute('aria-expanded'), 'false');
        assert.equal(await sidebar.evaluate(node => node.inert), true);
        await toggle.click(); await settle();
        await page.screenshot({path: path.join(output, 'sidebar-mobile.png'), fullPage: true});
        await page.locator('#sidebar-backdrop').click({position: {x: 380, y: 400}}); await settle();
        assert.equal(await toggle.getAttribute('aria-expanded'), 'false');
      } else if (scenario === 'feature-401') {
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
        const choice = page.getByRole('radio', {name: '自动', exact: true});
        assert.equal(await choice.isChecked(), true);
        await page.getByRole('radio', {name: '浅色', exact: true}).check();
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
        assert.equal(await page.locator('#navigation-toggle').getAttribute('aria-expanded'), 'false');
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      }
    }
    assert.deepEqual(errors, []);
    console.log('PASS: ' + scenario);
  } finally { release(); await context.close(); await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
