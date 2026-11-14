const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const fs = require('node:fs');
const [origin, conversation, runId] = process.argv.slice(2);
(async () => {
  const browser = await chromium.launch({channel:process.env.FLEET_BROWSER_CHANNEL || 'chrome',headless:true});
  const page = await browser.newPage({viewport:{width:1440,height:1000}});
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  try {
    await page.goto(origin + '/conversation/' + conversation,{waitUntil:'domcontentloaded'});
    await page.locator('.assistant-message.user').waitFor();
    await page.request.post(origin + '/_fixture/start');
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
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
    if (process.env.FLEET_SCREENSHOTS) {
      fs.mkdirSync(process.env.FLEET_SCREENSHOTS,{recursive:true});
      await page.screenshot({path:process.env.FLEET_SCREENSHOTS + '/live-feedback-retry-mobile.png'});
    }
    await page.request.post(origin + '/_fixture/release/retry');
    await narration.filter({hasText:'正在整理结果'}).waitFor();
    assert.equal(await page.locator('.assistant-message.assistant').getAttribute('aria-busy'),'true');
    await page.request.post(origin + '/_fixture/release/final');
    await page.waitForFunction(() => document.querySelector('.assistant-message.assistant')?.getAttribute('aria-busy') === 'false');
    await page.waitForFunction(() => window.liveSnapshots.some(run => run.state === 'succeeded'));
    assert.equal(await page.evaluate(() => window.liveStreamError),null,'SSE must work independently of polling');
    const finalText = page.locator('stream-markdown').filter({hasText:'正在整理结果，检查完成'});
    assert.equal(await finalText.count(),1,'final answer must not be duplicated');
    await narration.filter({hasText:'先检查工作区'}).waitFor();
    await page.reload({waitUntil:'domcontentloaded'});
    await page.locator('stream-markdown').filter({hasText:'正在整理结果，检查完成'}).waitFor();
    await narration.filter({hasText:'先检查工作区'}).waitFor();
    if (process.env.FLEET_SCREENSHOTS) {
      await page.setViewportSize({width:1440,height:1000});
      await page.screenshot({path:process.env.FLEET_SCREENSHOTS + '/live-feedback-complete-desktop.png'});
    }
    await page.evaluate(async () => {
      const {renderProgress} = await import('/assets/views/assistant/progress.js');
      const host = document.createElement('div'); document.body.append(host);
      const segment = {id:'failed-tool',kind:'tool',tool:'workspace.exec',
        state:'failed',error_code:'sandbox_unavailable'};
      const run = {state:'failed',progress:{segments:[segment]}};
      renderProgress(host,run);
      const badge = host.querySelector('tool-call-badge');
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
      host.remove();
    });
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({beforeEOF:true,refreshRecovery:true,inlineTool:true,retryVisible:true,mobile:true,errors}));
  } catch (error) {
    console.error(await page.locator('.assistant-transcript').textContent().catch(() => 'no transcript'));
    throw error;
  } finally {await browser.close();}
})().catch(error => {console.error(error);process.exitCode=1;});
