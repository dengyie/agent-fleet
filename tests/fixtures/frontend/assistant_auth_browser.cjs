const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const [origin] = process.argv.slice(2);
(async () => {
  const browser = await chromium.launch({channel: process.env.FLEET_BROWSER_CHANNEL || 'chrome', headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 390, height: 844}});
    page.setDefaultTimeout(5000);
    const errors = [], featureRequests = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => {
      if (/^\/api\/(platform|status|stream|tasks|sessions|machines)/.test(new URL(request.url()).pathname)) featureRequests.push(request.url());
    });
    async function expectLogin(returnTo) {
      await page.waitForURL(url => url.pathname === '/login', {waitUntil: 'domcontentloaded'});
      await page.getByRole('heading', {name: '登录工作空间', exact: true}).waitFor();
      assert.equal(new URL(page.url()).searchParams.get('return_to'), returnTo);
      assert.equal(await page.locator('.console-layout').isVisible(), false);
    }
    async function login() {
      await page.getByLabel('操作员令牌', {exact: true}).fill('browser-fixture-token');
      await page.getByRole('button', {name: '登录', exact: true}).click();
      await page.waitForURL(url => url.pathname !== '/login', {waitUntil: 'domcontentloaded'});
    }
    for (const route of ['/', '/assistant', '/monitoring', '/machine/example', '/task/example', '/session/example', '/conversation/conv-auth']) {
      await page.goto(origin + route, {waitUntil: 'domcontentloaded'});
      await expectLogin(route);
    }
    assert.deepEqual(featureRequests, [], 'protected data and SSE must not load before session validation');
    await page.goto(origin + '/assistant', {waitUntil: 'domcontentloaded'});
    await expectLogin('/assistant');
    const output = process.env.FLEET_SCREENSHOTS || '/tmp/agent-fleet-console-evidence';
    fs.mkdirSync(output, {recursive: true});
    await page.screenshot({path: path.join(output, 'login-mobile.png'), fullPage: true});
    await page.setViewportSize({width: 1440, height: 1000});
    await page.screenshot({path: path.join(output, 'login-desktop.png'), fullPage: true});
    await page.setViewportSize({width: 390, height: 844});
    await page.getByLabel('操作员令牌', {exact: true}).fill('incorrect-fixture');
    await page.getByRole('button', {name: '登录', exact: true}).click();
    await page.getByText('令牌无效或登录已失效，请重新输入。', {exact: true}).waitFor();
    assert.equal(await page.evaluate(() => sessionStorage.getItem('fleet_operator_token')), null);
    await login();
    await page.getByText('就绪', {exact: true}).waitFor();
    const model = page.getByRole('combobox', {name: '模型', exact: true});
    const workspace = page.getByRole('combobox', {name: '工作区', exact: true});
    const send = page.getByRole('button', {name: '发送任务'});
    assert.equal(await model.inputValue(), 'fixture');
    assert.equal(await workspace.inputValue(), 'default');
    assert.equal(await send.isDisabled(), false);
    await page.locator('#sidebar-conversations a').filter({hasText: '登录恢复验收'}).waitFor();
    await page.evaluate(() => sessionStorage.setItem('fleet_operator_token', 'expired-fixture'));
    await page.goto(origin + '/conversation/conv-auth?keep=1#history', {waitUntil: 'domcontentloaded'});
    await expectLogin('/conversation/conv-auth?keep=1#history');
    await login();
    await page.waitForFunction(() => document.querySelector('[aria-label="工作区"]').value === 'saved');
    assert.equal(await workspace.isDisabled(), true);
    assert.equal(new URL(page.url()).hash, '#history');

    const sessionRoute = '**/api/operator/session';
    await page.route(sessionRoute, route => route.fulfill({status: 401, contentType: 'application/json', body: JSON.stringify({error: 'unauthorized'})}));
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    await expectLogin('/conversation/conv-auth?keep=1#history');
    await page.unroute(sessionRoute);
    await login();
    await page.waitForFunction(() => document.querySelector('[aria-label="工作区"]').value === 'saved');

    const defaultsRoute = '**/api/platform/v1/defaults';
    await page.route(defaultsRoute, route => route.fulfill({status: 503, contentType: 'application/json',
      body: JSON.stringify({error: 'unavailable', detail: 'catalog unavailable', request_id: 'catalog-check'})}));
    await page.goto(origin + '/assistant', {waitUntil: 'domcontentloaded'});
    await page.getByText('平台配置加载失败', {exact: true}).waitFor();
    assert.ok((await page.locator('.assistant-initialization').textContent()).includes('catalog-check'));
    assert.equal(await send.isDisabled(), true);
    await page.unroute(defaultsRoute);
    await page.getByRole('button', {name: '重试加载', exact: true}).click();
    await page.getByText('就绪', {exact: true}).waitFor();
    const conversationRoute = '**/api/platform/v1/conversations/conv-auth';
    await page.route(conversationRoute, route => route.fulfill({status: 503, contentType: 'application/json',
      body: JSON.stringify({error: 'unavailable', detail: 'recovery unavailable'})}));
    await page.goto(origin + '/conversation/conv-auth', {waitUntil: 'domcontentloaded'});
    await page.getByText('会话恢复失败', {exact: true}).waitFor();
    assert.equal(await send.isDisabled(), true);
    await page.unroute(conversationRoute);
    await page.getByRole('button', {name: '重试加载', exact: true}).click();
    await page.waitForFunction(() => document.querySelector('[aria-label="工作区"]').value === 'saved');
    await page.waitForFunction(() => !document.querySelector('[aria-label="发送任务"]').disabled);

    // A 401 after a successful entry check must also redirect, even non-JSON errors.
    await page.route(defaultsRoute, route => route.fulfill({status: 401, contentType: 'text/html', body: 'unauthorized'}));
    await page.goto(origin + '/assistant', {waitUntil: 'domcontentloaded'});
    await expectLogin('/assistant');
    assert.equal(await page.evaluate(() => sessionStorage.getItem('fleet_operator_token')), null);
    await page.unroute(defaultsRoute);
    await login();
    await page.getByText('就绪', {exact: true}).waitFor();
    await page.route(defaultsRoute, route => route.fulfill({status: 403, contentType: 'application/json', body: JSON.stringify({error: 'forbidden', detail: 'denied'})}));
    await page.reload({waitUntil: 'domcontentloaded'});
    await page.getByText('当前操作员无访问权限', {exact: true}).waitFor();
    assert.equal(new URL(page.url()).pathname, '/assistant', 'resource 403 must not log out a valid operator');
    await page.unroute(defaultsRoute);
    await page.route(sessionRoute, route => route.fulfill({status: 503, contentType: 'application/json', body: JSON.stringify({error: 'unavailable', detail: 'session unavailable'})}));
    await page.goto(origin + '/monitoring', {waitUntil: 'domcontentloaded'});
    await page.getByText('暂时无法验证登录状态', {exact: true}).waitFor();
    assert.equal(await page.locator('.console-layout').isVisible(), false);
    assert.equal(await page.evaluate(() => sessionStorage.getItem('fleet_operator_token')), 'browser-fixture-token');
    await page.unroute(sessionRoute);
    await page.getByRole('button', {name: '重新验证', exact: true}).click();
    await page.locator('.monitoring-view').waitFor();
    await page.getByRole('button', {name: '退出登录', exact: true}).click();
    await expectLogin('/monitoring');
    await page.goBack({waitUntil: 'domcontentloaded'});
    await page.waitForURL(url => url.pathname === '/login', {waitUntil: 'domcontentloaded'});
    assert.equal(await page.locator('.console-layout').isVisible(), false);
    for (const unsafe of ['https://foreign.invalid/', '//foreign.invalid/', '/login?return_to=/login']) {
      await page.goto(origin + '/login?return_to=' + encodeURIComponent(unsafe), {waitUntil: 'domcontentloaded'});
      await login();
      await page.getByText('就绪', {exact: true}).waitFor();
      assert.equal(new URL(page.url()).pathname, '/assistant');
      await page.getByRole('button', {name: '退出登录', exact: true}).click();
      await page.waitForURL(url => url.pathname === '/login', {waitUntil: 'domcontentloaded'});
    }
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert.deepEqual(errors, []);
    console.log('PASS: page guards, real token login, return path, expiry, logout/back, 401/403/503, retry, no open redirect');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
