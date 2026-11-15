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
    await page.goto(origin + '/conversation/' + conversation, {waitUntil: 'domcontentloaded'});
    const group = page.locator('.request-group');
    await group.waitFor();
    assert.equal(await group.getAttribute('open'),null,'multiple requests start collapsed into one row');
    await group.locator(':scope > summary').click();
    const requests = page.locator('.request-metadata');
    await requests.nth(1).waitFor();
    assert.equal(await requests.count(),2,'both 429 and pending retry must be visible');
    assert.equal(await page.locator('parsererror').count(),0,'icon geometry must be valid XML');
    const iconSizes = await requests.locator('summary svg').evaluateAll(nodes => nodes.map(n => n.getBoundingClientRect().height));
    assert.ok(iconSizes.length >= 14 && iconSizes.every(height => height > 0 && height <= 24),'metadata icons stay compact: ' + JSON.stringify(iconSizes));
    assert.match(await requests.first().textContent(),/请求失败/);
    assert.match(await requests.first().locator('.request-fields').textContent(),/API 网关请求编号gateway-retry-1/);
    assert.doesNotMatch(await group.locator(':scope > summary').textContent(),/gateway-retry/,'correlation details stay folded');
    const active = requests.nth(1);
    await active.locator('summary').focus(); await page.keyboard.press('Enter');
    assert.equal(await active.getAttribute('open'),'');
    const elapsed = await active.locator('.request-duration').textContent();
    await page.waitForFunction(old => document.querySelectorAll('.request-metadata .request-duration')[1].textContent !== old, elapsed);
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
    assert.match(await active.locator('.request-fields').textContent(),/API 网关请求编号gateway-retry-2/);
    assert.match(await active.locator('.request-fields').textContent(),new RegExp('上游请求编号' + 'u'.repeat(128)));
    assert.match(await active.textContent(),/frozen-model/);
    assert.equal(await page.locator('stream-markdown strong').first().textContent(),'完成','keyed updates retain rendered markdown');
    if (process.env.FLEET_SCREENSHOTS) {
      fs.mkdirSync(process.env.FLEET_SCREENSHOTS,{recursive:true});
      await active.locator('dd').filter({hasText:'gateway-retry-2'}).scrollIntoViewIfNeeded();
      await page.screenshot({path:process.env.FLEET_SCREENSHOTS+'/request-correlation-desktop.png'});
    }
    const finalDuration = await active.locator('.request-duration').textContent();
    await page.reload(); await group.waitFor(); await group.locator(':scope > summary').click(); await requests.nth(1).waitFor();
    assert.equal(await requests.nth(1).locator('.request-duration').textContent(),finalDuration);
    assert.match(await requests.first().textContent(),/gateway-retry-1/);
    assert.match(await requests.nth(1).textContent(),/gateway-retry-2/);
    for (const reduced of ['no-preference','reduce']) {
      await page.emulateMedia({reducedMotion:reduced,colorScheme:'dark'});
      await page.setViewportSize({width:390,height:844});
      await page.waitForFunction(() => document.getElementById('sidebar').getBoundingClientRect().right <= 1);
      await requests.nth(1).locator('summary').click();
      await requests.nth(1).locator('dd').filter({hasText:'gateway-retry-2'}).scrollIntoViewIfNeeded();
      const metrics = await page.evaluate(() => ({overflow:document.documentElement.scrollWidth > innerWidth,
        animation:getComputedStyle(document.querySelectorAll('.request-detail')[1]).animationName}));
      assert.equal(metrics.overflow,false);
      if (reduced === 'reduce') assert.equal(metrics.animation,'none');
      if (reduced === 'no-preference' && process.env.FLEET_SCREENSHOTS)
        await page.screenshot({path:process.env.FLEET_SCREENSHOTS+'/request-correlation-mobile.png'});
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
      await page.reload(); await group.waitFor(); await group.locator(':scope > summary').click(); await requests.nth(1).waitFor();
      await requests.nth(1).locator('summary').click();
      await page.screenshot({path:process.env.FLEET_SCREENSHOTS+'/request-metadata-mobile.png'});
      await page.setViewportSize({width:1440,height:1000});
      await page.screenshot({path:process.env.FLEET_SCREENSHOTS+'/request-metadata-desktop.png'});
    }
    await page.evaluate(async () => {
      const {renderRequestMetadata} = await import('/assets/views/assistant/request-metadata.js');
      const host = document.createElement('div'); document.body.append(host);
      const make = (id, input, cache, output, reasoning, cost) => ({request_id:id,status:'succeeded',model:'deepseek',
        started_at:1,duration_ms: id === 'one' ? 2485 : 2574,
        usage:{input_tokens:input,cache_read_tokens:cache,cache_write_tokens:0,output_tokens:output,reasoning_tokens:reasoning},
        cost:{currency:'USD',source:'configured',amount:cost}});
      const rows = [make('one',673,0,75,16,'0.00014595'),make('two',825,640,235,56,'0.00017067')];
      renderRequestMetadata(host,rows);
      const group=host.querySelector('.request-group'), summary=group.querySelector(':scope > summary');
      assertGroup(!group.open && host.children.length === 1,'one collapsed group');
      assertGroup(JSON.stringify(Array.from(summary.querySelectorAll('.request-tokens .request-chip-value'),n=>n.textContent)) === JSON.stringify(['858','640','72','238','0']),'token sums');
      assertGroup(summary.textContent.includes('≈$0.00031662') && summary.textContent.includes('5059ms') && summary.textContent.includes('2 次请求'),'exact decimal cost/duration/count');
      group.open=true;
      rows[1].cost=null; rows[1].usage.reasoning_tokens=null;
      renderRequestMetadata(host,rows);
      assertGroup(group.open && group === host.querySelector('.request-group'),'aggregate expansion survives updates');
      assertGroup(summary.textContent.includes('≥≈$0.00014595') && summary.textContent.includes('≥16'),'missing accounting is partial rather than zero');
      const totals = {count:250,complete:true,models:['deepseek'],model_count:1,
        display_usage:{input_tokens:12500,output_tokens:3750,cache_read_tokens:10000,cache_write_tokens:2500,reasoning_tokens:1250},
        known:Object.fromEntries(['input_tokens','output_tokens','cache_read_tokens','cache_write_tokens','reasoning_tokens'].map(key=>[key,250])),
        duration_ms:784375,duration_known:250,cost:{amount:'0.00030750',currency:'USD',source:'configured'},cost_known:250,
        states:{succeeded:250}};
      renderRequestMetadata(host,rows,totals,248);
      assertGroup(summary.textContent.includes('250 次请求') && summary.textContent.includes('≈$0.00030750'), 'full totals survive detail truncation');
      assertGroup(summary.querySelector('.request-tokens .request-chip-value').textContent === '12500','full token count rather than retained rows');
      assertGroup(group.textContent.includes('248') && group.textContent.includes('明细'),'detail omission is disclosed');
      totals.complete=false; totals.known.input_tokens=2; totals.cost_known=2;
      renderRequestMetadata(host,rows,totals,248);
      assertGroup(summary.textContent.includes('≥12500') && summary.textContent.includes('≥≈$0.00030750'),'legacy partial totals are lower bounds');
      renderRequestMetadata(host,[]); assertGroup(host.children.length===0,'empty request collection removes summary');
      host.remove();
      function assertGroup(value,message){if(!value)throw new Error(message);}
    });
    const performanceEvidence = await page.evaluate(async () => {
      const {renderRequestMetadata, tickRequestMetadata} = await import('/assets/views/assistant/request-metadata.js');
      const host = document.createElement('div');
      const rows = Array.from({length:120}, (_,index) => ({request_id:'perf-'+index,attempt:1,step:1,
        request_index:index+1,status:'succeeded',model:'model',started_at:1,duration_ms:2988,usage:{input_tokens:47,output_tokens:14},cost:null}));
      const start = performance.now(); renderRequestMetadata(host,rows); const renderMs=performance.now()-start;
      const observer = new MutationObserver(() => {}); observer.observe(host,{subtree:true,childList:true,attributes:true,characterData:true});
      const update = performance.now(); renderRequestMetadata(host,rows); const updateMs=performance.now()-update;
      const changes=observer.takeRecords().length; observer.disconnect();
      const icons = Array.from(host.querySelectorAll('svg'));
      const reordered = rows.map(row => Object.fromEntries(Object.entries(row).reverse().map(([key,value]) =>
        [key, key === 'usage' ? Object.fromEntries(Object.entries(value).reverse()) : value])));
      observer.observe(host,{subtree:true,childList:true,attributes:true,characterData:true});
      renderRequestMetadata(host,reordered);
      const reorderedChanges=observer.takeRecords().length; observer.disconnect();
      const retainedIcons=Array.from(host.querySelectorAll('svg')).every((icon,index) => icon === icons[index]);
      return {requests:host.querySelectorAll('.request-metadata').length,renderMs,updateMs,changes,reorderedChanges,retainedIcons,idleClock:tickRequestMetadata(host)};
    });
    assert.equal(performanceEvidence.requests,120);
    // No-op updates and idle timers must do no DOM work, independent of CPU speed.
    assert.equal(performanceEvidence.changes,0);
    assert.equal(performanceEvidence.reorderedChanges,0,'SSE and polling key order must not rebuild metadata');
    assert.equal(performanceEvidence.retainedIcons,true,'equivalent snapshots preserve icon identity');
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
