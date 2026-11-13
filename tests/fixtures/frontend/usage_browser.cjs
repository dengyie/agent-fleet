const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const origin = process.argv[2];
(async () => {
  const browser = await chromium.launch({channel: process.env.FLEET_BROWSER_CHANNEL || 'chrome', headless: true});
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const output = process.env.FLEET_USAGE_SCREENSHOTS || '/tmp/agent-fleet-usage-evidence';
    fs.mkdirSync(output, {recursive: true});
    for (const width of [1440, 390]) {
      await page.setViewportSize({width, height: 1000});
      await page.goto(origin + '/usage');
      await page.getByLabel('开始日期（UTC）').fill('2026-10-01');
      await page.getByLabel('结束日期（含当日）').fill('2026-10-02');
      await page.getByLabel('项目筛选').fill('project-a');
      await page.getByLabel('机器筛选').fill('machine-a');
      await page.getByLabel('Agent筛选').fill('codex');
      await page.getByRole('button', {name:'查询用量'}).click();
      await page.getByText('已加载 1 组用量；时间采用 UTC。', {exact:true}).waitFor();
      assert.ok((await page.locator('.usage-table').innerText()).includes('未知'));
      assert.ok(!(await page.locator('.usage-table').innerText()).includes('$0'));
      await page.getByLabel('分组方式').selectOption('model');
      await page.getByRole('button', {name:'查询用量'}).click();
      await page.getByText('已加载 3 组用量；时间采用 UTC。', {exact:true}).waitFor();
      await page.getByText('$0.123456789012', {exact:true}).waitFor();
      await page.getByText('供应商报告', {exact:true}).waitFor();
      await page.getByText('配置估算', {exact:true}).waitFor();
      await page.getByText('$0.000012', {exact:true}).waitFor();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      await page.screenshot({path:path.join(output, `usage-${width}.png`), fullPage:true});
      await page.getByLabel('模型筛选').fill('unknown-model');
      await page.getByRole('button', {name:'查询用量'}).click();
      await page.getByText('已加载 1 组用量；时间采用 UTC。', {exact:true}).waitFor();
      assert.ok(!(await page.locator('.usage-table').innerText()).includes('$'));
      await page.getByLabel('模型筛选').fill('absent-model');
      await page.getByRole('button', {name:'查询用量'}).click();
      await page.getByText('此范围内没有已结算的用量记录。', {exact:true}).waitFor();
    }
    await page.route('**/api/platform/v1/usage?**', route => route.fulfill({status:503, contentType:'application/json', body:JSON.stringify({ok:false,error:'usage_unavailable',detail:'unavailable'})}));
    await page.getByRole('button', {name:'查询用量'}).click();
    await page.getByText('用量看板暂时不可用。', {exact:true}).waitFor();
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
