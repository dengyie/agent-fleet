// Browser fault injection surrounds actual Flask/SQLite workflow operations.
const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const [origin, gate] = process.argv.slice(2);
(async () => {
  const browser = await chromium.launch({channel: process.env.FLEET_BROWSER_CHANNEL || 'chrome', headless: true});
  try {
    const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
    const page = await context.newPage(); page.setDefaultTimeout(8000);
    const errors = [], requests = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => { if (/\/workflows(?:[/?]|$)|\/workflow-templates/.test(request.url())) requests.push(request.url()); });
    await page.goto(origin + '/assistant', {waitUntil: 'domcontentloaded'});
    await page.getByText('就绪', {exact: true}).waitFor();
    assert.equal(await page.getByRole('tab', {selected: true}).textContent(), '产物');
    if (gate === 'off') {
      assert.equal(await page.getByRole('tab', {name: '流程', exact: true}).count(), 0);
      assert.equal(requests.length, 0, 'default-off UI must not poll workflow APIs');
    } else {
      const tab = page.getByRole('tab', {name: '流程', exact: true}); await tab.waitFor(); await tab.click();
      const panel = page.getByRole('region', {name: '工作流', exact: true});
      await panel.locator('[data-workflow-id="workflow-001"]').waitFor();
      await panel.getByText('版本 1', {exact: true}).waitFor();
      assert.match(await panel.innerText(), /执行：|校验：|验收：/);
      await panel.getByText('<img src=x onerror="window.workflowXss=1">', {exact: true}).first().waitFor();
      assert.equal(await panel.locator('img').count(), 0);
      assert.equal(await page.evaluate(() => window.workflowXss), undefined);
      await panel.locator('[data-workflow-id="workflow-000"]').click();
      await panel.getByText('版本 1', {exact: true}).waitFor();
      await page.reload({waitUntil: 'domcontentloaded'}); await page.getByRole('tab', {name: '流程', exact: true}).click();
      await panel.locator('[data-workflow-id="workflow-000"][aria-pressed="true"]').waitFor();
      let releaseDelayed, markFetched;
      const released = new Promise(resolve => { releaseDelayed = resolve; });
      const fetched = new Promise(resolve => { markFetched = resolve; });
      const slowDetail = async route => {
        const response = await route.fetch(); markFetched();
        await released; await route.fulfill({response});
      };
      await page.route('**/api/platform/v1/workflows/workflow-001', slowDetail);
      await panel.locator('[data-workflow-id="workflow-001"]').click();
      await fetched;
      await panel.locator('[data-workflow-id="workflow-000"]').click();
      await panel.getByText('版本 1', {exact: true}).waitFor();
      releaseDelayed();
      await page.unrouteAll({behavior: 'wait'});
      assert.equal(await panel.getByText('<img src=x onerror="window.workflowXss=1">', {exact: true}).count(), 0,
        'a delayed response for the previous selection must not replace current details');
      const pause = panel.getByRole('button', {name: '暂停流程', exact: true});
      for (const control of [panel.locator('[data-workflow-id="workflow-000"]'), pause]) {
        await control.focus();
        const refreshed = page.waitForResponse(response => response.url().includes('/workflows/workflow-000/events?'));
        await panel.getByRole('button', {name: '刷新流程', exact: true}).evaluate(node => node.click());
        await (await refreshed).finished();
        await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
        assert.equal(await control.evaluate(node => node === document.activeElement), true,
          'refreshing an unchanged workflow must preserve the focused keyboard action');
      }
      let conflict = true;
      await page.route('**/api/platform/v1/workflows/workflow-000/pause', async route => {
        if (!conflict) return route.continue();
        conflict = false;
        const body = route.request().postDataJSON(); body.expected_revision += 100;
        const response = await route.fetch({postData: JSON.stringify(body)});
        return route.fulfill({response});
      });
      await pause.press('Enter');
      await panel.getByText('版本已更新，请查看当前结果后重新确认操作。', {exact: true}).waitFor();
      assert.equal(await panel.getByRole('button', {name: '重试上次操作'}).count(), 0);
      await panel.getByRole('button', {name: '暂停流程', exact: true}).click();
      await panel.getByRole('button', {name: '继续流程', exact: true}).waitFor();
      const payloads = [];
      await page.route('**/api/platform/v1/workflows', async route => {
        if (route.request().method() !== 'POST') return route.continue();
        payloads.push(route.request().postDataJSON());
        const response = await route.fetch();
        if (payloads.length === 1) return route.abort('failed');
        return route.fulfill({response});
      });
      await panel.getByRole('button', {name: '创建流程', exact: true}).click();
      await panel.getByText('结果待确认，请重试上次操作。', {exact: true}).waitFor();
      await page.route('**/api/platform/v1/workflows?*', route => route.fulfill({status: 503,
        json: {ok: false, error: 'workflow_store', detail: '临时列表故障'}}), {times: 1});
      await panel.getByRole('button', {name: '刷新流程', exact: true}).click();
      await panel.getByText('临时列表故障', {exact: true}).waitFor();
      await panel.getByText('结果待确认，请重试上次操作。', {exact: true}).waitFor();
      const recovered = page.waitForResponse(response => response.url().includes('/workflows/workflow-000/events?'));
      await panel.getByRole('button', {name: '刷新流程', exact: true}).click();
      await (await recovered).finished();
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      assert.equal(await panel.getByText('临时列表故障', {exact: true}).count(), 0);
      await panel.getByText('结果待确认，请重试上次操作。', {exact: true}).waitFor();
      await panel.getByRole('button', {name: '重试上次操作', exact: true}).click();
      await panel.getByText('操作已记录，等待执行回执。', {exact: true}).waitFor();
      assert.deepEqual(payloads[1], payloads[0], 'an uncertain result must retain token and exact payload');
      assert.equal(payloads[0].input_refs.length, 1, 'creation must preserve the registered input slot');
      const response = await context.request.get(origin + '/api/platform/v1/workflows?limit=100');
      assert.equal((await response.json()).workflows.length, 3, 'one durable create receipt, not two workflows');
      await page.setViewportSize({width: 390, height: 844});
      await page.getByRole('button', {name: '工作面板', exact: true}).click();
      await page.getByRole('tab', {name: '流程', exact: true}).click();
      await page.getByRole('tab', {name: '流程', exact: true}).press('Home');
      assert.equal(await page.getByRole('tab', {selected: true}).textContent(), '产物');
      await page.getByRole('tab', {selected: true}).press('End');
      assert.equal(await page.getByRole('tab', {selected: true}).textContent(), '流程');
      await panel.getByRole('button', {name: '取消流程', exact: true}).click();
      await panel.getByText('已取消', {exact: true}).first().waitFor();
      assert.equal(await panel.getByText('正在停止，等待实际回执', {exact: true}).count(), 0,
        'confirmed cancellation must stop showing an unconfirmed stop');
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false);
      const screenshotRoot = process.env.FLEET_SCREENSHOTS;
      if (screenshotRoot) { fs.mkdirSync(screenshotRoot, {recursive: true}); await page.screenshot({path: path.join(screenshotRoot, 'workflow-panel.png'), fullPage: true}); }
      await page.keyboard.press('Escape');
      assert.equal(await page.getByRole('button', {name: '工作面板', exact: true}).getAttribute('aria-expanded'), 'false');
      assert.equal(await page.getByRole('button', {name: '工作面板', exact: true}).evaluate(node => node === document.activeElement), true);
    }
    assert.deepEqual(errors, []);
    await context.close();
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
