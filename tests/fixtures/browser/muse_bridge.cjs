'use strict';
const assert = require('node:assert/strict');
const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const {operate, selectTarget, validateRequest} = require('../../../tools/browser_bridge/cdp_muse.cjs');

(async () => {
  assert.throws(() => validateRequest({op:'evaluate', expression:'evil'}), /operation_forbidden/);
  assert.throws(() => selectTarget([{type:'page',id:'a',url:'https://muse.ai/',webSocketDebuggerUrl:'ws://evil.test:9222/devtools/page/a'}]), /devtools_endpoint_invalid/);
  assert.throws(() => selectTarget([{type:'page',id:'a',url:'https://muse.ai/'} , {type:'page',id:'b',url:'https://muse.ai/'}]), /muse_tabs_ambiguous/);
  const browser = await chromium.launch({channel:process.env.FLEET_BROWSER_CHANNEL || 'chrome',headless:true});
  try {
    const context = await browser.newContext({serviceWorkers:'block'});
    await context.route('**/*', route => route.fulfill({contentType:'text/html',body:`<!DOCTYPE html><meta charset="utf-8">
      <div role="log" data-hatch-chat-column="true"><div data-message-item data-message-id="old" data-message-role="assistant"><div data-hatch-assistant-message-body>Old answer</div></div></div>
      <div data-hatch-composer-chrome><textarea></textarea><button aria-label="发送" disabled>Send</button></div>
      <script>
      const field = document.querySelector('textarea'), button = document.querySelector('button');
      window.sends = 0;
      field.addEventListener('input', () => { button.disabled = !field.value; });
      button.addEventListener('click', () => {
        window.sends++;
        const message = document.createElement('div'); message.dataset.messageItem='true';
        message.dataset.messageId='new-user-' + window.sends; message.dataset.messageRole='user';
        const text = document.createElement('p'); text.className='whitespace-pre-wrap'; text.style.whiteSpace='pre-wrap'; text.textContent=field.value; message.append(text);
        document.querySelector('[role=log]').append(message); field.value=''; button.disabled=true;
      });
      </script>`}));
    const page = await context.newPage(); await page.goto('https://muse.ai/');
    const session = await context.newCDPSession(page);
    const call = (method, params) => session.send(method, params);
    const snapshot = await operate(call, {op:'snapshot'}, 'fixture');
    assert.equal(snapshot.ready, true); assert.equal(snapshot.draft, false);
    assert.equal(snapshot.messages[0].text, 'Old answer');
    await page.locator('textarea').fill('private draft');
    await assert.rejects(operate(call, {op:'send',target_id:'fixture',cursor:'old',prompt:'test'}, 'fixture'), /composer_has_draft/);
    assert.equal(await page.locator('textarea').inputValue(), 'private draft');
    await page.locator('textarea').fill('');
    await assert.rejects(operate(call, {op:'send',target_id:'fixture',cursor:'stale',prompt:'test'}, 'fixture'), /conversation_changed/);
    await page.locator('[data-hatch-assistant-message-body]').evaluate(el => el.setAttribute('aria-busy','true'));
    await assert.rejects(operate(call, {op:'send',target_id:'fixture',cursor:'old',prompt:'test'}, 'fixture'), /agent_busy/);
    assert.equal(await page.locator('textarea').inputValue(), '');
    assert.equal(await page.evaluate(() => window.sends), 0);
    await page.locator('[data-hatch-assistant-message-body]').evaluate(el => el.removeAttribute('aria-busy'));
    await page.locator('textarea').evaluate(el => el.addEventListener('input', () => {
      document.querySelector('[data-hatch-assistant-message-body]').setAttribute('aria-busy','true');
    }, {once:true}));
    await assert.rejects(operate(call, {op:'send',target_id:'fixture',cursor:'old',prompt:'test'}, 'fixture'), /agent_busy/);
    assert.equal(await page.evaluate(() => window.sends), 0);
    await page.locator('textarea').fill('');
    await page.locator('[data-hatch-assistant-message-body]').evaluate(el => el.removeAttribute('aria-busy'));
    const prompt = 'Text with "quotes", \\slashes, ${noCode} and 中文\nsecond line';
    const sent = await operate(call, {op:'send',target_id:'fixture',cursor:'old',prompt}, 'fixture');
    assert.equal(sent.sent, true); assert.equal(await page.evaluate(() => window.sends), 1);
    const after = await operate(call, {op:'snapshot'}, 'fixture');
    assert.equal(after.messages.at(-1).text, prompt);
    const unicodePrompt = '😀'.repeat(8192);
    await operate(call, {op:'send',target_id:'fixture',cursor:after.messages.at(-1).id,prompt:unicodePrompt}, 'fixture');
    const unicodeAfter = await operate(call, {op:'snapshot'}, 'fixture');
    assert.equal(unicodeAfter.messages.at(-1).text, unicodePrompt);
    await page.goto('https://muse.ai/other');
    await assert.rejects(operate(call, {op:'send',target_id:'fixture',cursor:'old',prompt}, 'fixture'), /page_changed/);
    console.log(JSON.stringify({ok:true, assertions:'fixed operations, endpoint pinning, ambiguity, draft preservation, cursor fence, exact Unicode send, origin fence'}));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode=1; });
