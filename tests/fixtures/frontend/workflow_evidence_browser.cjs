// Real scoped downloads and human acceptance; reports remain untrusted bytes.
const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const [origin, verdict] = process.argv.slice(2);

(async () => {
  const browser = await chromium.launch({channel: process.env.FLEET_BROWSER_CHANNEL || 'chrome', headless: true});
  try {
    const context = await browser.newContext({viewport: {width: 390, height: 844}, acceptDownloads: true});
    const page = await context.newPage(); page.setDefaultTimeout(8000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(origin + '/assistant', {waitUntil: 'domcontentloaded'});
    await page.getByText('就绪', {exact: true}).waitFor();
    await page.getByRole('button', {name: '工作面板', exact: true}).click();
    await page.getByRole('tab', {name: '流程', exact: true}).click();
    const panel = page.getByRole('region', {name: '工作流', exact: true});
    await panel.locator('[data-workflow-id="workflow-a"]').click();
    await panel.locator('.assistant-workflow-meta').getByText('校验：' + (verdict === 'passed' ? '通过' : '失败'), {exact: true}).waitFor();
    const detailResponse = await context.request.get(origin + '/api/platform/v1/workflows/workflow-a');
    assert.equal(detailResponse.status(), 200);
    const detail = (await detailResponse.json()).workflow;
    const developed = detail.steps.find(step => step.step_key === 'develop');
    const reviewed = detail.steps.find(step => step.step_key === 'review');
    const bundle = developed.outputs.find(output => output.output_key === 'source_bundle');
    const report = reviewed.outputs.find(output => output.output_key === 'review_report');
    assert.equal(reviewed.subject_digest, developed.subject_digest);
    const manifestResponse = await context.request.get(origin + '/api/platform/v1/workspaces/' + detail.workspace_id + '/artifacts');
    assert.equal(manifestResponse.status(), 200);
    const manifest = (await manifestResponse.json()).artifacts.find(item => item.artifact_id === bundle.artifact_id);
    assert.equal(manifest.name, '<img src=x onerror=window.workflowXss=1>.tar');
    for (const [step, output] of [['develop', bundle], ['review', report]]) {
      const link = panel.locator('[data-step="' + step + '"]').getByRole('link', {name: output.output_key, exact: true});
      assert.equal(new URL(await link.getAttribute('href'), origin).origin, origin);
      const downloaded = page.waitForEvent('download');
      await link.press('Enter');
      const download = await downloaded;
      assert.equal(await download.failure(), null);
      const raw = fs.readFileSync(await download.path());
      assert.equal(raw.length, output.size);
      assert.equal(crypto.createHash('sha256').update(raw).digest('hex'), output.sha256);
      if (step === 'review') assert.match(JSON.parse(raw).findings[0].message, /<img src=x onerror=/);
      const foreign = await context.request.get(new URL(await link.getAttribute('href'), origin).href,
        {headers: {'Cf-Access-Authenticated-User-Email': 'other@example.test'}});
      assert.equal(foreign.status(), 404);
    }
    assert.equal(await panel.locator('img,script,iframe').count(), 0);
    assert.equal(await page.evaluate(() => window.workflowXss), undefined);
    assert.equal(context.pages().length, 1);
    if (verdict === 'failed') {
      assert.equal(detail.execution_state, 'waiting_approval');
      assert.equal(detail.reason_code, 'blocked_by_validation');
      assert.equal(detail.steps.find(step => step.step_key === 'test').execution_state, 'pending');
      assert.equal(await panel.getByRole('button', {name: '接受交付', exact: true}).count(), 0);
      assert.equal(await panel.getByRole('button', {name: '使此步及后继失效', exact: true}).count(), 2);
      await panel.getByText('等待原因：blocked_by_validation', {exact: true}).waitFor();
    } else {
      assert.equal(detail.delivery_state, 'awaiting_acceptance');
      assert.equal(detail.steps.find(step => step.step_key === 'test').subject_digest, developed.subject_digest);
      await panel.getByRole('button', {name: '接受交付', exact: true}).press('Enter');
      await panel.locator('.assistant-workflow-state').getByText('已交付', {exact: true}).waitFor();
      const accepted = (await (await context.request.get(origin + '/api/platform/v1/workflows/workflow-a')).json()).workflow;
      assert.equal(accepted.acceptance_state, 'accepted');
      assert.equal(accepted.delivery_digest, detail.delivery_digest);
    }
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false);
    const screenshotRoot = process.env.FLEET_SCREENSHOTS;
    if (screenshotRoot) {
      fs.mkdirSync(screenshotRoot, {recursive: true});
      await page.screenshot({path: path.join(screenshotRoot, 'workflow-evidence-' + verdict + '.png'), fullPage: true});
    }
    assert.deepEqual(errors, []);
    await context.close();
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
