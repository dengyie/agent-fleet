const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const [origin, mode, screenshot] = process.argv.slice(2);

(async () => {
  const browser = await chromium.launch({channel: process.env.FLEET_BROWSER_CHANNEL || 'chrome', headless: true});
  try {
    const context = await browser.newContext({viewport: mode === 'mobile' ? {width: 390, height: 844} : {width: 1280, height: 960}});
    assert.equal((await context.request.post(origin + '/api/accounts/login', {
      data: {email: 'owner@example.test', password: 'safe-password-123!'}, headers: {Origin: origin},
    })).status(), 200);
    const page = await context.newPage(); page.setDefaultTimeout(10000);
    const posts = [];
    page.on('request', request => { if (request.method() === 'POST' && request.url().includes('/workflow-summary-generations')) posts.push(request); });
    await page.goto(origin + '/account', {waitUntil: 'domcontentloaded'});
    const generator = page.getByRole('region', {name: '模型摘要生成', exact: true});
    await generator.getByLabel('生成摘要 ID', {exact: true}).fill('generated-' + mode);
    await generator.getByLabel('生成摘要标题', {exact: true}).fill('实际使用摘要');
    await generator.getByLabel('来源对话', {exact: true}).selectOption('source-chat');
    await generator.locator('input[data-source-run="newer-run-20"]').check();
    await generator.getByRole('button', {name: '加载较早来源运行', exact: true}).click();
    assert.equal(await generator.locator('input[data-source-run="newer-run-20"]').isChecked(), true,
      'Loading an older page must preserve the existing selection');
    await generator.locator('input[data-source-run="newer-run-20"]').uncheck();
    await generator.locator('input[data-source-run="source-run-a"]').check();
    await generator.getByLabel('摘要模型', {exact: true}).selectOption('model-a');
    await generator.getByRole('button', {name: '生成摘要候选', exact: true}).click();
    await generator.getByRole('button', {name: '发布生成摘要', exact: true}).waitFor({state: 'visible'});
    await generator.getByRole('button', {name: '发布生成摘要', exact: true}).click();
    await generator.getByText('已发布生成摘要版本 1', {exact: true}).waitFor();
    const summaries = page.getByRole('region', {name: '工作流摘要管理', exact: true});
    await summaries.getByText('模型生成的完整摘要\n保留决策与未完成工作。\n', {exact: true}).waitFor();
    assert.equal(posts.length, 2);
    const request = posts[0].postDataJSON();
    assert.deepEqual(request.source_run_ids, ['source-run-a']);
    assert.equal(request.model_profile_id, 'model-a');
    assert.equal('content' in posts[1].postDataJSON(), false);
    await page.reload({waitUntil: 'domcontentloaded'});
    await page.getByRole('region', {name: '模型摘要生成', exact: true}).getByText('此结果已发布为版本 1', {exact: true}).waitFor();
    assert.equal(posts.length, 2, 'Reload must not repeat model submission or publication');
    await generator.getByRole('button', {name: '加载较早来源运行', exact: true}).click();
    await generator.locator('input[data-source-run="source-run-a"]').check();
    let lost = false;
    await page.route('**/workflow-summary-generations', async route => {
      if (route.request().method() !== 'POST' || lost) return route.continue();
      lost = true;
      const accepted = await route.fetch();
      assert.equal(accepted.status(), 201);
      await route.fulfill({status: 503, contentType: 'application/json', body: JSON.stringify({ok: false, error: 'response_lost'})});
    });
    await generator.getByRole('button', {name: '生成摘要候选', exact: true}).click();
    await generator.getByRole('button', {name: '读取或恢复生成请求', exact: true}).waitFor({state: 'visible'});
    await page.waitForFunction(() => document.querySelector('.workflow-summary-generation').getAttribute('aria-busy') === 'false');
    assert.equal(lost, true);
    assert.equal(await generator.getByRole('button', {name: '生成摘要候选', exact: true}).isDisabled(), true,
      'An ambiguous accepted response must not permit a new model request even after a prior terminal item');
    await summaries.getByLabel('摘要工作区', {exact: true}).selectOption('workspace-b');
    await generator.getByText('当前摘要工作区：workspace-b', {exact: true}).waitFor();
    assert.equal(await generator.getByLabel('生成摘要 ID', {exact: true}).inputValue(), '');
    assert.equal(await generator.locator('.generation-result').textContent(), '');
    await summaries.getByLabel('摘要工作区', {exact: true}).selectOption('workspace-a');
    await generator.getByRole('button', {name: '发布生成摘要', exact: true}).waitFor({state: 'visible'});
    assert.equal(posts.length, 3, 'Recovery must look up the accepted token without another POST');
    await generator.getByRole('button', {name: '发布生成摘要', exact: true}).click();
    await generator.getByText('已发布生成摘要版本 2', {exact: true}).waitFor();
    assert.equal(posts.length, 4);
    await page.screenshot({path: screenshot, fullPage: true});
    const overflow = await page.evaluate(() => [...document.querySelectorAll('body *')].filter(element => {
      const rect = element.getBoundingClientRect(); return rect.width > 0 && rect.right > innerWidth + 1 && rect.left >= 0;
    }).map(element => ({tag: element.tagName, className: element.className, width: element.getBoundingClientRect().width,
      text: element.textContent.slice(0, 60)})).slice(0, 20));
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, JSON.stringify(overflow));
    await context.close();
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
