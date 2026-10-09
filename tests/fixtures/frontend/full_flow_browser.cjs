const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const fs = require('node:fs');
const origin = process.argv[2];

(async () => {
  const browser = await chromium.launch({channel: process.env.FLEET_BROWSER_CHANNEL || 'chrome', headless: true});
  const context = await browser.newContext({acceptDownloads: true});
  let page = await context.newPage();
  const errors = [];
  const watch = p => {p.setDefaultTimeout(12000); p.on('pageerror', e => errors.push(e.message));};
  watch(page);
  try {
    // SPA readiness is asserted after the auth redirect below. Waiting for
    // every page resource's `load` event here makes the journey depend on
    // nondeterministic local browser scheduling.
    await page.goto(origin + '/assistant', {waitUntil: 'domcontentloaded'});
    await page.waitForURL('**/login?**', {waitUntil: 'domcontentloaded'});
    await page.getByLabel('账号或邮箱', {exact: true}).fill('mango');
    await page.getByLabel('密码', {exact: true}).fill('Full-flow-password-123!');
    await page.getByRole('button', {name: '登录', exact: true}).click();
    await page.waitForURL('**/assistant', {waitUntil: 'domcontentloaded'});
    await page.getByText('就绪', {exact: true}).waitFor();
    await page.getByRole('textbox', {name: '给助手的任务'}).fill('slow full flow');
    await page.getByRole('button', {name: '发送任务'}).click();
    await page.waitForURL('**/conversation/**', {waitUntil: 'domcontentloaded'});
    await page.locator('.assistant-run-status[data-state="running"]').waitFor();
    const conversationUrl = page.url();
    await page.close();
    assert.equal((await context.request.post(origin + '/__test/release-provider')).status(), 200);
    page = await context.newPage(); watch(page);
    await page.goto(conversationUrl);
    await page.getByText('Full flow completed: 中文回复', {exact: true}).waitFor();
    await page.locator('.assistant-run-status[data-state="succeeded"]').waitFor();
    await page.getByRole('tab', {name: '运行', exact: true}).click();
    for (const name of ['workspace.list', 'workspace.write', 'workspace.read', 'workspace.artifact']) {
      await page.locator('.assistant-events').getByText(name, {exact: true}).waitFor();
    }
    await page.getByRole('tab', {name: '产物', exact: true}).click();
    await page.getByRole('button', {name: '预览', exact: true}).click();
    await page.getByText('Full-flow artifact: 中文内容', {exact: false}).waitFor();
    const downloadPromise = page.waitForEvent('download');
    await page.getByRole('link', {name: '下载', exact: true}).click();
    const download = await downloadPromise;
    assert.equal(fs.readFileSync(await download.path(), 'utf8'), 'Full-flow artifact: 中文内容\nverified bytes\n');
    await page.reload();
    await page.getByText('Full flow completed: 中文回复', {exact: true}).waitFor();
    assert.equal(await page.getByText('Full flow completed: 中文回复', {exact: true}).count(), 1);
    await page.waitForFunction(() => !document.querySelector('.assistant-composer textarea').readOnly);
    await page.getByRole('textbox', {name: '给助手的任务'}).fill('http-502');
    await page.getByRole('button', {name: '发送任务'}).click();
    await page.locator('.assistant-run-status[data-state="unknown"]').waitFor();
    await page.locator('.assistant-events').getByText(/HTTP 502/).waitFor();
    await page.reload();
    await page.locator('.assistant-run-status[data-state="unknown"]').waitFor();
    await page.locator('.assistant-events').getByText(/HTTP 502/).waitFor();
    await page.getByText('Full flow completed: 中文回复', {exact: true}).waitFor();
    assert.deepEqual(errors, []);
    if (process.env.FLEET_SCREENSHOTS) {
      fs.mkdirSync(process.env.FLEET_SCREENSHOTS, {recursive: true});
      await page.screenshot({path: process.env.FLEET_SCREENSHOTS + '/full-flow-recovered.png', fullPage: true});
    }
    await page.getByRole('button', {name: '退出登录', exact: true}).click();
    await page.waitForURL('**/login**', {waitUntil: 'domcontentloaded'});
    await page.goto(conversationUrl);
    await page.waitForURL('**/login?**', {waitUntil: 'domcontentloaded'});
    console.log('PASS: account → real HTTP model → tools → artifact → tab close/recovery → failure → logout');
  } catch (error) {
    if (process.env.FLEET_SCREENSHOTS && !page.isClosed()) {
      fs.mkdirSync(process.env.FLEET_SCREENSHOTS, {recursive: true});
      await page.screenshot({path: process.env.FLEET_SCREENSHOTS + '/full-flow-failure.png', fullPage: true});
    }
    throw error;
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
