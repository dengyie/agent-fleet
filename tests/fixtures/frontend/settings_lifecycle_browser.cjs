// Hold real committed responses to exercise settings/editor/identity lifetimes.
const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const [origin, group, scenario, argument] = process.argv.slice(2);
function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return {promise, resolve};
}
async function settle(page) {
  await page.evaluate(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done))));
}
async function selectSection(page, name) {
  await page.getByRole('navigation', {name: '设置分类', exact: true}).getByRole('link', {name, exact: true}).click();
}

async function enrollment(page, context) {
  const api = origin + '/api/platform/v1/settings';
  const catalog = await (await context.request.get(api)).json();
  const expectedRoot = catalog.workspaces.find(row => row.workspace_id === 'review-work').root_path;
  if (scenario === 'assistant-evict') {
    await page.goto(origin + '/assistant');
    if (!await page.getByRole('tab', {name: '节点', exact: true}).isVisible()) await page.getByRole('button', {name: '工作面板', exact: true}).click();
    await page.getByRole('tab', {name: '节点', exact: true}).click();
    await page.getByText('管理执行配置', {exact: true}).click();
  } else await page.goto(origin + '/settings/nodes');
  await page.getByRole('button', {name: /Review VM/}).click();
  await page.getByText('部署或更新 VM 执行配置', {exact: true}).click();
  const committed = deferred(), release = deferred(), finished = deferred();
  let postCount = 0, downloadCount = 0;
  page.on('download', () => { downloadCount++; });
  await page.route('**/settings/nodes/review-node/enrollment', async route => {
    postCount++;
    const response = await route.fetch();
    assert.equal(response.status(), 200);
    committed.resolve();
    await release.promise;
    await route.fulfill({response});
    finished.resolve();
  });
  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('button', {name: '下载接入配置', exact: true}).click();
  await committed.promise;
  if (scenario === 'category') await selectSection(page, '外观');
  else if (scenario === 'close') {
    await page.getByRole('button', {name: '关闭设置', exact: true}).click();
    await page.locator('.settings-dialog').waitFor({state: 'detached'});
  } else if (scenario === 'editor') {
    await page.getByRole('button', {name: '取消编辑', exact: true}).click();
    await page.getByRole('button', {name: /Review VM/}).click();
    await page.getByLabel('节点名称', {exact: true}).fill('new unsaved node draft');
  } else if (scenario === 'owner') {
    await page.locator('.settings-owner-scope summary').click();
    await page.getByLabel('账号 ID', {exact: true}).fill('other-owner');
    await page.getByRole('button', {name: '加载账号配置', exact: true}).click();
    await page.getByRole('button', {name: /Other VM/}).waitFor();
  } else if (scenario.endsWith('evict')) {
    if (scenario === 'assistant-evict') {
      await page.evaluate(() => window.dispatchEvent(new CustomEvent('fleet:navigate', {detail: {path: '/fleet'}})));
      await page.evaluate(() => window.dispatchEvent(new CustomEvent('fleet:navigate', {detail: {path: '/settings/appearance'}})));
    }
    for (const name of ['外观', '默认配置', '工作区与存储', '节点展示与心跳', '系统与运行参数', '模型源与 API']) await selectSection(page, name);
    const key = scenario === 'evict' ? 'settings:nodes' : 'assistant:';
    assert.equal(await page.locator(`[data-route-cache-entry="${key}"]`).count(), 0, 'initiating route must really be evicted');
  } else if (scenario === 'logout') {
    assert.equal((await context.request.post(origin + '/api/accounts/logout', {headers: {Origin: origin}, data: {}})).status(), 200);
    // Invoke the real non-cached page teardown while keeping the test document
    // inspectable. A real navigation would destroy the pending callback too.
    await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: false})));
    assert.equal(await page.locator('.route-view-entry').count(), 0);
    release.resolve();
    await finished.promise;
    await settle(page);
    assert.equal(downloadCount, 0, 'a disposed identity must never receive an enrollment file');
    assert.equal(postCount, 1);
    return;
  }
  const downloaded = page.waitForEvent('download');
  release.resolve();
  const download = await downloaded;
  await finished.promise;
  await download.saveAs(argument);
  const data = JSON.parse(await fs.readFile(argument, 'utf8'));
  assert.equal(download.suggestedFilename(), 'review-node-enrollment.json');
  assert.equal(data.node_id, 'review-node');
  assert.deepEqual(data.workspace.roots, {'review-work': expectedRoot});
  assert.equal(data.workspace.execution_mode, 'vm_direct');
  assert.equal((await page.content()).includes(data.credential), false);
  assert.equal(postCount, 1, 'retrieving a result must not rotate the credential a second time');
  assert.equal(downloadCount, 1);
  if (scenario === 'editor') {
    assert.equal(await page.getByLabel('节点名称', {exact: true}).inputValue(), 'new unsaved node draft');
    assert.equal(await page.getByText(/接入配置已下载/).count(), 0, 'do not update a replacement editor');
  }
}

async function runtime(page, context) {
  const kind = scenario, outcome = argument;
  const api = origin + '/api/platform/v1/settings/' + kind;
  await page.goto(origin + '/settings/' + kind);
  const label = kind === 'hosts' ? '节点清单（YAML）' : 'Worker 轮询间隔（秒）';
  if (kind === 'runtime') await page.getByText('运行间隔与容量', {exact: true}).click();
  const control = page.getByLabel(label, {exact: true});
  const saved = kind === 'hosts' ? 'hosts:\n  - name: saved-node\n' : '2';
  const draft = kind === 'hosts' ? 'hosts:\n  - name: unsaved-draft\n' : '3';
  await control.fill(saved);
  const committed = deferred(), release = deferred(), finished = deferred();
  let reads = 0;
  await page.route(api, async route => {
    if (route.request().method() === 'GET') { reads++; return route.continue(); }
    const response = await route.fetch();
    assert.equal(response.status(), 200);
    committed.resolve();
    await release.promise;
    if (outcome === 'failure') await route.fulfill({status: 503, json: {error: 'held_failure', detail: 'old save receipt failed'}});
    else await route.fulfill({response});
    finished.resolve();
  });
  await page.getByRole('button', {name: '保存服务器配置', exact: true}).click();
  await committed.promise;
  const reloaded = page.waitForResponse(response => response.url() === api && response.request().method() === 'GET');
  await page.getByRole('button', {name: '重新加载', exact: true}).click();
  await reloaded;
  await page.getByRole('button', {name: '保存服务器配置', exact: true}).waitFor({state: 'visible'});
  await page.waitForFunction(() => !document.querySelector('.settings-content button[type=submit]')?.disabled);
  if (kind === 'runtime') await page.getByText('运行间隔与容量', {exact: true}).click();
  await control.fill(draft);
  release.resolve();
  await finished.promise;
  await settle(page);
  assert.equal(reads, 1, 'an old receipt must not trigger a second reload');
  assert.equal(await control.inputValue(), draft, 'an old receipt must not destroy the current unsaved draft');
  assert.equal(await page.getByText(/old save receipt failed|已保存。需要重启 Hub/).count(), 0, 'stale success/error status must not be displayed');
  // Ensure the new editor remains usable and the server really receives it.
  await page.unroute(api);
  await page.getByRole('button', {name: '保存服务器配置', exact: true}).click();
  await page.getByText(/已保存。需要重启 Hub/).waitFor();
  const persisted = await (await context.request.get(api)).json();
  if (kind === 'hosts') assert.equal(persisted.content, draft);
  else assert.equal(persisted.settings.find(spec => spec.key === 'AGENT_FLEET_PLATFORM_WORKER_INTERVAL_S').saved, draft);
}

async function integrations(page) {
  let requests = 0;
  const release = deferred();
  await page.route('**/api/accounts/me', async route => {
    requests++;
    if (requests === 1) return route.fulfill({status: 503, json: {error: 'held_failure', detail: 'integration discovery failed'}});
    if (scenario === 'inflight') await release.promise;
    await route.continue();
  });
  await page.goto(origin + '/settings/integrations');
  await page.getByText('integration discovery failed', {exact: true}).waitFor();
  if (scenario === 'retry') await page.getByRole('button', {name: '重试加载集成', exact: true}).click();
  else {
    await selectSection(page, '外观');
    if (scenario === 'inflight') await Promise.all([
      page.waitForRequest(request => request.url().endsWith('/api/accounts/me')),
      selectSection(page, '集成与工作流'),
    ]);
    else await selectSection(page, '集成与工作流');
  }
  if (scenario === 'inflight') {
    await selectSection(page, '外观');
    await selectSection(page, '集成与工作流');
    await selectSection(page, '外观');
    assert.equal(requests, 2, 'suspended/resumed discovery must not duplicate the in-flight GET');
    const loaded = page.waitForResponse(response => response.url().endsWith('/api/accounts/me'));
    release.resolve();
    // Discovery may complete while its category is suspended.
    await loaded;
    await settle(page);
    await selectSection(page, '集成与工作流');
  }
  await page.getByRole('link', {name: '配置集成运行开关', exact: true}).waitFor();
  assert.equal(await page.getByText('integration discovery failed', {exact: true}).count(), 0);
  assert.equal(requests, 2);
  const panels = await page.locator('.settings-content .account-panel').count();
  assert.ok(panels > 0, 'the real account features should mount at least one integration panel');
  await selectSection(page, '外观');
  await selectSection(page, '集成与工作流');
  assert.equal(requests, 2, 'healthy cached integrations must not be discovered again');
  assert.equal(await page.locator('.settings-content .account-panel').count(), panels, 'child panels must not be duplicated');
}

(async () => {
  const browser = await chromium.launch({channel: process.env.FLEET_BROWSER_CHANNEL || 'chrome', headless: true});
  try {
    const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
    const login = await context.request.post(origin + '/api/accounts/login', {
      headers: {Origin: origin}, data: {email: 'admin@example.test', password: 'safe-password-123!'},
    });
    assert.equal(login.status(), 200);
    const page = await context.newPage(); page.setDefaultTimeout(5000);
    const errors = []; page.on('pageerror', error => errors.push(error.message));
    if (group === 'enrollment') await enrollment(page, context);
    else if (group === 'runtime') await runtime(page, context);
    else if (group === 'integrations') await integrations(page);
    else throw new Error('Unknown lifecycle test group: ' + group);
    assert.deepEqual(errors, []);
    console.log('PASS: settings lifecycle ' + group + '/' + scenario + '/' + (argument || ''));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
