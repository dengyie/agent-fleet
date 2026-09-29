const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const [origin, taskId] = process.argv.slice(2);
(async () => {
  const browser = await chromium.launch({channel: process.env.FLEET_BROWSER_CHANNEL || 'chrome', headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    const errors = [], failedAssets = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('response', response => { if (response.url().includes('/assets/') && response.status() >= 400) failedAssets.push(response.url()); });
    const output = process.env.FLEET_SCREENSHOTS || '/tmp/agent-fleet-console-evidence';
    fs.mkdirSync(output, {recursive: true});
    await page.addInitScript(() => { const Native = window.EventSource; window.EventSource = class extends Native { constructor(...args) { super(...args); window.reviewSse = this; } }; });
    await page.goto(origin);
    await page.getByText('下发任务', {exact: true}).first().waitFor();
    assert.equal(await page.locator('[data-icon] svg').first().evaluate(svg => svg.namespaceURI), 'http://www.w3.org/2000/svg');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.screenshot({path: path.join(output, 'fleet-desktop.png'), fullPage: true});
    await page.goto(origin + '/machine/studio-mac');
    await page.locator('#task-create-form').waitFor();
    assert.ok((await page.locator('#nt-confirm').boundingBox()).width <= 24, 'checkbox must retain its control width');
    assert.equal(await page.locator('#nt-error').isVisible(), false, 'an empty error must not appear as an alert');
    await page.locator('#nt-project').fill('draft project');
    await page.locator('#nt-instruction').fill('keep this unsent task');
    await page.locator('#nt-confirm').check();
    await page.locator('#nt-instruction').focus();
    await page.evaluate(() => {
      const editor = document.querySelector('#nt-instruction');
      editor.setSelectionRange(3, 7);
      editor.dispatchEvent(new CompositionEvent('compositionstart', {data: '中'}));
      window.reviewSse.dispatchEvent(new MessageEvent('machine_update', {data: JSON.stringify({machine:'studio-mac', online:true, ts:Date.now()/1000, event_seq:100000, agents:{}, system:{load:'0.43'}, changes:[]}), lastEventId:'100000'}));
    });
    assert.equal(await page.locator('#nt-project').inputValue(), 'draft project');
    assert.equal(await page.locator('#nt-instruction').inputValue(), 'keep this unsent task');
    assert.equal(await page.locator('#nt-confirm').isChecked(), true);
    assert.deepEqual(await page.locator('#nt-instruction').evaluate(n => [n === document.activeElement, n.selectionStart, n.selectionEnd]), [true, 3, 7]);
    await page.screenshot({path: path.join(output, 'node-desktop.png'), fullPage: true});
    await page.goto(origin + '/task/' + taskId);
    await page.getByText('浏览器验收任务', {exact: true}).first().waitFor();
    await page.screenshot({path: path.join(output, 'task-desktop.png'), fullPage: true});
    await page.goto(origin + '/session/browser-session');
    await page.getByText('browser-session', {exact: false}).first().waitFor();
    await page.locator('.session-full-text summary').first().click();
    assert.ok((await page.locator('.session-full-text pre').first().textContent()).endsWith('_END'));
    await page.getByRole('button', {name: '下一页', exact: true}).click();
    await page.getByText('History row 101', {exact: true}).waitFor();
    await page.getByRole('button', {name: '下一页', exact: true}).click();
    await page.getByText('History row 205', {exact: true}).waitFor();
    assert.equal(await page.getByRole('button', {name: '下一页', exact: true}).isDisabled(), true);
    await page.getByRole('button', {name: '上一页', exact: true}).click();
    await page.getByText('History row 101', {exact: true}).waitFor();
    await page.screenshot({path: path.join(output, 'session-desktop.png'), fullPage: true});
    await page.goto(origin + '/monitoring');
    await page.getByText('studio-mac-api', {exact: true}).first().waitFor();
    await page.screenshot({path: path.join(output, 'monitoring-desktop.png'), fullPage: true});
    await page.route('**/api/platform/v1/nodes', route => route.fulfill({status:503, contentType:'application/json', body:JSON.stringify({ok:false,detail:'nodes unavailable'})}));
    await page.goto(origin + '/assistant');
    await page.getByText('就绪', {exact: true}).waitFor();
    await page.screenshot({path: path.join(output, 'assistant-desktop.png'), fullPage: true});
    const input = page.getByRole('textbox', {name: '给助手的任务'});
    await input.fill('检查工作区');
    await input.dispatchEvent('keydown', {key: 'Enter', isComposing: true});
    assert.equal(await input.inputValue(), '检查工作区');
    assert.ok(page.url().endsWith('/assistant'));
    let failedRecovery = 0;
    await page.route('**/api/platform/v1/conversations/*', route => {
      if (route.request().method() === 'GET' && /\/conversations\/[^/?]+$/.test(route.request().url()) && failedRecovery < 2) {
        failedRecovery++;
        return route.fulfill({status:503, contentType:'application/json', body:JSON.stringify({ok:false,error:'unavailable',detail:'temporary recovery failure'})});
      }
      return route.continue();
    });
    await input.press('Enter');
    await page.getByText('浏览器验收结果', {exact: true}).waitFor();
    assert.equal(failedRecovery, 2);
    await page.waitForFunction(() => !document.querySelector('.assistant-composer textarea').readOnly);
    assert.equal(await page.getByRole('button', {name:'发送任务'}).isDisabled(), false);
    assert.ok(page.url().includes('/conversation/'));
    assert.equal(await page.locator('stream-markdown img').count(), 0);
    assert.equal(await page.locator('.assistant-run-status').innerText(), '已完成');
    await page.locator('#sidebar-conversations a').first().waitFor();
    assert.equal(await page.getByRole('combobox', {name:'工作区', exact:true}).isDisabled(), true);
    await page.reload();
    await page.locator('#sidebar-conversations a').first().waitFor();
    assert.equal(await page.getByRole('combobox', {name:'工作区', exact:true}).inputValue(), 'workspace');
    await page.getByText('浏览器验收结果', {exact: true}).waitFor();
    assert.equal(await page.locator('.assistant-message.assistant').count(), 1);
    await page.screenshot({path: path.join(output, 'assistant-complete.png'), fullPage: true});
    const unicode = await page.evaluate(async () => {
      const md = document.createElement('stream-markdown'); document.body.append(md);
      const results = [];
      for (const content of ['😀'.repeat(20000) + 'END', '中😀'.repeat(16383) + 'OK']) {
        md.setAttribute('content', content);
        results.push(md.textContent.trim() === content && !md.querySelector('[role=status]'));
      }
      md.setAttribute('content', '😀'.repeat(32768) + 'EXCESS');
      results.push(!md.textContent.includes('EXCESS') && !!md.querySelector('[role=status]'));
      md.remove(); return results;
    });
    assert.deepEqual(unicode, [true, true, true]);
    const nodes = await page.evaluate(async () => {
      const {FleetStore} = await import('/assets/state/store.js');
      const {mountNavigation} = await import('/assets/shell/navigation.js');
      const store = new FleetStore(); const stop = mountNavigation({page:'fleet'}, store);
      store.setStatus({machines:[{machine:'review-node', online:true}]});
      const results = [document.querySelector('#sidebar-nodes .node-dot').classList.contains('online')];
      store.applySseEvent({type:'machine_update',data:{machine:'review-node', online:false, ts:Date.now()/1000,event_seq:999}});
      results.push(!document.querySelector('#sidebar-nodes .node-dot').classList.contains('online'));
      store.applySseEvent({type:'machine_update',data:{machine:'new-node', online:true, ts:Date.now()/1000,event_seq:1000}});
      results.push(document.querySelectorAll('#sidebar-nodes .node-dot').length === 2);
      stop(); return results;
    });
    assert.deepEqual(nodes, [true, true, true]);
    // Malicious model/tool fields exercise the real copied components, not an escape mock.
    await page.evaluate(() => {
      const card = document.createElement('tool-call-badge');
      card.id = 'xss-card'; card.setAttribute('name', '<img src=x onerror=alert(1)>');
      card.setAttribute('args', '<script>alert(2)</script>'); card.setAttribute('status', 'unknown');
      document.body.appendChild(card);
      const md = document.createElement('stream-markdown'); md.id = 'xss-markdown';
      md.setAttribute('content', '[x](javascript:alert(1))\n<svg onload=alert(1)>\n<script>alert(2)</script>');
      document.body.appendChild(md);
    });
    assert.equal(await page.locator('#xss-card img, #xss-card script, #xss-markdown script, #xss-markdown svg, #xss-markdown [href^="javascript:"]').count(), 0);
    await page.locator('#xss-card button').click();
    assert.equal(await page.locator('#xss-card button').getAttribute('aria-expanded'), 'true');
    await page.locator('#xss-card').evaluate(node => node.remove());
    await page.locator('#xss-markdown').evaluate(node => node.remove());
    await page.getByRole('button', {name: '深色主题', exact: true}).click();
    assert.equal(await page.locator('html').getAttribute('data-resolved-theme'), 'dark');
    await page.reload();
    await page.getByText('浏览器验收结果', {exact: true}).waitFor();
    assert.equal(await page.locator('html').getAttribute('data-resolved-theme'), 'dark');
    await page.screenshot({path: path.join(output, 'assistant-dark.png'), fullPage: true});
    await page.getByRole('button', {name: '浅色主题', exact: true}).click();
    await page.setViewportSize({width: 390, height: 844});
    await page.getByRole('button', {name: '打开导航'}).waitFor();
    await page.waitForFunction(() => document.getElementById('sidebar').inert, null, {timeout: 3000});
    assert.equal(await page.locator('#sidebar').evaluate(node => node.inert), true, 'closed mobile navigation must not receive keyboard focus');
    await page.getByRole('button', {name: '打开导航'}).click();
    assert.equal(await page.locator('#mobile-menu').getAttribute('aria-expanded'), 'true');
    await page.keyboard.press('Shift+Tab');
    assert.equal(await page.evaluate(() => document.activeElement === document.querySelector('#sidebar [data-theme=system]')), true);
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(() => document.activeElement === document.querySelector('#sidebar a')), true);
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#mobile-menu').getAttribute('aria-expanded'), 'false');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.locator('auto-scroll-anchor').evaluate(node => {
      node.scrollIntoView({block: 'center', behavior: 'instant'});
      window.dispatchEvent(new Event('scroll'));
    });
    assert.equal(await page.getByRole('button', {name: '滚动到最新消息'}).isVisible(), false, 'latest-message anchor is already visible');
    await page.screenshot({path: path.join(output, 'assistant-mobile.png'), fullPage: true});
    for (const [route, ready] of [['/', '.healthbar'], ['/machine/studio-mac', '#task-create-form'], ['/task/' + taskId, '#route-view .panel'], ['/session/browser-session', '#route-view .panel'], ['/monitoring', '.monitoring-view']]) {
      await page.goto(origin + route);
      await page.locator(ready).first().waitFor();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, 'mobile overflow: ' + route);
      if (route.startsWith('/machine/')) assert.ok(await page.locator('.timeline i').count() > 100, 'dense production history fixture');
    }
    await page.route('**/api/platform/v1/services?*', route => route.fulfill({status: 503, contentType: 'application/json', body: JSON.stringify({ok:false,error:'unavailable',detail:'暂时不可用'})}));
    await page.goto(origin + '/monitoring');
    await page.getByText(/加载失败|暂时不可用/).first().waitFor();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.goto(origin);
    await page.route('**/api/tasks?*', route => {
      if (route.request().headers()['x-access-token'] !== 'browser-fixture-token') {
        return route.fulfill({status:401, contentType:'application/json', body:JSON.stringify({ok:false,error:'unauthorized',detail:'operator required'})});
      }
      return route.continue();
    });
    await page.getByRole('button', {name:'操作员登录', exact:true}).click();
    await page.getByLabel('操作员令牌', {exact:true}).fill('incorrect-fixture');
    await page.getByRole('button', {name:'验证并登录'}).click();
    await page.getByText('令牌无效或无操作权限', {exact:true}).waitFor();
    assert.equal(await page.evaluate(() => sessionStorage.getItem('fleet_operator_token')), null);
    await page.getByLabel('操作员令牌', {exact:true}).fill('browser-fixture-token');
    await Promise.all([page.waitForEvent('load'), page.getByRole('button', {name:'验证并登录'}).click()]);
    assert.equal(await page.evaluate(() => sessionStorage.getItem('fleet_operator_token')), 'browser-fixture-token');
    const leaked = await page.evaluate(async () => {
      const api = await import('/assets/api/client.js');
      const originalFetch = window.fetch, config = window.FleetConfig;
      let headers;
      window.FleetConfig = {apiBaseUrl:'https://foreign.invalid/api'};
      window.fetch = async (_, options) => { headers = options.headers; throw new Error('test transport'); };
      try { await api.getStatus(); } catch (_) {} finally { window.fetch = originalFetch; window.FleetConfig = config; }
      return !!headers['X-Access-Token'];
    });
    assert.equal(leaked, false, 'operator token must not leave this origin');
    assert.deepEqual(errors, []);
    assert.deepEqual(failedAssets, []);
    console.log('PASS: six real API views, durable assistant, IME, XSS, themes, mobile navigation, error states, module closure');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
