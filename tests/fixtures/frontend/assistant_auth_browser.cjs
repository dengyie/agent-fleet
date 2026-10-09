const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const [origin] = process.argv.slice(2);
(async () => {
  const browser = await chromium.launch({channel: process.env.FLEET_BROWSER_CHANNEL || 'chrome', headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 390, height: 844}});
    await page.addInitScript(() => {
      const originalNow = Date.now.bind(Date);
      let shift = 0;
      Date.now = () => originalNow() + shift;
      window.advanceFleetTime = amount => { shift += amount; };
    });
    page.setDefaultTimeout(5000);
    const errors = [], featureRequests = [];
    let sessionRequests = 0;
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => {
      if (new URL(request.url()).pathname === '/api/operator/session') sessionRequests++;
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
      assert.equal(await page.getByText('正在验证登录状态…', {exact: true}).count(), 0);
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
    const checksAtEntry = sessionRequests;
    await page.evaluate(() => { window.dispatchEvent(new Event('focus')); window.dispatchEvent(new Event('focus')); });
    await page.waitForTimeout(100);
    assert.equal(sessionRequests, checksAtEntry, 'focus does not bypass the one-minute session interval');
    await page.setViewportSize({width: 1440, height: 900});
    const documentTimeOrigin = await page.evaluate(() => performance.timeOrigin);
    await page.locator('[data-nav="monitoring"]').click();
    await page.getByRole('heading', {name: '服务监控', exact: true}).waitFor();
    assert.equal(sessionRequests, checksAtEntry, 'in-app navigation does not repeat session validation');
    assert.equal(await page.evaluate(() => performance.timeOrigin), documentTimeOrigin,
      'in-app navigation keeps the current document alive');
    assert.equal(await page.locator('.console-layout').isVisible(), true, 'the console stays visible during route changes');
    assert.equal(await page.locator('#access-state').isVisible(), false, 'route changes do not show an authentication screen');
    await page.goBack();
    await page.getByRole('combobox', {name: '模型', exact: true}).waitFor();
    assert.equal(sessionRequests, checksAtEntry, 'browser history navigation does not repeat session validation');
    assert.equal(await page.evaluate(() => performance.timeOrigin), documentTimeOrigin,
      'browser history navigation keeps the current document alive');
    await page.setViewportSize({width: 390, height: 844});
    await page.locator('#mobile-menu').click();
    await page.locator('[data-nav="monitoring"]').click();
    await page.getByRole('heading', {name: '服务监控', exact: true}).waitFor();
    assert.equal(sessionRequests, checksAtEntry, 'mobile navigation does not repeat session validation');
    assert.equal(await page.evaluate(() => performance.timeOrigin), documentTimeOrigin,
      'mobile navigation keeps the current document alive');
    assert.equal(await page.locator('body').evaluate(node => node.classList.contains('navigation-open')), false,
      'mobile navigation closes the drawer after route changes');
    await page.goBack();
    await page.getByRole('combobox', {name: '模型', exact: true}).waitFor();
    await page.setViewportSize({width: 1440, height: 900});
    const model = page.getByRole('combobox', {name: '模型', exact: true});
    const workspace = page.getByRole('combobox', {name: '工作区', exact: true});
    const send = page.getByRole('button', {name: '发送任务'});
    assert.equal(await model.inputValue(), 'fixture');
    assert.equal(await workspace.inputValue(), 'default');
    assert.equal(await send.isDisabled(), false);
    await page.locator('#sidebar-conversations a').filter({hasText: '登录恢复验收'}).waitFor();
    await page.setViewportSize({width: 1280, height: 900});
    const divider = page.locator('#sidebar-nodes-resize');
    const initialNodeHeight = Number(await divider.getAttribute('aria-valuenow'));
    const sidebarGeometry = async () => page.evaluate(() => {
      const bounds = selector => {
        const {top, height, bottom} = document.querySelector(selector).getBoundingClientRect();
        return {top, height, bottom};
      };
      return {
        history: bounds('#sidebar-conversations'),
        nodes: bounds('#sidebar-nodes'),
        footer: bounds('.sidebar-footer'),
      };
    });
    const beforeKeyboardResize = await sidebarGeometry();
    await divider.focus();
    await divider.press('ArrowUp');
    const increasedNodeHeight = Number(await divider.getAttribute('aria-valuenow'));
    assert.equal(increasedNodeHeight, Math.min(initialNodeHeight + 16, Number(await divider.getAttribute('aria-valuemax'))));
    assert.equal((await sidebarGeometry()).footer.top, beforeKeyboardResize.footer.top,
      'keyboard resizing keeps the self-hosted footer anchored');
    assert.equal(await page.evaluate(() => localStorage.getItem('fleet-sidebar-nodes-height')),
      String(increasedNodeHeight));
    const afterKeyboardIncrease = await sidebarGeometry();
    await divider.press('ArrowDown');
    assert.equal((await sidebarGeometry()).footer.top, afterKeyboardIncrease.footer.top,
      'keyboard resizing leaves the self-hosted footer anchored');
    const beforeDrag = await sidebarGeometry();
    const dividerBounds = await divider.boundingBox();
    await page.mouse.move(dividerBounds.x + dividerBounds.width / 2, dividerBounds.y + dividerBounds.height / 2);
    await page.mouse.down();
    await page.mouse.move(dividerBounds.x + dividerBounds.width / 2, dividerBounds.y - 48, {steps: 8});
    await page.mouse.up();
    const afterDrag = await sidebarGeometry();
    assert.equal(afterDrag.footer.top, beforeDrag.footer.top, 'resizing the lists keeps the self-hosted footer anchored: ' +
      JSON.stringify({beforeDrag, afterDrag}));
    assert.equal(afterDrag.footer.height, beforeDrag.footer.height, 'resizing the lists leaves the self-hosted footer size unchanged');
    assert.ok(afterDrag.nodes.height > beforeDrag.nodes.height, 'dragging upward expands the node list');
    assert.ok(afterDrag.history.height < beforeDrag.history.height, 'the conversation list gives space to the node list');
    assert.equal(await page.evaluate(() => localStorage.getItem('fleet-sidebar-nodes-height')),
      await divider.getAttribute('aria-valuenow'), 'dragged height persists for the next visit');

    const historySearch = page.getByRole('searchbox', {name: '搜索最近对话'});
    await historySearch.fill('归档');
    const searchState = await page.locator('#sidebar-conversations .conversation-history-entry').evaluateAll(entries =>
      entries.map(entry => ({searchText: entry.dataset.searchText, hidden: entry.hidden,
        display: getComputedStyle(entry).display, text: entry.innerText})));
    assert.equal(await page.locator('#sidebar-conversations .conversation-history-entry:visible').count(), 0,
      'search matches conversation content, not action labels: ' + JSON.stringify({
        query: await historySearch.inputValue(), status: await page.locator('#conversation-search-status').textContent(),
        entries: searchState,
      }));
    await historySearch.fill('');
    let historyEntry = page.locator('#sidebar-conversations .conversation-history-entry').filter({hasText: '历史操作验收'});
    await historyEntry.locator('summary').click();
    await historyEntry.getByRole('button', {name: '重命名'}).click();
    await page.locator('#conversation-title-input').fill('   ');
    await page.getByRole('button', {name: '保存', exact: true}).click();
    await page.getByRole('dialog', {name: '重命名对话'}).waitFor();
    await page.locator('#conversation-title-error').waitFor({state: 'visible'});
    assert.equal(await page.locator('#conversation-title-input').inputValue(), '   ');
    await page.route('**/api/platform/v1/conversations/conv-controls', route => {
      if (route.request().method() === 'PATCH') {
        return route.fulfill({status:503, contentType:'application/json', body:JSON.stringify({error:'temporary', detail:'rename unavailable'})});
      }
      return route.continue();
    });
    await page.locator('#conversation-title-input').fill('保留失败后的输入');
    await page.getByRole('button', {name: '保存', exact: true}).click();
    await page.getByText('rename unavailable', {exact:true}).waitFor();
    await page.getByRole('dialog', {name: '重命名对话'}).waitFor();
    assert.equal(await page.locator('#conversation-title-input').inputValue(), '保留失败后的输入');
    await page.unroute('**/api/platform/v1/conversations/conv-controls');
    await page.locator('#conversation-title-input').fill('改名后的对话');
    await page.getByRole('button', {name: '保存', exact: true}).click();
    await page.locator('#sidebar-conversations a').filter({hasText: '改名后的对话'}).waitFor();
    historyEntry = page.locator('#sidebar-conversations .conversation-history-entry').filter({hasText: '改名后的对话'});
    await historyEntry.locator('summary').click();
    await historyEntry.getByRole('button', {name: '归档'}).click();
    await historyEntry.waitFor({state: 'detached'});
    await page.getByRole('button', {name: '查看归档'}).click();
    await page.getByRole('searchbox', {name: '搜索已归档对话'}).fill('永久删除');
    assert.equal(await page.locator('#sidebar-conversations .conversation-history-entry:visible').count(), 0,
      'archived search excludes action menu labels');
    await page.getByRole('searchbox', {name: '搜索已归档对话'}).fill('');
    historyEntry = page.locator('#sidebar-conversations .conversation-history-entry').filter({hasText: '改名后的对话'});
    await historyEntry.waitFor();
    await historyEntry.locator('a').click();
    await page.waitForURL(url => url.pathname === '/conversation/conv-controls');
    await page.getByRole('heading', {name: '改名后的对话', exact: true}).waitFor();
    await page.getByText('就绪', {exact: true}).waitFor();
    assert.equal(await send.isDisabled(), true, 'archived conversations cannot send new turns');
    assert.equal(await page.getByLabel('给助手的任务').evaluate(input => input.readOnly), true);
    await page.goto(origin + '/assistant', {waitUntil: 'domcontentloaded'});
    await page.getByText('就绪', {exact: true}).waitFor();
    await page.getByRole('button', {name: '查看归档'}).click();
    historyEntry = page.locator('#sidebar-conversations .conversation-history-entry').filter({hasText: '改名后的对话'});
    await historyEntry.waitFor();
    await historyEntry.locator('summary').click();
    await historyEntry.getByRole('button', {name: '取消归档'}).click();
    await historyEntry.waitFor({state: 'detached'});
    await page.getByRole('button', {name: '最近对话'}).click();
    historyEntry = page.locator('#sidebar-conversations .conversation-history-entry').filter({hasText: '改名后的对话'});
    await historyEntry.waitFor();
    await historyEntry.locator('summary').click();
    await historyEntry.getByRole('button', {name: '归档'}).click();
    await historyEntry.waitFor({state: 'detached'});
    await page.getByRole('button', {name: '查看归档'}).click();
    historyEntry = page.locator('#sidebar-conversations .conversation-history-entry').filter({hasText: '改名后的对话'});
    await historyEntry.waitFor();
    await historyEntry.locator('summary').click();
    await historyEntry.getByRole('button', {name: '永久删除'}).click();
    await page.getByRole('dialog', {name: '永久删除对话？'}).waitFor();
    await page.getByRole('button', {name: '永久删除', exact: true}).click();
    await historyEntry.waitFor({state: 'detached'});
    await page.setViewportSize({width: 390, height: 844});

    await page.evaluate(() => sessionStorage.setItem('fleet_operator_token', 'expired-fixture'));
    await page.goto(origin + '/conversation/conv-auth?keep=1#history', {waitUntil: 'domcontentloaded'});
    await expectLogin('/conversation/conv-auth?keep=1#history');
    await login();
    await page.waitForFunction(() => document.querySelector('[aria-label="工作区"]').value === 'saved');
    assert.equal(await workspace.isDisabled(), true);
    assert.equal(new URL(page.url()).hash, '#history');

    const sessionRoute = '**/api/operator/session';
    await page.route(sessionRoute, route => route.fulfill({status: 401, contentType: 'application/json', body: JSON.stringify({error: 'unauthorized'})}));
    await page.evaluate(() => { window.advanceFleetTime(60000); window.dispatchEvent(new Event('focus')); });
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
    const featureRequestCount = featureRequests.length;
    await page.goto(origin + '/monitoring', {waitUntil: 'domcontentloaded'});
    await page.getByText('工作空间暂时无法连接，稍后会自动重试。', {exact: true}).waitFor();
    assert.equal(await page.locator('.console-layout').isVisible(), true);
    assert.equal(await page.locator('#access-state').isVisible(), false);
    assert.equal(featureRequests.length, featureRequestCount);
    assert.equal(await page.evaluate(() => sessionStorage.getItem('fleet_operator_token')), 'browser-fixture-token');
    await page.unroute(sessionRoute);
    await page.evaluate(() => { window.advanceFleetTime(60000); window.dispatchEvent(new Event('focus')); });
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
