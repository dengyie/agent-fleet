const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const [origin, viewport, scenario] = process.argv.slice(2);
(async () => {
  const mobile = viewport === 'mobile';
  const browser = await chromium.launch({channel: process.env.FLEET_BROWSER_CHANNEL || 'chrome', headless: true});
  try {
    const context = await browser.newContext({viewport: mobile ? {width: 390, height: 844} : {width: 1440, height: 1000}, hasTouch: mobile});
    assert.equal((await context.request.post(origin + '/api/accounts/login', {
      data: {email: 'admin@example.test', password: 'safe-password-123!'}, headers: {Origin: origin},
    })).status(), 200);
    const page = await context.newPage(); page.setDefaultTimeout(6000);
    const errors = []; page.on('pageerror', error => errors.push(error.message));
    await page.goto(origin + '/assistant');
    const composer = page.getByLabel('给助手的任务', {exact: true}); await composer.waitFor();
    await composer.fill('设置交互回归：保留草稿，不提交');
    const navigate = target => page.evaluate(target => window.dispatchEvent(new CustomEvent('fleet:navigate', {detail: {path: target}})), target);
    const dialog = page.locator('.settings-dialog[open]');
    const settle = async () => {
      await dialog.waitFor();
      await dialog.evaluate(node => Promise.all(node.getAnimations().map(animation => animation.finished)));
    };
    const open = async section => { await navigate('/settings/' + section); await settle(); };
    const assertOpen = async () => {
      assert.equal(await dialog.count(), 1);
      assert.equal(await dialog.evaluate(node => node.classList.contains('is-closing')), false, 'gesture must not begin dismissing settings');
    };
    const choose = async (id, label) => {
      const navigation = page.getByRole('navigation', {name: '设置分类', exact: true});
      if (mobile) await navigation.getByLabel('设置分类', {exact: true}).selectOption(id);
      else await navigation.getByRole('link', {name: label, exact: true}).click();
      await settle();
    };
    const close = async () => {
      await page.getByRole('button', {name: '关闭设置', exact: true}).click();
      await dialog.waitFor({state: 'detached'});
    };

    if (scenario === 'history-menus') {
      if (mobile) await page.getByRole('button', {name: '打开导航', exact: true}).click();
      const menus = page.locator('#sidebar-conversations .conversation-actions');
      // Open the lower menu first so its popup does not cover the next trigger.
      const first = menus.nth(1), second = menus.nth(0);
      const firstTrigger = first.locator('summary'), secondTrigger = second.locator('summary');
      await firstTrigger.click();
      assert.equal(await first.evaluate(node => node.open), true);
      await secondTrigger.click();
      assert.equal(await first.evaluate(node => node.open), false, 'only one conversation action menu stays open');
      assert.equal(await second.evaluate(node => node.open), true);
      await page.getByLabel('搜索最近对话', {exact: true}).click();
      assert.equal(await second.evaluate(node => node.open), false, 'outside interaction dismisses the menu');
      await secondTrigger.click(); await page.keyboard.press('Escape');
      assert.equal(await second.evaluate(node => node.open), false);
      assert.equal(await secondTrigger.evaluate(node => node === document.activeElement), true);
      if (mobile) assert.equal(await page.locator('#sidebar').evaluate(node => node.classList.contains('open')), true, 'Escape closes the menu before closing its navigation drawer');
      await secondTrigger.press('ArrowDown');
      const rename = second.getByRole('button', {name: '重命名', exact: true});
      assert.equal(await rename.evaluate(node => node === document.activeElement), true);
      await page.keyboard.press('End');
      assert.equal(await second.getByRole('button', {name: '归档', exact: true}).evaluate(node => node === document.activeElement), true);
      await page.keyboard.press('Home');
      assert.equal(await rename.evaluate(node => node === document.activeElement), true);
      await page.keyboard.press('Tab');
      assert.equal(await second.evaluate(node => node.open), false, 'Tab leaves a dismissed menu');
      await firstTrigger.click();
      if (mobile) {
        await page.getByRole('button', {name: '收起导航', exact: true}).click();
        await page.getByRole('button', {name: '打开导航', exact: true}).click();
        assert.equal(await first.evaluate(node => node.open), false, 'reopening navigation must not reveal a stale action menu');
      }
      // Opening settings does not leave background action menus expanded.
      await page.locator('[data-nav="settings"]').click(); await settle(); await close();
      assert.equal(await page.locator('#sidebar-conversations .conversation-actions[open]').count(), 0);
    } else if (scenario === 'focus-return') {
      if (mobile) await page.getByRole('button', {name: '打开导航', exact: true}).click();
      await page.getByRole('button', {name: '账号菜单', exact: true}).click();
      await page.getByRole('menuitem', {name: '账号与访问', exact: true}).click();
      await settle(); await close();
      assert.equal(await page.locator(mobile ? '#navigation-toggle' : '#account-menu-trigger').evaluate(node => node === document.activeElement), true,
        'closing returns focus to its opener, with a visible fallback for a collapsed mobile drawer');
      // A programmatic open from the composer should return to that exact field.
      await composer.focus();
      await open('appearance'); await page.keyboard.press('Escape');
      await dialog.waitFor({state: 'detached'});
      assert.equal(await composer.evaluate(node => node === document.activeElement || node.getRootNode().activeElement === node), true);
    } else if (scenario === 'category-focus') {
      await open('appearance');
      for (const [id, label] of [['nodes', 'VM 与执行节点'], ['appearance', '外观'], ['nodes', 'VM 与执行节点']]) {
        await choose(id, label);
        const target = mobile ? dialog.locator('.settings-mobile-section') : dialog.locator(`[data-section="${id}"]`);
        assert.equal(await target.evaluate(node => node === document.activeElement), true, 'category switch preserves navigation focus instead of jumping to close');
      }
    } else if (scenario === 'search') {
      await open('appearance');
      const search = page.getByLabel('搜索设置', {exact: true});
      await search.fill('not-a-setting');
      await page.getByText('没有匹配的设置', {exact: true}).waitFor();
      await search.press('Escape');
      await assertOpen();
      assert.equal(await search.inputValue(), '', 'Escape clears the query before dismissing the dialog');
      await search.fill('VM');
      const clearSearch = page.getByRole('button', {name: '清除设置搜索', exact: true});
      assert.equal(await clearSearch.evaluate(node => node.closest('label')), null, 'clear remains a separately labelled control');
      await clearSearch.click();
      assert.equal(await search.inputValue(), '');
      assert.equal(await search.evaluate(node => node === document.activeElement), true);
      await search.fill('VM');
      await search.press('Enter'); await settle();
      assert.equal(new URL(page.url()).pathname, '/settings/nodes');
      const selected = dialog.locator('[aria-current="page"]');
      assert.equal(await selected.evaluate(node => node === document.activeElement), true);
      await page.keyboard.press('End');
      assert.equal(await dialog.locator('[data-section="appearance"]').evaluate(node => node === document.activeElement), true);
      await page.keyboard.press('Home');
      assert.equal(await dialog.locator('[data-section="models"]').evaluate(node => node === document.activeElement), true);
      await page.keyboard.press('ArrowDown');
      assert.equal(await dialog.locator('[data-section="defaults"]').evaluate(node => node === document.activeElement), true);
      await page.keyboard.press('ArrowUp');
      assert.equal(await dialog.locator('[data-section="models"]').evaluate(node => node === document.activeElement), true);
      await page.keyboard.press('Enter'); await settle();
      assert.equal(new URL(page.url()).pathname, '/settings/models');
      await choose('appearance', '外观');
      assert.equal(await search.evaluate(node => node === document.activeElement), true, 'a cached query hiding the selected category returns focus to search');
      await search.fill('VM');
      await search.dispatchEvent('keydown', {key: 'Enter', isComposing: true});
      await search.dispatchEvent('keydown', {key: 'Escape', isComposing: true});
      assert.equal(new URL(page.url()).pathname, '/settings/appearance');
      await assertOpen();
      assert.equal(await search.inputValue(), 'VM', 'IME composition keys must not navigate or clear');
    } else if (scenario === 'scroll') {
      await open('runtime');
      const state = await dialog.evaluate(node => ({
        content: getComputedStyle(node.querySelector('.settings-content')).overscrollBehaviorY,
        navigation: getComputedStyle(node.querySelector('.settings-navigation')).overscrollBehaviorY,
        body: getComputedStyle(document.body).overflowY,
        close: node.querySelector('.settings-close').getBoundingClientRect().toJSON(),
        category: node.querySelector('.settings-mobile-section').getBoundingClientRect().toJSON(),
      }));
      assert.equal(state.content, 'contain', 'content boundary must not scroll the background');
      assert.equal(state.navigation, 'contain', 'category boundary must not scroll the background');
      assert.equal(state.body, 'hidden');
      if (mobile) {
        assert.ok(state.close.width >= 44 && state.close.height >= 44, 'mobile close has a 44px tap target');
        assert.ok(state.category.height >= 44, 'mobile category has a 44px tap target');
      }
      const scrollBefore = await page.evaluate(() => scrollY);
      await page.mouse.move(mobile ? 180 : 1000, 400); await page.mouse.wheel(0, 5000);
      assert.equal(await page.evaluate(() => scrollY), scrollBefore);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      if (process.env.FLEET_SCREENSHOTS) {
        await fs.mkdir(process.env.FLEET_SCREENSHOTS, {recursive: true});
        await page.screenshot({path: path.join(process.env.FLEET_SCREENSHOTS, `settings-interactions-${viewport}.png`), fullPage: true});
      }
    } else if (scenario === 'keyboard-safety') {
      await open('appearance');
      // Native dialog owns focus confinement in both directions.
      await dialog.locator('.settings-close').focus();
      await page.keyboard.press('Shift+Tab');
      assert.equal(await dialog.evaluate(node => document.activeElement === document.body || node.contains(document.activeElement)), true, 'Tab can reach browser chrome but never a background control');
      await page.keyboard.press('Tab');
      assert.equal(await dialog.evaluate(node => document.activeElement === document.body || node.contains(document.activeElement)), true);
      await page.emulateMedia({colorScheme: 'dark', reducedMotion: 'reduce'});
      await close(); await open('appearance');
      await page.keyboard.press('Escape'); await dialog.waitFor({state: 'detached'});
      assert.equal(await composer.inputValue(), '设置交互回归：保留草稿，不提交');
      // Closing a bookmarked settings URL falls back to a live assistant page.
      await page.goto(origin + '/settings/appearance'); await settle(); await close();
      assert.equal(new URL(page.url()).pathname, '/assistant');
      assert.equal(await page.getByRole('button', {name: '打开导航', exact: true}).count(), mobile ? 1 : 0);
    } else {
      await open('appearance');
      const rect = await dialog.boundingBox();
      const outside = {x: Math.max(2, rect.x / 2), y: 400};
      const inside = {x: rect.x + rect.width / 2, y: rect.y + rect.height / 2};
      if (scenario === 'pointer-safety') {
        await page.getByRole('heading', {name: '外观主题', exact: true}).click(); await assertOpen();
        await page.mouse.click(rect.x + 3, rect.y + 3); await assertOpen();
        const drag = async (from, to) => {
          await page.mouse.move(from.x, from.y); await page.mouse.down();
          await page.mouse.move(to.x, to.y, {steps: 4}); await page.mouse.up(); await assertOpen();
        };
        await drag(inside, outside);
        await drag(outside, inside);
        await drag(outside, {x: outside.x, y: outside.y + 80});
        await page.mouse.move(outside.x, outside.y); await page.mouse.down();
        await page.mouse.move(outside.x, outside.y + 80, {steps: 4});
        await page.mouse.move(outside.x, outside.y, {steps: 4}); await page.mouse.up(); await assertOpen();
        await page.mouse.click(outside.x, outside.y, {button: 'right'}); await assertOpen();
        await page.mouse.move(outside.x, outside.y); await page.mouse.down();
        await dialog.dispatchEvent('pointercancel', {pointerId: 1});
        await page.mouse.up(); await assertOpen();
      }
      if (mobile) await page.touchscreen.tap(outside.x, outside.y);
      else await page.mouse.click(outside.x, outside.y);
      assert.equal(await dialog.evaluate(node => node.classList.contains('is-closing')), true, 'a genuine backdrop click starts the existing dismissal animation');
      await dialog.waitFor({state: 'detached'});
      assert.equal(new URL(page.url()).pathname, '/assistant');
      assert.equal(await composer.inputValue(), '设置交互回归：保留草稿，不提交');
      await open('appearance'); await close();
    }
    assert.deepEqual(errors, []);
    await context.close();
    console.log(JSON.stringify({viewport, scenario, pageErrors: errors}));
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exit(1);});
