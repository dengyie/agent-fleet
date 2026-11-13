// Real browser layout/focus checks with deterministic API fault injection.
const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const [origin, scenario] = process.argv.slice(2);
(async () => {
  const browser = await chromium.launch({channel: process.env.FLEET_BROWSER_CHANNEL || 'chrome', headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1440, height:900}});
    page.setDefaultTimeout(5000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    let runState = 'succeeded';
    const run = () => ({run_id:'review-run', state:runState, config:{model_profile_id:'actual-b'}});
    await page.route('**/api/platform/v1/defaults', r => r.fulfill({json:{ok:true,
      defaults:{model_profile_id:'default-a', workspace_id:'workspace'},
      models:[{profile_id:'default-a',model:'Model A'}, {profile_id:'actual-b',model:'Model B',enabled:false}],
      workspaces:[{workspace_id:'workspace',name:'Review workspace'}]}}));
    await page.route('**/api/platform/v1/conversations/review-model', r => r.fulfill({json:{ok:true,conversation:{
      conversation_id:'review-model',workspace_id:'workspace',messages:[{role:'user',content:'用 Model B 分析项目'},
        {role:'assistant',content:Array.from({length:50}, (_,i) => `段落 ${i+1}：工作区检查结果。`).join('\n\n')}],runs:[run()]}}}));
    await page.route('**/api/platform/v1/runs/review-run', r => r.fulfill({json:{ok:true,run:run()}}));
    await page.route('**/api/platform/v1/runs/review-run/events*', r => r.fulfill({json:{ok:true,events:[],next_cursor:0}}));
    const input = page.getByRole('textbox',{name:'给助手的任务'});
    async function openHistory() {
      await page.goto(origin + '/conversation/review-model', {waitUntil: 'domcontentloaded'});
      await page.waitForFunction(() => {
        const model = document.querySelector('#assistant-model');
        return model && model.options.length > 0 && model.value !== '';
      });
    }
    if (scenario === 'action-errors') {
      await page.goto(origin + '/assistant', {waitUntil: 'domcontentloaded'});
      await page.getByText('就绪',{exact:true}).waitFor();
      let rejectCreate = true;
      await page.route('**/api/platform/v1/conversations', r => r.request().method() !== 'POST' ? r.continue() : r.fulfill(rejectCreate
        ? {status:403,json:{ok:false,error:'workspace_access_denied',detail:'工作区权限已撤销',request_id:'review-403'}}
        : {json:{ok:true,conversation:{conversation_id:'review-model'}}}));
      await input.fill('keep my draft');
      await page.getByRole('button',{name:'发送任务'}).click();
      await page.getByText('提交失败',{exact:true}).waitFor();
      const denied = page.getByText('工作区权限已撤销 · 请求编号 review-403',{exact:true});
      assert.equal(await denied.isVisible(),true,'submission errors must be visible without opening the activity tab');
      assert.equal(await input.inputValue(),'keep my draft');
      assert.equal(await input.evaluate(n => n.readOnly),false);
      assert.equal(await page.getByRole('tab',{selected:true}).textContent(),'产物');
      rejectCreate = false;
      const turns = [];
      runState = 'running';
      await page.route('**/api/platform/v1/conversations/review-model/turns', r => {
        turns.push(r.request().postDataJSON());
        return r.fulfill(turns.length === 1
          ? {status:503,json:{ok:false,error:'unavailable',detail:'提交响应暂时不可用',request_id:'review-503'}}
          : {json:{ok:true,run:run()}});
      });
      await page.setViewportSize({width:390,height:844});
      await page.getByRole('button',{name:'发送任务'}).click();
      await page.getByText('提交结果待确认，请重试原请求',{exact:true}).waitFor();
      assert.equal(await page.getByText('提交响应暂时不可用 · 请求编号 review-503',{exact:true}).isVisible(),true);
      assert.equal(await denied.count(),0,'a new attempt replaces the old action error');
      assert.equal(await input.evaluate(n => n.readOnly),true);
      await page.getByRole('button',{name:'发送任务'}).click();
      await page.getByText('运行中',{exact:true}).waitFor();
      assert.deepEqual(turns[1], turns[0],'uncertain submission retries retain the exact payload and token');
      assert.equal(await page.locator('.assistant-action-error').isVisible(),false);
      let rejectCancel = true;
      await page.route('**/api/platform/v1/runs/review-run/cancel', r => r.fulfill(rejectCancel
        ? {status:403,json:{ok:false,error:'forbidden',detail:'无权取消此运行',request_id:'cancel-403'}}
        : {json:{ok:true,state:'cancelling'}}));
      await page.getByRole('button',{name:'取消运行',exact:true}).click();
      await page.getByText('无权取消此运行 · 请求编号 cancel-403',{exact:true}).waitFor();
      assert.equal(await page.locator('.assistant-run-status').textContent(),'运行中');
      assert.equal(await page.getByRole('button',{name:'取消运行',exact:true}).isDisabled(),false);
      rejectCancel = false;
      await page.getByRole('button',{name:'取消运行',exact:true}).click();
      await page.waitForFunction(() => document.querySelector('.assistant-action-error').hidden);
    }
    if (scenario === 'modal') {
      await page.setViewportSize({width:390,height:844});
      await openHistory();
      await page.getByRole('button',{name:'工作面板',exact:true}).click();
      await page.keyboard.press('Control+k');
      await page.keyboard.press('Meta+k');
      assert.equal(await page.locator('#mobile-menu').getAttribute('aria-expanded'),'false','global shortcuts must not open another overlay');
      assert.equal(await page.evaluate(() => document.querySelector('.assistant-inspector').contains(document.activeElement)),true);
      // Background header/navigation must not accept pointer or programmatic focus.
      const menuBox = await page.locator('#mobile-menu').boundingBox();
      await page.mouse.click(menuBox.x + menuBox.width / 2, menuBox.y + menuBox.height / 2);
      await page.locator('#mobile-menu').evaluate(n => n.focus());
      assert.equal(await page.locator('#mobile-menu').getAttribute('aria-expanded'),'false');
      assert.equal(await page.locator('#mobile-menu').evaluate(n => n === document.activeElement),false);
      await page.getByRole('button',{name:'收起工作面板',exact:true}).focus();
      await page.keyboard.press('Shift+Tab');
      assert.equal(await page.evaluate(() => document.querySelector('.assistant-inspector').contains(document.activeElement)),true);
      await page.keyboard.press('Tab');
      assert.equal(await page.evaluate(() => document.querySelector('.assistant-inspector').contains(document.activeElement)),true);
      await page.keyboard.press('Escape');
      await page.waitForFunction(() => document.querySelector('.assistant-inspector').hidden);
      assert.equal(await page.locator('.assistant-inspector').isVisible(),false);
      assert.equal(await page.locator('.inspector-toggle').evaluate(n => n === document.activeElement),true);
      await page.keyboard.press('Control+k');
      assert.equal(await page.locator('#conversation-search').evaluate(n => n === document.activeElement),true);
      await page.keyboard.press('Escape');
      await page.getByRole('button',{name:'工作面板',exact:true}).click();
      await page.setViewportSize({width:1440,height:900});
      await page.waitForFunction(() => !document.querySelector('dialog:modal'));
      const desktopPanel = await page.locator('.assistant-inspector').boundingBox();
      const desktopChat = await page.locator('.assistant-chat').boundingBox();
      assert.equal(desktopPanel.height,desktopChat.height,'desktop panel must fill the workspace row');
      await input.focus();
      assert.equal(await input.evaluate(n => n === document.activeElement),true);
      await page.getByRole('tab',{name:'运行',exact:true}).focus();
      await page.setViewportSize({width:390,height:844});
      await page.waitForFunction(() => document.querySelector('.assistant-inspector').hidden);
      assert.equal(await page.locator('.inspector-toggle').evaluate(n => n === document.activeElement),true);
      await page.getByRole('button',{name:'工作面板',exact:true}).click();
      // Dispose on the same document (auth/session loss), then mount afresh.
      // The fixture must revoke the session too: otherwise token login will
      // auto-redirect back while the test is navigating to its next page.
      await page.route('**/api/operator/session', r => r.fulfill({status:401,json:{ok:false,error:'unauthorized'}}));
      assert.equal(await page.evaluate(() => {
        window.dispatchEvent(new Event('fleet-auth-required'));
        return !document.querySelector('dialog:modal');
      }),true,'teardown must close the native modal before document navigation');
      await page.waitForURL('**/login?**', {waitUntil: 'domcontentloaded'});
      await page.waitForFunction(() => !document.querySelector('#login-view').hidden && !document.querySelector('#login-form [type=submit]').disabled);
      await page.unroute('**/api/operator/session');
      await page.goto(origin + '/assistant', {waitUntil: 'domcontentloaded'});
      await page.getByText('就绪',{exact:true}).waitFor();
      await input.focus();
      assert.equal(await input.evaluate(n => n === document.activeElement),true);
    }
    if (scenario === 'scroll') {
      await openHistory();
      for (const width of [1440,390]) {
        await page.setViewportSize({width,height:900});
        for (const text of ['one line','多行草稿\n'.repeat(25)]) {
          await input.fill(text);
          // Include the layout occupied by an available cancellation action.
          await page.locator('.composer-actions .button-secondary').evaluate(n => n.hidden = false);
          await page.locator('[data-chat-scroll]').evaluate(n => {n.scrollTop=0;n.dispatchEvent(new Event('scroll'));});
          const jump = page.getByRole('button',{name:'滚动到最新消息',exact:true});
          await jump.waitFor();
          const bounds = await page.evaluate(() => {
            const rect = s => document.querySelector(s).getBoundingClientRect().toJSON();
            return {button:rect('#back-to-bottom'),thread:rect('[data-chat-scroll]'),composer:rect('.assistant-composer')};
          });
          assert.ok(bounds.button.y >= bounds.thread.y && bounds.button.bottom <= bounds.thread.bottom,
            `scroll control must stay inside message viewport: ${JSON.stringify(bounds)}`);
          assert.ok(bounds.button.bottom <= bounds.composer.y,'scroll control must never cover the editor');
          await jump.click();
          await page.waitForFunction(() => {const n=document.querySelector('[data-chat-scroll]');return n.scrollHeight-n.scrollTop-n.clientHeight < 72;});
        }
      }
    }
    if (scenario === 'model') {
      await openHistory();
      assert.equal(await page.locator('#assistant-model').inputValue(),'default-a');
      await page.getByText('本次模型：Model B',{exact:true}).waitFor();
      assert.equal(await page.locator('.assistant-run-model').isVisible(),true,'executed model must be visible separately from the next-turn model');
      assert.equal(await page.locator('.assistant-run-model').textContent(),'本次模型：Model B');
      await page.setViewportSize({width:390,height:844});
      assert.equal(await page.locator('.assistant-run-model').isVisible(),true);
    }
    assert.deepEqual(errors,[]);
    console.log('PASS: ' + scenario);
  } finally { await browser.close(); }
})().catch(error => {console.error(error);process.exitCode=1;});
