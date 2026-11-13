const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const [origin, mode] = process.argv.slice(2);
const prefix = origin + '/api/platform/v1/workflow-summaries';
const password = 'safe-password-123!';

(async () => {
  const browser = await chromium.launch({channel: process.env.FLEET_BROWSER_CHANNEL || 'chrome', headless: true});
  try {
    const context = await browser.newContext({viewport: {width: 1280, height: 900}});
    const email = mode === 'user' ? 'user@example.test' : 'owner@example.test';
    assert.equal((await context.request.post(origin + '/api/accounts/login', {data: {email, password}, headers: {Origin: origin}})).status(), 200);
    const page = await context.newPage(); page.setDefaultTimeout(10000);
    const mutations = [];
    const summaryResponses = [];
    page.on('response', response => { if (response.url().startsWith(prefix)) summaryResponses.push([response.status(), response.url()]); });
    page.on('request', request => { if (request.url().startsWith(prefix) && request.method() === 'POST') mutations.push(request.url()); });
    await page.goto(origin + '/settings/integrations', {waitUntil: 'domcontentloaded'});
    await page.getByRole('navigation', {name: '设置分类', exact: true}).waitFor();
    const panel = page.getByRole('region', {name: '工作流摘要管理', exact: true});
    if (mode === 'user') {
      assert.equal(await panel.count(), 0);
    } else {
      await panel.getByLabel('摘要工作区', {exact: true}).waitFor();
      const id = panel.getByLabel('摘要 ID', {exact: true});
      await id.fill('manual-summary');
      await panel.getByLabel('标题', {exact: true}).fill('性能测试注意事项');
      await panel.getByLabel('完整内容（最多 8192 字节 UTF-8）', {exact: true}).fill('第一版完整内容\n保留换行。');
      await panel.getByRole('button', {name: '发布新版本', exact: true}).click();
      try { await panel.getByText('已发布；已读取服务端当前版本 1', {exact: true}).waitFor(); }
      catch (error) { throw new Error(error.message + '\nstatus=' + await panel.locator('[role=status]').textContent() + '\nresponses=' + JSON.stringify(summaryResponses)); }
      await panel.getByText('第一版完整内容\n保留换行。', {exact: true}).waitFor();
      assert.equal(mutations.length, 1);
      await panel.getByLabel('标题', {exact: true}).fill('性能测试注意事项更新');
      await panel.getByLabel('完整内容（最多 8192 字节 UTF-8）', {exact: true}).fill('第二版完整内容');
      await panel.getByRole('button', {name: '发布新版本', exact: true}).click();
      await panel.getByText('已发布；已读取服务端当前版本 2', {exact: true}).waitFor();
      const historyCount = await panel.locator('input[name="revision"]').count();
      if (historyCount !== 1) throw new Error('history control missing; panel=' + await panel.innerText());
      await panel.locator('input[name="revision"]').fill('1');
      await panel.getByRole('button', {name: '读取版本', exact: true}).click();
      await panel.getByText('第一版完整内容\n保留换行。', {exact: true}).waitFor();
      await panel.getByRole('button', {name: '停用摘要', exact: true}).click();
      await panel.getByText('摘要已停用', {exact: true}).waitFor();
      assert.equal(mutations.length, 3);
    }
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false);
    await context.close();
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
