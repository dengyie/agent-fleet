// Actual workflow HTTP/SQLite states, with a temporary read failure at the browser boundary.
const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const [origin, scenario] = process.argv.slice(2);

const labels = {waiting_node: '等待节点', waiting_external: '等待执行回执', unknown: '需要核对',
  cancelling: '正在停止', awaiting_acceptance: '等待人工验收'};

(async () => {
  const browser = await chromium.launch({channel: process.env.FLEET_BROWSER_CHANNEL || 'chrome', headless: true});
  try {
    const context = await browser.newContext({viewport: {width: 390, height: 844}});
    const page = await context.newPage(); page.setDefaultTimeout(8000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    let failRead = scenario === 'empty';
    await page.route('**/api/platform/v1/workflows?*', async route => {
      if (!failRead) return route.continue();
      await route.fulfill({status: 503, json: {ok: false, error: 'workflow_store', detail: '临时读取故障'}});
    });
    await page.goto(origin + '/assistant', {waitUntil: 'domcontentloaded'});
    await page.getByText('就绪', {exact: true}).waitFor();
    await page.getByRole('button', {name: '工作面板', exact: true}).click();
    await page.getByRole('tab', {name: '流程', exact: true}).click();
    const panel = page.getByRole('region', {name: '工作流', exact: true});
    if (scenario === 'empty') {
      await panel.getByText('临时读取故障', {exact: true}).waitFor();
      assert.equal(await panel.getByText('已交付', {exact: true}).count(), 0);
      failRead = false;
      await panel.getByRole('button', {name: '刷新流程', exact: true}).press('Enter');
      await panel.getByText('暂无工作流', {exact: true}).waitFor();
      assert.equal(await panel.getByText('临时读取故障', {exact: true}).count(), 0,
        'a recovered read must clear its stale error without clearing pending write receipts');
      assert.equal(await panel.getByRole('button', {name: '创建流程', exact: true}).isEnabled(), true);
    } else {
      const response = await context.request.get(origin + '/api/platform/v1/workflows?limit=100');
      assert.equal(response.status(), 200);
      const expected = scenario === 'awaiting_acceptance' ? 'succeeded' : scenario;
      const item = (await response.json()).workflows.find(workflow => workflow.execution_state === expected);
      assert.ok(item, 'the expected state must exist in the actual repository');
      await panel.locator('[data-workflow-id="' + item.workflow_id + '"]').click();
      await panel.locator('.assistant-workflow-state').getByText(labels[scenario], {exact: true}).waitFor();
      assert.equal(await panel.getByText('已交付', {exact: true}).count(), 0);
      if (scenario === 'unknown') {
        assert.equal(await panel.getByRole('button', {name: '核对执行', exact: true}).count(), 1);
        assert.equal(await panel.getByRole('button', {name: '接受交付', exact: true}).count(), 0);
      }
      if (scenario === 'cancelling') {
        await panel.getByText('正在停止，等待实际回执', {exact: true}).waitFor();
        assert.equal(await panel.getByText('已取消', {exact: true}).count(), 0);
      }
      if (scenario === 'awaiting_acceptance') {
        await panel.getByText('校验：未要求', {exact: true}).waitFor();
        const requests = [];
        await page.route('**/api/platform/v1/workflows/' + item.workflow_id + '/acceptance', async route => {
          const body = route.request().postDataJSON(); requests.push(body);
          const response = await route.fetch({postData: JSON.stringify(requests.length === 1
            ? {...body, expected_revision: body.expected_revision + 1} : body)});
          await route.fulfill({response});
        });
        await panel.getByRole('button', {name: '接受交付', exact: true}).press('Enter');
        await panel.getByText('版本已更新，请查看当前结果后重新确认操作。', {exact: true}).waitFor();
        assert.equal(requests.length, 1, 'acceptance conflicts must never be retried automatically');
        assert.equal(await panel.getByRole('button', {name: '重试上次操作', exact: true}).count(), 0);
        await panel.getByRole('button', {name: '接受交付', exact: true}).press('Enter');
        await panel.locator('.assistant-workflow-state').getByText('已交付', {exact: true}).waitFor();
        assert.equal(requests.length, 2);
        assert.equal(requests[0].delivery_digest, requests[1].delivery_digest);
        assert.notEqual(requests[0].client_token, requests[1].client_token);
        await page.reload({waitUntil: 'domcontentloaded'});
        await page.getByText('就绪', {exact: true}).waitFor();
        await page.getByRole('button', {name: '工作面板', exact: true}).click();
        await page.getByRole('tab', {name: '流程', exact: true}).click();
        await panel.locator('.assistant-workflow-state').getByText('已交付', {exact: true}).waitFor();
        await panel.getByText('验收：已接受', {exact: true}).waitFor();
      }
    }
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false);
    await page.getByRole('tab', {name: '流程', exact: true}).focus();
    for (let i = 0; i < 8; i++) {
      await page.keyboard.press('Tab');
      assert.equal(await page.evaluate(() => document.querySelector('#assistant-inspector').contains(document.activeElement)), true);
    }
    const screenshotRoot = process.env.FLEET_SCREENSHOTS;
    assert.equal(await page.getByRole('tab', {selected: true}).textContent(), '流程');
    if (screenshotRoot) {
      fs.mkdirSync(screenshotRoot, {recursive: true});
      await page.screenshot({path: path.join(screenshotRoot, 'workflow-' + scenario + '.png'), fullPage: true});
    }
    await page.keyboard.press('Escape');
    assert.equal(await page.getByRole('button', {name: '工作面板', exact: true}).evaluate(node => node === document.activeElement), true);
    assert.deepEqual(errors, []);
    await context.close();
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
