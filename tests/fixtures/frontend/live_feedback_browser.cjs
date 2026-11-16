const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const fs = require('node:fs');
const [origin, conversation, runId, outcome = 'succeeded'] = process.argv.slice(2);
(async () => {
  const browser = await chromium.launch({channel:process.env.FLEET_BROWSER_CHANNEL || 'chrome',headless:true});
  const page = await browser.newPage({viewport:{width:1440,height:1000}});
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  try {
    await page.goto(origin + '/conversation/' + conversation,{waitUntil:'domcontentloaded'});
    await page.locator('.assistant-message.user').waitFor();
    await page.evaluate(async () => {
      await customElements.whenDefined('auto-scroll-anchor');
      const root = document.createElement('div'); root.setAttribute('data-chat-scroll','');
      root.style.cssText = 'height:150px;overflow:auto;position:fixed;top:0;left:0;width:300px';
      const filler = document.createElement('div'); filler.style.height = '30px';
      const anchor = document.createElement('auto-scroll-anchor'); root.append(filler,anchor); document.body.append(root);
      const frames = async () => {for (let i=0;i<3;i++) await new Promise(requestAnimationFrame);};
      try {
        await frames();
        filler.style.height = '5000px'; anchor.setAttribute('is-streaming','');
        await frames();
        if (root.scrollHeight-root.scrollTop-root.clientHeight >= 72 || !anchor.isAtBottom) {
          throw new Error('automatic streaming scroll must reach the new text without losing follow mode');
        }
        root.scrollTop = 0; root.dispatchEvent(new Event('scroll'));
        filler.style.height = '5500px'; anchor.setAttribute('is-streaming','');
        await frames();
        if (root.scrollTop !== 0 || anchor.isAtBottom) throw new Error('streaming must respect manual scroll away');
      } finally {root.remove();}
    });
    await page.request.post(origin + '/_fixture/start');
    await page.locator('.assistant-message-progress').filter({hasText:'模型正在思考'}).waitFor();
    assert.equal(await page.locator('.assistant-message-activity stream-markdown').count(),0);
    assert.ok(!(await page.locator('body').textContent()).includes('raw-reasoning-browser-sentinel'));
    await page.reload({waitUntil:'domcontentloaded'});
    await page.locator('.assistant-message-progress').filter({hasText:'模型正在思考'}).waitFor();
    await page.request.post(origin + '/_fixture/release/thinking');
    const narration = page.locator('.assistant-message-activity stream-markdown');
    await narration.filter({hasText:'先检查工作区'}).waitFor();
    assert.equal(await page.locator('.assistant-message.assistant').getAttribute('aria-busy'),'true');
    await page.waitForFunction(() => {const thread=document.querySelector('[data-chat-scroll]');return thread && thread.scrollHeight-thread.scrollTop-thread.clientHeight < 72;});
    await page.reload({waitUntil:'domcontentloaded'});
    await narration.filter({hasText:'先检查工作区'}).waitFor();
    await page.evaluate(async runId => {
      const {streamRun} = await import('/assets/api/platform.js');
      window.liveSnapshots = []; window.liveStreamError = null;
      window.liveStream = streamRun(runId,{onRun:run => window.liveSnapshots.push(run)})
        .catch(error => {window.liveStreamError = error.message;});
    },runId);
    await page.request.post(origin + '/_fixture/release/tool');
    await page.locator('.assistant-message-activity tool-call-badge').first().waitFor();
    await page.waitForFunction(() => document.querySelector('.assistant-message-activity tool-call-badge')?.getAttribute('status') === 'success');
    assert.match(await page.locator('tool-call-badge').first().getAttribute('label'),/列出目录/);
    assert.ok(await page.locator('tool-call-badge').first().getAttribute('output'));
    await page.locator('.assistant-message-progress').filter({hasText:'正在自动重试 1/3'}).waitFor();
    await page.waitForFunction(() => window.liveSnapshots.some(run => run.progress?.retry?.retry_index === 1));
    await page.evaluate(async () => {
      const {renderProgress} = await import('/assets/views/assistant/progress.js');
      const host = document.querySelector('.assistant-message.assistant .assistant-message-activity');
      const button = host.querySelector('tool-call-badge button');
      button.click(); button.focus();
      renderProgress(host,window.liveSnapshots.at(-1));
      if (host.querySelector('tool-call-badge button') !== button || document.activeElement !== button) {
        throw new Error('progress snapshots must preserve tool drawer and focus');
      }
    });
    await page.setViewportSize({width:390,height:844});
    await page.waitForFunction(() => document.getElementById('sidebar').getBoundingClientRect().right <= 1);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
    if (process.env.FLEET_SCREENSHOTS) {
      fs.mkdirSync(process.env.FLEET_SCREENSHOTS,{recursive:true});
      await page.screenshot({path:process.env.FLEET_SCREENSHOTS + '/live-feedback-' + outcome + '-retry-mobile.png'});
    }
    await page.request.post(origin + '/_fixture/release/retry');
    await narration.filter({hasText:'正在整理结果'}).waitFor();
    await page.locator('.assistant-message-progress').filter({hasText:'正在生成回复'}).waitFor();
    assert.equal(await page.locator('.assistant-message-progress').filter({hasText:'正在自动重试'}).count(),0);
    assert.equal(await page.locator('.assistant-message.assistant').getAttribute('aria-busy'),'true');
    await page.request.post(origin + '/_fixture/release/delta');
    await narration.filter({hasText:'正在整理结果，检查'}).waitFor();
    assert.equal(await page.locator('.assistant-message.assistant').getAttribute('aria-busy'),'true');
    await page.request.post(origin + '/_fixture/release/final');
    await page.waitForFunction(() => document.querySelector('.assistant-message.assistant')?.getAttribute('aria-busy') === 'false');
    const terminalState = outcome === 'stream_error' ? 'unknown' : 'succeeded';
    await page.waitForFunction(state => window.liveSnapshots.some(run => run.state === state),terminalState);
    assert.equal(await page.evaluate(() => window.liveStreamError),null,'SSE must work independently of polling');
    assert.equal(await page.evaluate(() => JSON.stringify(window.liveSnapshots).includes('raw-reasoning-browser-sentinel')),false);
    assert.equal(await page.locator('.assistant-message-progress').filter({hasText:'正在生成回复'}).count(),0);
    const expectedText = outcome === 'stream_error' ? '正在整理结果，检查' : '正在整理结果，检查完成';
    const finalText = page.locator('stream-markdown').filter({hasText:expectedText});
    assert.equal(await finalText.count(),1,'final answer must not be duplicated');
    if (outcome === 'stream_error') {
      await page.locator('.assistant-message.assistant > stream-markdown').filter({hasText:'provider_stream_error'}).waitFor();
      const snapshot = await page.evaluate(() => window.liveSnapshots.at(-1));
      assert.equal(snapshot.requests.at(-1).gateway_request_id,'gateway-final-stream');
      assert.equal(snapshot.requests.at(-1).status,'unknown');
      assert.equal(snapshot.requests.at(-1).http_status,200);
      assert.ok(!JSON.stringify(snapshot).includes('private-stream-error-browser-sentinel'));
    }
    await narration.filter({hasText:'先检查工作区'}).waitFor();
    await page.reload({waitUntil:'domcontentloaded'});
    await page.locator('stream-markdown').filter({hasText:expectedText}).waitFor();
    await narration.filter({hasText:'先检查工作区'}).waitFor();
    if (outcome === 'stream_error') {
      await page.locator('.assistant-message.assistant > stream-markdown').filter({hasText:'provider_stream_error'}).waitFor();
      assert.equal(await page.locator('.assistant-message-activity tool-call-badge').count(),1);
      assert.ok(!(await page.locator('body').textContent()).includes('private-stream-error-browser-sentinel'));
    }
    if (process.env.FLEET_SCREENSHOTS) {
      await page.setViewportSize({width:1440,height:1000});
      await page.waitForFunction(() => document.getElementById('sidebar').getBoundingClientRect().left >= 0);
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      await page.screenshot({path:process.env.FLEET_SCREENSHOTS + '/live-feedback-' + outcome + '-complete-desktop.png'});
    }
    await page.evaluate(async () => {
      const {renderProgress} = await import('/assets/views/assistant/progress.js');
      const host = document.createElement('div'); document.body.append(host);
      const segment = {id:'failed-tool',kind:'tool',tool:'workspace.exec',
        state:'failed',error_code:'sandbox_unavailable',
        title:'执行命令 · python3 -c print(ASSISTANT_LONG_RUN_21)'};
      const run = {state:'failed',progress:{segments:[segment]}};
      renderProgress(host,run);
      const badge = host.querySelector('tool-call-badge');
      const label = badge.querySelector('.tool-call-label');
      if (label.getBoundingClientRect().width <= 112 || document.documentElement.scrollWidth > innerWidth) {
        throw new Error('tool operation title must use available width without page overflow');
      }
      const button = badge.querySelector('button'); button.click(); button.focus();
      if (!badge.querySelector('#drawer').textContent.includes('sandbox_unavailable')) {
        throw new Error('failed inline tool must display its specific error');
      }
      renderProgress(host,structuredClone(run));
      if (badge.querySelector('button') !== button || document.activeElement !== button) {
        throw new Error('unchanged error snapshots must preserve drawer and focus');
      }
      delete segment.error_code; segment.state = 'succeeded';
      renderProgress(host,run);
      if (badge.hasAttribute('error') || badge.querySelector('#drawer').textContent.includes('sandbox_unavailable')) {
        throw new Error('a successful receipt must clear stale error details');
      }
      run.progress.truncated=true;
      renderProgress(host,run);
      if (!host.textContent.includes('部分过程记录已省略') || host.querySelector('tool-call-badge') !== badge) {
        throw new Error('retained progress must disclose omission without rebuilding tool cards');
      }
      run.state='running';
      run.progress.retry={provider_error:'timeout',provider_status:200,retry_index:1,max_retries:3};
      const retryNotice=renderProgress(host,run);
      if (!retryNotice.includes('模型响应超时') || !retryNotice.includes('正在自动重试 1/3') || retryNotice.includes('HTTP 200')) {
        throw new Error('interrupted accepted generations must explain timeout and live retry');
      }
      host.remove();
    });
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({beforeEOF:true,refreshRecovery:true,inlineTool:true,retryVisible:true,mobile:true,errors}));
  } catch (error) {
    console.error(JSON.stringify(await page.evaluate(() => {
      const t = document.querySelector('[data-chat-scroll]');
      const a = document.querySelector('auto-scroll-anchor');
      return {scrollHeight:t?.scrollHeight,scrollTop:t?.scrollTop,clientHeight:t?.clientHeight,
        isAtBottom:a?.isAtBottom,streaming:a?.hasAttribute('is-streaming')};
    })));
    console.error(await page.locator('.assistant-transcript').textContent().catch(() => 'no transcript'));
    throw error;
  } finally {await browser.close();}
})().catch(error => {console.error(error);process.exitCode=1;});
