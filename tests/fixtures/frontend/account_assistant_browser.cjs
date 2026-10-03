const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const [origin, role] = process.argv.slice(2);

(async () => {
  const browser = await chromium.launch({channel: process.env.FLEET_BROWSER_CHANNEL || 'chrome', headless: true});
  try {
    const page = await browser.newPage();
    page.setDefaultTimeout(10000);
    await page.addInitScript(() => {
      const setInterval = window.setInterval.bind(window);
      window.setInterval = (fn, delay, ...args) => {
        if (delay === 60000) window.runSessionTimer = fn;
        return setInterval(fn, delay, ...args);
      };
    });
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
    // Hold identity validation while ordinary page data remains authorized.
    // The destination and draft must be usable before this request returns.
    let releaseSession;
    const heldSession = new Promise(resolve => { releaseSession = resolve; });
    const sessionRoute = '**/api/operator/session';
    await page.route(sessionRoute, async route => { await heldSession; await route.continue(); });
    await page.getByRole('button', {name: '登录', exact: true}).click();
    await page.waitForURL('**/assistant');
    await page.getByText('就绪', {exact: true}).waitFor();

    assert.equal(await page.locator('#access-state').isVisible(), false);
    assert.equal(await page.locator('.console-layout').isVisible(), true);
    assert.deepEqual(requests, [], 'administrator APIs wait for the verified role');
    assert.deepEqual(denied, []);
    await page.getByRole('textbox', {name: '给助手的任务'}).fill('draft before session validation');
    const documentMarker = await page.evaluate(() => window.authDocumentMarker = Math.random());
    const sessionResponse = page.waitForResponse(response => response.url().endsWith('/api/operator/session'));
    releaseSession();
    await sessionResponse;
    await page.locator('[data-nav="account"]').waitFor();
    await page.unroute(sessionRoute);
    assert.equal(await page.evaluate(() => window.authDocumentMarker), documentMarker, 'first identity resolution must not reload');
    assert.equal(await page.getByRole('textbox', {name: '给助手的任务'}).inputValue(), 'draft before session validation');

    for (const trigger of ['focus', 'pageshow', 'timer']) {
      let releaseCheck;
      const checkHeld = new Promise(resolve => { releaseCheck = resolve; });
      await page.route(sessionRoute, async route => { await checkHeld; await route.continue(); });
      const requested = page.waitForRequest(request => request.url().endsWith('/api/operator/session'));
      await page.evaluate(trigger => {
        if (trigger === 'timer') window.runSessionTimer();
        else if (trigger === 'pageshow') {
          window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}));
          window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}));
        } else window.dispatchEvent(new Event('focus'));
      }, trigger);
      await requested;
      assert.equal(await page.locator('.console-layout').isVisible(), true, trigger + ' must not hide the page');
      assert.equal(await page.locator('#access-state').isVisible(), false);
      await page.getByRole('textbox', {name: '给助手的任务'}).fill('draft during ' + trigger);
      const response = page.waitForResponse(response => response.url().endsWith('/api/operator/session'));
      releaseCheck(); await response; await page.unroute(sessionRoute);
      await page.waitForFunction(() => !document.querySelector('#access-state button').disabled);
      assert.equal(await page.evaluate(() => window.authDocumentMarker), documentMarker);
      assert.equal(await page.getByRole('textbox', {name: '给助手的任务'}).inputValue(), 'draft during ' + trigger);
    }

    await page.route(sessionRoute, route => route.fulfill({status: 503, contentType: 'application/json', body: '{"error":"unavailable"}'}));
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    await page.getByText('暂时无法验证登录状态', {exact: true}).waitFor();
    assert.equal(await page.locator('.console-layout').isVisible(), true);
    assert.equal(await page.getByRole('textbox', {name: '给助手的任务'}).inputValue(), 'draft during timer');
    assert.equal(await page.evaluate(() => window.authDocumentMarker), documentMarker);
    await page.unroute(sessionRoute);
    await page.getByRole('button', {name: '重新验证', exact: true}).click();
    await page.locator('#access-state').waitFor({state: 'hidden'});

    const restrictedPanels = '.assistant-memory-context, .assistant-execution-window, .assistant-legacy-section, .assistant-unknown-section';
    if (role === 'user') {
      assert.equal(await page.locator(restrictedPanels).count(), 0, 'ordinary users must not mount administrator controls');
      assert.deepEqual(requests, [], 'ordinary initialization must not call administrator APIs');
    } else {
      assert.equal(await page.locator(restrictedPanels).count(), 4);
      await page.locator('summary').filter({hasText: '记忆上下文'}).click();
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
      await page.locator('summary').filter({hasText: '记忆上下文'}).click();
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
      await page.locator('.assistant-events').getByText('已载入记忆上下文', {exact: true}).waitFor();
    }
    // Logout is available immediately, even before the shell knows the account.
    let releaseLogoutCheck, sawLogoutCheck;
    const pendingLogoutCheck = new Promise(resolve => { releaseLogoutCheck = resolve; });
    const logoutCheckSeen = new Promise(resolve => { sawLogoutCheck = resolve; });
    await page.route(sessionRoute, async route => {
      sawLogoutCheck(); await pendingLogoutCheck; await route.abort();
    }, {times: 1});
    await page.reload();
    await logoutCheckSeen;
    await page.getByText('Account assistant completed', {exact: true}).first().waitFor();
    await page.getByRole('button', {name: '退出登录', exact: true}).click();
    await page.waitForURL(url => url.pathname === '/login');
    releaseLogoutCheck();
    await page.getByRole('heading', {name: '登录工作空间', exact: true}).waitFor();
    assert.equal((await page.request.get(origin + '/api/operator/session')).status(), 401, 'logout must revoke the server cookie before identity resolution');
    assert.equal(await page.locator('.console-layout').isVisible(), false);
    assert.deepEqual(denied, []);
    assert.deepEqual(errors, []);
    console.log('PASS: ' + role + ' assistant permissions, completion, recovery, artifacts and cancellation');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
