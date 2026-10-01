const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const [origin] = process.argv.slice(2);
(async () => {
  const browser = await chromium.launch({channel: process.env.FLEET_BROWSER_CHANNEL || 'chrome', headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 390, height: 844}});
    page.setDefaultTimeout(5000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const notice = page.locator('.assistant-initialization');
    const send = page.getByRole('button', {name: '发送任务'});
    const model = page.getByRole('combobox', {name: '模型', exact: true});
    const workspace = page.getByRole('combobox', {name: '工作区', exact: true});
    await page.goto(origin + '/assistant');
    await page.getByText('请先登录操作员', {exact: true}).waitFor();
    assert.equal(await send.isDisabled(), true);
    assert.equal(await model.isDisabled(), true);
    assert.equal(await workspace.isDisabled(), true);
    assert.equal(await page.getByText('平台配置加载失败', {exact: true}).count(), 0);
    await notice.getByRole('button', {name: '操作员登录', exact: true}).click();
    await page.getByLabel('操作员令牌', {exact: true}).fill('incorrect-fixture');
    await page.getByRole('button', {name: '验证并登录'}).click();
    await page.getByText('令牌无效或无操作权限', {exact: true}).waitFor();
    assert.equal(await page.evaluate(() => sessionStorage.getItem('fleet_operator_token')), null);
    await page.getByLabel('操作员令牌', {exact: true}).fill('browser-fixture-token');
    await Promise.all([page.waitForEvent('load'), page.getByRole('button', {name: '验证并登录'}).click()]);
    await page.getByText('就绪', {exact: true}).waitFor();
    assert.equal(await model.inputValue(), 'fixture');
    assert.equal(await workspace.inputValue(), 'default');
    assert.equal(await send.isDisabled(), false);
    await page.locator('#sidebar-conversations a').filter({hasText: '登录恢复验收'}).waitFor();
    assert.equal(await notice.isVisible(), false);

    // Invalid persisted token, and login from an existing conversation URL.
    await page.evaluate(() => sessionStorage.setItem('fleet_operator_token', 'expired-fixture'));
    await page.goto(origin + '/conversation/conv-auth');
    await page.getByText('请先登录操作员', {exact: true}).waitFor();
    await notice.getByRole('button', {name: '操作员登录', exact: true}).click();
    await page.getByLabel('操作员令牌', {exact: true}).fill('browser-fixture-token');
    await Promise.all([page.waitForEvent('load'), page.getByRole('button', {name: '验证并登录'}).click()]);
    await page.waitForFunction(() => document.querySelector('[aria-label="工作区"]').value === 'saved');
    assert.ok(page.url().endsWith('/conversation/conv-auth'));
    assert.equal(await workspace.isDisabled(), true);

    const defaultsRoute = '**/api/platform/v1/defaults';
    await page.route(defaultsRoute, route => route.fulfill({status: 503, contentType: 'application/json',
      body: JSON.stringify({error: 'unavailable', detail: 'catalog unavailable', request_id: 'catalog-check'})}));
    await page.goto(origin + '/assistant');
    await page.getByText('平台配置加载失败', {exact: true}).waitFor();
    assert.ok((await notice.textContent()).includes('catalog-check'));
    assert.equal(await send.isDisabled(), true);
    assert.equal(await notice.getByRole('button', {name: '操作员登录', exact: true}).count(), 0);
    await page.unroute(defaultsRoute);
    await notice.getByRole('button', {name: '重试加载', exact: true}).click();
    await page.getByText('就绪', {exact: true}).waitFor();
    assert.equal(await send.isDisabled(), false);

    const conversationRoute = '**/api/platform/v1/conversations/conv-auth';
    await page.route(conversationRoute, route => route.fulfill({status: 503, contentType: 'application/json',
      body: JSON.stringify({error: 'unavailable', detail: 'recovery unavailable'})}));
    await page.goto(origin + '/conversation/conv-auth');
    await page.getByText('会话恢复失败', {exact: true}).waitFor();
    assert.equal(await send.isDisabled(), true, 'do not submit into a conversation whose state is unknown');
    assert.equal(await page.getByText('平台配置加载失败', {exact: true}).count(), 0);
    await page.unroute(conversationRoute);
    await notice.getByRole('button', {name: '重试加载', exact: true}).click();
    await page.waitForFunction(() => document.querySelector('[aria-label="工作区"]').value === 'saved');
    await page.waitForFunction(() => !document.querySelector('[aria-label="发送任务"]').disabled);

    await page.route(defaultsRoute, route => route.fulfill({status: 403, contentType: 'application/json',
      body: JSON.stringify({error: 'forbidden', detail: 'denied'})}));
    await page.goto(origin + '/assistant');
    await page.getByText('当前操作员无访问权限', {exact: true}).waitFor();
    assert.equal(await send.isDisabled(), true);
    assert.equal(await notice.getByRole('button', {name: '切换操作员', exact: true}).isVisible(), true);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert.deepEqual(errors, []);
    console.log('PASS: real anonymous/bad-token 401, login recovery, catalog 503 retry, conversation recovery retry, 403');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
