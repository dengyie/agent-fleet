const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const [origin, role] = process.argv.slice(2);

(async () => {
  const browser = await chromium.launch({channel: process.env.FLEET_BROWSER_CHANNEL || 'chrome', headless: true});
  try {
    const page = await browser.newPage();
    page.setDefaultTimeout(10000);
    const requests = [], denied = [], errors = [], turns = [];
    const adminApi = /\/api\/platform\/v1\/(memory|execution-windows|commands|legacy-tasks)(\/|\?|$)/;
    page.on('request', request => {
      if (adminApi.test(request.url())) requests.push(request.url());
      if (request.method() === 'POST' && request.url().endsWith('/turns')) turns.push(request.postDataJSON());
    });
    page.on('response', response => { if (response.status() === 403) denied.push(response.url()); });
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(origin + '/login?return_to=%2Fassistant');
    await page.getByLabel('账号或邮箱', {exact: true}).fill(role === 'admin' ? 'mango' : role + '@example.test');
    await page.getByLabel('密码', {exact: true}).fill('Review-password-123!');
    await page.getByRole('button', {name: '登录', exact: true}).click();
    await page.waitForURL('**/assistant');
    await page.getByText('就绪', {exact: true}).waitFor();

    const restrictedPanels = '.assistant-memory-context, .assistant-execution-window, .assistant-legacy-section, .assistant-unknown-section';
    if (role === 'user') {
      assert.equal(await page.locator(restrictedPanels).count(), 0, 'ordinary users must not mount administrator controls');
      assert.deepEqual(requests, [], 'ordinary initialization must not call administrator APIs');
    } else {
      assert.equal(await page.locator(restrictedPanels).count(), 4);
      await page.getByRole('tab', {name: '上下文', exact: true}).click();
      const searched = page.waitForResponse(response => response.url().includes('/memory/search'));
      await page.getByRole('searchbox', {name: '搜索记忆'}).fill('fixture');
      await page.getByRole('button', {name: '搜索', exact: true}).click();
      assert.equal((await searched).status(), 200);
      await page.locator('.assistant-memory-row input').check();
      assert.equal(await page.locator('#assistant-memory-enabled').isChecked(), false);
    }

    await page.getByRole('textbox', {name: '给助手的任务'}).fill('finish this run');
    await page.getByRole('button', {name: '发送任务'}).click();
    await page.getByText('Account assistant completed', {exact: true}).waitFor();
    await page.waitForFunction(() => !document.querySelector('.assistant-composer textarea').readOnly);
    assert.equal(turns[0].memory_context, undefined, 'selecting memory alone must not opt in');
    await page.reload();
    await page.getByText('Account assistant completed', {exact: true}).waitFor();
    await page.getByRole('button', {name: '预览', exact: true}).click();
    await page.getByText('Owner-scoped artifact preview', {exact: true}).waitFor();
    await page.waitForFunction(() => !document.querySelector('.assistant-composer textarea').readOnly);
    if (role === 'admin') {
      await page.getByRole('tab', {name: '上下文', exact: true}).click();
      await page.locator('#assistant-memory-enabled').check();
      assert.equal(await page.locator('.assistant-memory-row input').isChecked(), false);
    }

    await page.getByRole('textbox', {name: '给助手的任务'}).fill('cancel this run');
    await page.getByRole('button', {name: '发送任务'}).click();
    await page.locator('.assistant-run-status[data-state="running"]').waitFor();
    await page.getByRole('button', {name: '取消运行', exact: true}).click();
    await page.locator('.assistant-run-status[data-state="cancelled"]').waitFor();
    await page.waitForFunction(() => !document.querySelector('.assistant-composer textarea').readOnly);
    assert.equal(turns[1].memory_context, undefined, 'opting in without selected memory must not send context');
    if (role === 'user') {
      assert.deepEqual(requests, [], 'submission, recovery and cancellation must not call administrator APIs');
      assert.equal(await page.locator(restrictedPanels).count(), 0);
    } else {
      assert.ok(requests.some(url => url.includes('/commands/unknown')));
      assert.ok(requests.some(url => url.includes('/execution-windows')));
      await page.locator('.assistant-memory-row input').check();
      await page.getByRole('textbox', {name: '给助手的任务'}).fill('use selected memory');
      await page.getByRole('button', {name: '发送任务'}).click();
      await page.locator('.assistant-run-status[data-state="succeeded"]').waitFor();
      await page.waitForFunction(() => !document.querySelector('.assistant-composer textarea').readOnly);
      assert.equal(turns[2].memory_context.enabled, true);
      assert.deepEqual(turns[2].memory_context.memory_ids, ['fixture-memory']);
      assert.equal(typeof turns[2].memory_context.revisions['fixture-memory'], 'number');
      assert.equal(JSON.stringify(turns[2]).includes('Use the owner workspace.'), false, 'only references belong in the request');
      await page.getByRole('tab', {name: '运行', exact: true}).click();
      await page.locator('.assistant-events').getByText('已载入记忆上下文', {exact: true}).waitFor();
    }
    assert.deepEqual(denied, []);
    assert.deepEqual(errors, []);
    console.log('PASS: ' + role + ' assistant permissions, completion, recovery, artifacts and cancellation');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
