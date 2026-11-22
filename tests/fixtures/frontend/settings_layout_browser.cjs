// Real category data, native theme controls and layouts within the settings scrollport.
const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const [origin, viewport] = process.argv.slice(2);
(async () => {
  const mobile = viewport === 'mobile';
  const browser = await chromium.launch({channel: process.env.FLEET_BROWSER_CHANNEL || 'chrome', headless: true});
  try {
    const context = await browser.newContext({viewport: mobile ? {width: 390, height: 844} : {width: 1440, height: 1000}});
    assert.equal((await context.request.post(origin + '/api/accounts/login', {
      headers: {Origin: origin}, data: {email: 'admin@example.test', password: 'safe-password-123!'},
    })).status(), 200);
    const page = await context.newPage(); page.setDefaultTimeout(10000);
    const errors = []; page.on('pageerror', error => errors.push(error.message));
    const dialog = page.locator('.settings-dialog[open]');
    const navigate = async id => {
      await page.goto(origin + '/settings/' + id);
      await dialog.waitFor();
      await dialog.evaluate(node => Promise.all(node.getAnimations().map(animation => animation.finished)));
    };
    const checkLayout = async label => {
      const overflow = await dialog.locator('.settings-content').evaluate(node => ({scroll: node.scrollWidth, width: node.clientWidth}));
      assert.ok(overflow.scroll <= overflow.width + 1, label + ' must fit the content scrollport: ' + JSON.stringify(overflow));
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), label + ' must fit the page');
      if (mobile) {
        const controls = await dialog.locator('.settings-content :is(button, input:not([type=checkbox]):not([type=radio]), select)').evaluateAll(nodes => nodes.filter(n => n.getClientRects().length).map(n => ({name: n.textContent || n.name, height: n.getBoundingClientRect().height})));
        for (const control of controls) assert.ok(control.height >= 43, label + ': touch target ' + JSON.stringify(control));
      }
    };
    const screenshot = async label => {
      if (!process.env.FLEET_SCREENSHOTS) return;
      await fs.mkdir(process.env.FLEET_SCREENSHOTS, {recursive: true});
      await page.screenshot({path: path.join(process.env.FLEET_SCREENSHOTS, 'settings-layout-' + label + '-' + viewport + '.png')});
    };
    await navigate('models');
    await dialog.getByText('尚未添加配置', {exact: true}).waitFor();
    await screenshot('empty');
    await dialog.getByRole('button', {name: '新增模型源', exact: true}).click();
    assert.equal(await page.getByLabel('模型配置 ID', {exact: true}).evaluate(node => node === document.activeElement), true);
    assert.equal(await dialog.locator('.settings-empty').isVisible(), false, 'empty state makes room for the editor');
    await page.getByLabel('模型配置 ID', {exact: true}).fill('layout-model');
    await page.getByLabel('服务商名称', {exact: true}).fill('布局验收 API');
    await page.getByLabel('模型 ID', {exact: true}).fill('视觉验收模型-' + '长名称'.repeat(14));
    await page.getByLabel('Base URL', {exact: true}).fill('https://models.example.test/' + 'very-long-endpoint/'.repeat(8));
    await page.getByLabel('API Key', {exact: true}).fill('layout-fixture-key');
    await page.locator('.settings-advanced summary').click();
    await checkLayout('expanded model editor');
    await page.getByLabel('最大重试次数', {exact: true}).fill('2');
    await page.getByRole('button', {name: '保存配置', exact: true}).click();
    await page.getByText('配置已保存，将用于之后的新任务', {exact: true}).waitFor();
    const record = dialog.getByRole('button', {name: /视觉验收模型/}); await record.click();
    assert.equal(await record.getAttribute('aria-pressed'), 'true');
    await checkLayout('long record');
    await page.getByRole('button', {name: '取消编辑', exact: true}).click();
    assert.equal(await record.getAttribute('aria-pressed'), 'false');
    await screenshot('models');
    const readiness = {
      defaults: () => page.getByLabel('默认模型', {exact: true}).waitFor(),
      nodes: () => page.getByRole('button', {name: '新增执行节点', exact: true}).waitFor(),
      workspaces: () => page.getByRole('button', {name: '新增工作区', exact: true}).waitFor(),
      hosts: () => page.getByLabel('节点清单（YAML）', {exact: true}).waitFor(),
      runtime: () => page.locator('.settings-runtime-group').first().waitFor(),
      account: () => page.getByRole('heading', {name: '账号管理', exact: true}).waitFor(),
      plugins: () => page.locator('.plugin-card').first().waitFor(),
      integrations: () => page.getByRole('region', {name: 'MCP 集成管理', exact: true}).waitFor(),
      appearance: () => page.getByRole('radio', {name: '自动', exact: true}).waitFor(),
    };
    for (const [id, ready] of Object.entries(readiness)) {
      await navigate(id); await ready();
      if (id === 'runtime') {
        for (const summary of await page.locator('.settings-runtime-group summary').all()) await summary.click();
        const toggle = page.getByLabel('记忆管理', {exact: true}); const before = await toggle.isChecked();
        await toggle.locator('..').click(); assert.equal(await toggle.isChecked(), !before);
        assert.ok(await toggle.locator('..').evaluate(node => node.getBoundingClientRect().height >= 44));
      }
      await checkLayout(id);
      await dialog.locator('.settings-content').evaluate(node => { node.scrollTop = 0; });
      await screenshot(id);
    }
    const dark = page.getByRole('radio', {name: '深色', exact: true});
    await dark.check();
    await page.waitForFunction(() => document.documentElement.dataset.resolvedTheme === 'dark');
    assert.equal(await page.locator('html').getAttribute('data-resolved-theme'), 'dark');
    await screenshot('dark');
    // Native radio arrow navigation changes theme without custom key handlers.
    await dark.focus(); await page.keyboard.press('ArrowLeft');
    assert.equal(await page.getByRole('radio', {name: '浅色', exact: true}).isChecked(), true);
    assert.equal(await page.locator('html').getAttribute('data-resolved-theme'), 'light');
    await page.getByRole('radio', {name: '自动', exact: true}).check();
    await page.emulateMedia({colorScheme: 'dark', reducedMotion: 'reduce'});
    await page.waitForFunction(() => document.documentElement.dataset.resolvedTheme === 'dark');
    assert.equal(await page.locator('html').getAttribute('data-resolved-theme'), 'dark');
    await page.reload();
    await page.getByRole('radio', {name: '自动', exact: true}).waitFor();
    assert.equal(await page.getByRole('radio', {name: '自动', exact: true}).isChecked(), true);
    await page.getByRole('button', {name: '关闭设置', exact: true}).click();
    await dialog.waitFor({state: 'detached'});
    await page.getByRole('button', {name: '外观主题', exact: true}).click();
    await page.getByRole('menuitemradio', {name: '浅色', exact: true}).click();
    await page.evaluate(() => window.dispatchEvent(new CustomEvent('fleet:navigate', {detail: {path: '/settings/appearance'}})));
    await page.getByRole('radio', {name: '浅色', exact: true}).waitFor();
    assert.equal(await page.getByRole('radio', {name: '浅色', exact: true}).isChecked(), true, 'cached cards synchronize with toolbar');
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({viewport, categories: 10, layout: true, nativeThemes: true, pageErrors: errors}));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
