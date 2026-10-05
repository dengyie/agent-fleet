const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const fs = require('node:fs');
const [origin, conversation] = process.argv.slice(2);
(async () => {
  const browser = await chromium.launch({channel:process.env.FLEET_BROWSER_CHANNEL || 'chromium',headless:true});
  let page;
  try {
    page = await browser.newPage({viewport:{width:1440,height:900}});
    const errors = []; page.on('pageerror', e => errors.push(e.message));
    let conversationReads = 0;
    page.on('request', r => {if (r.url().endsWith('/conversations/' + conversation)) conversationReads++;});
    await page.goto(origin + '/conversation/' + conversation);
    const requests = page.locator('.request-metadata');
    await requests.nth(1).waitFor();
    assert.equal(await requests.count(),2,'both 429 and pending retry must be visible');
    assert.equal(await page.locator('parsererror').count(),0,'icon geometry must be valid XML');
    const iconSizes = await requests.locator('summary svg').evaluateAll(nodes => nodes.map(n => n.getBoundingClientRect().height));
    assert.ok(iconSizes.length >= 14 && iconSizes.every(height => height > 0 && height <= 24),'metadata icons stay compact');
    assert.match(await requests.first().textContent(),/请求失败/);
    const active = requests.nth(1);
    await active.locator('summary').focus(); await page.keyboard.press('Enter');
    assert.equal(await active.getAttribute('open'),'');
    const elapsed = await active.locator('.request-duration').textContent();
    await page.waitForFunction(old => document.querySelectorAll('.request-duration')[1].textContent !== old, elapsed);
    await active.evaluate(n => {window.metadataNode=n;window.metadataSummary=n.querySelector('summary');});
    await page.waitForResponse(r => r.url().includes('/runs/') && !r.url().includes('/events'));
    assert.equal(await active.evaluate(n => n === window.metadataNode && n.querySelector('summary') === window.metadataSummary),true);
    assert.equal(await active.getAttribute('open'),'','polling must preserve expanded details');
    assert.equal(conversationReads,1,'request polling must not refetch full history');
    await page.evaluate(() => fetch('/test/release-provider',{method:'POST'}));
    await page.locator('.assistant-message.assistant strong').filter({hasText:'完成'}).first().waitFor();
    await page.waitForFunction(() => document.querySelectorAll('.request-metadata')[1].dataset.state === 'succeeded');
    assert.equal(await active.getAttribute('open'),'','completion preserves expanded details');
    const summary = active.locator('summary');
    assert.match(await summary.textContent(),/actual-model/);
    assert.match(await summary.textContent(),/\$0\.000131/);
    assert.deepEqual(await summary.locator('.request-tokens .request-chip-value').allTextContents(),['47','100','10','14','0']);
    assert.match(await active.textContent(),/chatcmpl-metadata/);
    assert.match(await active.textContent(),/frozen-model/);
    assert.equal(await page.locator('stream-markdown strong').first().textContent(),'完成','keyed updates retain rendered markdown');
    const finalDuration = await active.locator('.request-duration').textContent();
    await page.reload(); await requests.nth(1).waitFor();
    assert.equal(await requests.nth(1).locator('.request-duration').textContent(),finalDuration);
    for (const reduced of ['no-preference','reduce']) {
      await page.emulateMedia({reducedMotion:reduced,colorScheme:'dark'});
      await page.setViewportSize({width:390,height:844});
      await requests.nth(1).locator('summary').click();
      const metrics = await page.evaluate(() => ({overflow:document.documentElement.scrollWidth > innerWidth,
        animation:getComputedStyle(document.querySelectorAll('.request-detail')[1]).animationName}));
      assert.equal(metrics.overflow,false);
      if (reduced === 'reduce') assert.equal(metrics.animation,'none');
      await requests.nth(1).evaluate(n => n.open=false);
    }
    // Long and hostile response identifiers remain text, and metadata updates
    // never rebuild the existing Markdown or steal focus/text selection.
    await page.evaluate(async () => {
      const {renderRequestMetadata} = await import('/assets/views/assistant/request-metadata.js');
      const host = document.querySelectorAll('.message-requests')[1];
      renderRequestMetadata(host,[{request_id:'x',attempt:1,step:1,request_index:1,status:'succeeded',
        model:'<img src=x onerror="window.xss=true">',started_at:1,duration_ms:0,usage:{},cost:null}]);
    });
    assert.equal(await page.locator('.message-requests img').count(),0);
    assert.equal(await page.evaluate(() => Boolean(window.xss)),false);
    assert.match(await page.locator('.message-requests').last().textContent(),/费用未提供/);
    await page.evaluate(async () => {
      const {renderRequestMetadata} = await import('/assets/views/assistant/request-metadata.js');
      renderRequestMetadata(document.querySelectorAll('.message-requests')[1], [{request_id:'unknown',
        attempt:1,step:1,request_index:1,status:'unknown',started_at:1,duration_ms:null}]);
    });
    assert.match(await page.locator('.message-requests').last().textContent(),/耗时未确认/);
    assert.doesNotMatch(await page.locator('.message-requests').last().textContent(),/请求中/);
    if (process.env.FLEET_SCREENSHOTS) {
      fs.mkdirSync(process.env.FLEET_SCREENSHOTS,{recursive:true});
      await page.reload(); await requests.nth(1).waitFor();
      await requests.nth(1).locator('summary').click();
      await page.screenshot({path:process.env.FLEET_SCREENSHOTS+'/request-metadata-mobile.png'});
      await page.setViewportSize({width:1440,height:1000});
      await page.screenshot({path:process.env.FLEET_SCREENSHOTS+'/request-metadata-desktop.png'});
    }
    const performanceEvidence = await page.evaluate(async () => {
      const {renderRequestMetadata, tickRequestMetadata} = await import('/assets/views/assistant/request-metadata.js');
      const host = document.createElement('div');
      const rows = Array.from({length:120}, (_,index) => ({request_id:'perf-'+index,attempt:1,step:1,
        request_index:index+1,status:'succeeded',model:'model',started_at:1,duration_ms:2988,usage:{input_tokens:47,output_tokens:14},cost:null}));
      const start = performance.now(); renderRequestMetadata(host,rows); const renderMs=performance.now()-start;
      const observer = new MutationObserver(() => {}); observer.observe(host,{subtree:true,childList:true,attributes:true,characterData:true});
      const update = performance.now(); renderRequestMetadata(host,rows); const updateMs=performance.now()-update;
      const changes=observer.takeRecords().length; observer.disconnect();
      return {requests:host.children.length,renderMs,updateMs,changes,idleClock:tickRequestMetadata(host)};
    });
    assert.equal(performanceEvidence.requests,120);
    // No-op updates and idle timers must do no DOM work, independent of CPU speed.
    assert.equal(performanceEvidence.changes,0);
    assert.equal(performanceEvidence.idleClock,false);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({requests:2,liveClock:true,historyReads:conversationReads,restored:true,mobile:true,reducedMotion:true,performanceEvidence,errors}));
  } catch (error) {
    if (page) {
      console.error('layout', await page.evaluate(() => Array.from(document.querySelectorAll('.assistant-thread,.assistant-transcript,.request-summary,.composer-dock')).map(n=>({class:n.className,rect:n.getBoundingClientRect().toJSON(),scrollTop:n.scrollTop,scrollHeight:n.scrollHeight,clientHeight:n.clientHeight}))));
      if (process.env.FLEET_SCREENSHOTS) { fs.mkdirSync(process.env.FLEET_SCREENSHOTS,{recursive:true}); await page.screenshot({path:process.env.FLEET_SCREENSHOTS+'/metadata-failure.png'}); }
    }
    throw error;
  } finally { await browser.close(); }
})().catch(error => {console.error(error);process.exitCode=1;});
