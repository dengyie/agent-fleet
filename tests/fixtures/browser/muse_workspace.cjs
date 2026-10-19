'use strict';
const assert = require('node:assert/strict');
const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const origin = process.argv[2];
(async () => {
  const browser = await chromium.launch({channel: process.env.FLEET_BROWSER_CHANNEL || 'chrome', headless:true});
  const context = await browser.newContext({viewport:{width:1500,height:1000},serviceWorkers:'block'});
  await context.grantPermissions(['clipboard-read','clipboard-write'],{origin});
  const errors=[];
  try {
    let failNextSessionPoll = true;
    await context.route('**/*', async route => {
      const url = route.request().url();
      if (!url.startsWith(origin)) { await route.abort(); return; }
      if (failNextSessionPoll && url.includes('/api/platform/v1/muse/sessions?')) {
        failNextSessionPoll = false;
        await route.fulfill({status:502, contentType:'application/json',
          body:JSON.stringify({ok:false,error:'http_error',detail:'服务返回异常，请稍后重试'})});
        return;
      }
      await route.continue();
    });
    const page=await context.newPage(); page.on('pageerror', e=>errors.push(e.message));
    page.setDefaultTimeout(7000);
    await page.goto(origin+'/muse');
    try { await page.getByRole('heading',{name:'连接你的 Muse'}).waitFor({timeout:10000}); }
    catch (error) { throw new Error(`Muse route did not mount: url=${page.url()} body=${(await page.locator('body').innerText()).slice(0,500)} pageErrors=${errors.join(' | ')}`); }
    await page.getByLabel('Muse 登录态',{exact:true}).fill(JSON.stringify({cookies:[{name:'hatch_sess',value:'fixture-secret',domain:'.muse.ai'}]}));
    await page.getByRole('button',{name:'导入并连接'}).click();
    await page.locator('.muse-message').first().waitFor();
    await page.waitForFunction(()=>document.querySelector('.muse-notice')?.hidden===false);
    await page.waitForFunction(()=>document.querySelector('.muse-connection')?.dataset.connected==='true');
    await page.waitForFunction(()=>document.querySelector('.muse-notice')?.hidden===true, null, {timeout:10000});
    assert.equal(await page.getByLabel('Muse 登录态',{exact:true}).inputValue(),'');
    const firstMessage=page.locator('.muse-message-assistant').first();
    assert.equal(await firstMessage.evaluate(node=>getComputedStyle(node.parentElement).display),'flex');
    await firstMessage.getByRole('button',{name:'复制消息'}).click();
    assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),'已完成资料整理。\n\n**下一步**：实现接入。');
    const mainPane=page.locator('.muse-pane[data-session-id="main"]');
    const mainLayout=await mainPane.evaluate(node=>{
      const activity=node.querySelector('[data-browser-activity]').getBoundingClientRect();
      const transcript=node.querySelector('.muse-transcript').getBoundingClientRect();
      const composer=node.querySelector('.muse-composer').getBoundingClientRect();
      return {activityBottom:activity.bottom,transcriptTop:transcript.top,transcriptBottom:transcript.bottom,composerTop:composer.top,composerBottom:composer.bottom,paneBottom:node.getBoundingClientRect().bottom};
    });
    assert(mainLayout.transcriptTop>=mainLayout.activityBottom-1);
    assert(mainLayout.composerTop>=mainLayout.transcriptTop);
    assert(mainLayout.composerBottom<=mainLayout.paneBottom+1);
    assert.equal(await page.locator('[data-card-type="browser_task"]').count(),1);
    const mainActivity = page.locator('.muse-pane[data-session-id="main"] [data-browser-activity]');
    await mainActivity.waitFor();
    assert.match(await mainActivity.innerText(), /已完成/);
    assert.match(await mainActivity.innerText(), /查阅参考资料/);
    assert.match(await mainActivity.innerText(), /参考来源/);
    assert.match(await mainActivity.innerText(), /已执行 6 步/);
    assert.equal(await mainActivity.getByRole('button',{name:'查看最新浏览器截图'}).count(),1);
    const activityBoundary = await page.evaluate(async()=>{
      const {createBrowserActivity,updateBrowserActivity}=await import('/assets/views/muse/cards.js');
      const {latestBrowserTask,reduceBrowserActivity}=await import('/assets/views/muse/model.js');
      const privateTask=latestBrowserTask([{visibility:'private',event_name:'delta.presentation',
        payload:{kind:'browser_task',data:{activity_title:'private activity'}}}]);
      const panel=createBrowserActivity();
      const newer=reduceBrowserActivity(null,[{seq:10,event_name:'delta.presentation',payload:{kind:'browser_task',data:{activity_title:'newer'}}}]);
      const older=reduceBrowserActivity(newer,[{seq:9,event_name:'delta.presentation',payload:{kind:'browser_task',data:{activity_title:'older'}}}]);
      const sameSequence=reduceBrowserActivity(newer,[{seq:10,event_name:'delta.presentation',payload:{kind:'browser_task',data:{
        current_site:{page_title:'Unsafe URL',url:'javascript:alert(1)'}}}}]);
      updateBrowserActivity(panel,sameSequence);
      const site=panel.querySelector('.muse-browser-activity-site');
      return {privateTask,olderTitle:older.data.activity_title,sameSequenceTitle:sameSequence.data.current_site.page_title,
        siteTag:site?.tagName,siteHref:site?.getAttribute('href')};
    });
    assert.equal(activityBoundary.privateTask,null);
    assert.equal(activityBoundary.olderTitle,'newer');
    assert.equal(activityBoundary.sameSequenceTitle,'Unsafe URL');
    assert.equal(activityBoundary.siteTag,'SPAN');
    assert.equal(activityBoundary.siteHref,null);
    await page.locator('.muse-permissions').evaluate(node=>{node.open=true;});
    const fullAccess=page.getByLabel('自动同意全部权限请求');
    assert.equal(await fullAccess.isChecked(),false);
    await fullAccess.check();
    await page.waitForFunction(async()=>{
      const response=await fetch('/api/platform/v1/muse/status?account_id=default');
      return (await response.json()).settings?.auto_approve_all===true;
    });
    await page.locator('.muse-session[data-session-id="side"]').click();
    await page.locator('[data-card-type="plan"]').waitFor();
    const sideActivity = page.locator('.muse-pane[data-session-id="side"] [data-browser-activity]');
    assert.match(await sideActivity.innerText(), /尚未在此会话返回浏览器活动/);
    assert.equal(await sideActivity.getByRole('button',{name:'查看最新浏览器截图'}).count(),0);
    assert.equal(await page.locator('.muse-message img').count(),0);
    assert.equal(await page.locator('.muse-message script').count(),0);
    const side=page.locator('.muse-pane[data-session-id="side"]');
    const downloadEvent=page.waitForEvent('download');
    await side.getByRole('button',{name:'notes.txt',exact:true}).click();
    assert.equal((await downloadEvent).suggestedFilename(),'notes.txt');
    await side.locator('tool-call-badge button').click();
    // Retain the actual component children across a history poll.
    await side.locator('tool-call-badge button').evaluate(node=>{window.fixtureToolButton=node;});
    await side.getByLabel('发送给 Muse').fill('保留子会话草稿');
    await page.getByRole('button',{name:'并排窗口'}).click();
    assert.equal(await page.locator('.muse-pane:visible').count(),2);
    const main=page.locator('.muse-pane[data-session-id="main"]');
    await main.getByRole('button',{name:'查看最新浏览器截图'}).click();
    await page.waitForFunction(()=>document.querySelector('.muse-pane[data-session-id="main"] img')?.naturalWidth===1);
    await main.getByLabel('发送给 Muse').fill('保留主会话草稿');
    assert.equal(await side.getByLabel('发送给 Muse').inputValue(),'保留子会话草稿');
    await page.waitForFunction(()=>document.querySelector('.muse-pane[data-session-id="side"] input[aria-label="自动审批此会话"]')?.disabled===true);
    assert.equal(await side.getByLabel('自动审批此会话').isDisabled(),true);
    await fullAccess.uncheck();
    await page.waitForFunction(async()=>{
      const response=await fetch('/api/platform/v1/muse/status?account_id=default');
      return (await response.json()).settings?.auto_approve_all===false;
    });
    await side.getByLabel('自动审批此会话').check();
    await page.waitForFunction(()=>document.querySelector('.muse-audit')?.textContent.includes('自动审批'),null,{timeout:20000});
    await fullAccess.check();
    await page.waitForFunction(async()=>{
      const response=await fetch('/api/platform/v1/muse/status?account_id=default');
      return (await response.json()).settings?.auto_approve_all===true;
    });
    assert(await side.locator('tool-call-badge button').evaluate(node=>node===window.fixtureToolButton && node.getAttribute('aria-expanded')==='true'));
    await page.screenshot({path:'/tmp/fleet-muse-workspace-split.png',fullPage:true});
    await page.getByRole('button',{name:'新建子会话'}).click();
    const fresh=page.locator('.muse-pane').last();
    const freshSessionId=await fresh.getAttribute('data-session-id');
    let releaseSend, sendArrived, holdSend=true;
    const sendGate=new Promise(resolve=>{releaseSend=resolve;});
    const sendStarted=new Promise(resolve=>{sendArrived=resolve;});
    let releaseHistory, historyArrived, holdHistory=true, historyCount=0;
    const historyTimes=[];
    const historyGate=new Promise(resolve=>{releaseHistory=resolve;});
    const historyStarted=new Promise(resolve=>{historyArrived=resolve;});
    await page.route(`**/api/platform/v1/muse/sessions/${freshSessionId}/messages`,async route=>{
      if(holdSend){holdSend=false;sendArrived();await sendGate;}
      await route.continue();
    });
    await page.route(`**/api/platform/v1/muse/sessions/${freshSessionId}/history*`,async route=>{
      historyCount++;
      historyTimes.push(Date.now());
      if(holdHistory){holdHistory=false;historyArrived();await historyGate;}
      if(historyCount<=2){
        const response=await route.fetch();
        const body=await response.json();
        if(historyCount===1) body.chat_events=body.chat_events.filter(event=>(event.role||event.payload?.role)!=='assistant');
        if(historyCount===2){
          const assistant=body.chat_events.find(event=>(event.role||event.payload?.role)==='assistant');
          if(assistant) assistant.status='streaming';
        }
        await route.fulfill({response,body:JSON.stringify(body)});
        return;
      }
      if(historyCount>=3 && historyCount<=6){
        const response=await route.fetch();
        const body=await response.json();
        const assistant=body.chat_events.find(event=>(event.role||event.payload?.role)==='assistant');
        if(assistant) assistant.status='streaming';
        await route.fulfill({response,body:JSON.stringify(body)});
        return;
      }
      if(historyCount===7){
        const response=await route.fetch();
        const body=await response.json();
        body.chat_events.push({seq:104,message_id:'final-answer',role:'assistant',event_name:'message.assistant',display_text:'FLEET_FINAL'});
        await route.fulfill({response,body:JSON.stringify(body)});
        return;
      }
      await route.continue();
    });
    await fresh.getByLabel('发送给 Muse').fill('这是夹具测试');
    const submit=fresh.getByRole('button',{name:'发送',exact:true}).click();
    await sendStarted;
    const sendingBubble=fresh.locator('.muse-transient-message[data-transient-message="user"]');
    await sendingBubble.waitFor();
    assert.match(await sendingBubble.innerText(),/这是夹具测试/);
    assert.equal(await sendingBubble.evaluate(node=>getComputedStyle(node).alignSelf),'flex-end');
    assert.equal(await sendingBubble.getAttribute('data-delivery-phase'),'sending');
    assert.equal(await sendingBubble.locator('.muse-delivery-spinner').evaluate(node=>getComputedStyle(node).animationName),'muse-spin');
    releaseSend();
    await historyStarted;
    assert.equal(await fresh.locator('.muse-thinking-message .muse-delivery-label').innerText(),'Muse 正在思考…');
    assert.equal(await fresh.locator('.muse-thinking-message').getAttribute('aria-live'),'polite');
    releaseHistory();
    await submit;
    await fresh.locator('.muse-thinking-message .muse-delivery-label').filter({hasText:'Muse 正在输出…'}).waitFor();
    const streamed= fresh.locator('.muse-message-assistant stream-markdown[is-streaming]');
    await streamed.waitFor();
    assert.equal(await streamed.evaluate(node=>getComputedStyle(node,'::after').animationName),'muse-caret-blink');
    const completedPolls=historyCount;
    await page.getByRole('button',{name:'单窗口'}).click();
    await page.locator('#muse-tab-main').click();
    await page.waitForTimeout(1100);
    assert.equal(historyCount,completedPolls,'hidden Muse panes must not keep the fast reply poll running');
    await page.locator(`#muse-tab-${freshSessionId}`).click();
    await page.getByRole('button',{name:'并排窗口'}).click();
    await fresh.getByText('FLEET_OK',{exact:true}).waitFor();
    await fresh.getByText('FLEET_FINAL',{exact:true}).waitFor();
    assert(historyTimes.length>=7);
    assert(historyTimes.slice(1,7).every((time,index)=>time-historyTimes[index]<1800),
      `reply polling should stay pane-local; timestamps=${historyTimes.join(',')}`);
    await fresh.locator('.muse-transient-message').waitFor({state:'detached'});
    assert.equal(await fresh.locator('.muse-transient-message').count(),0);
    assert.equal(await fresh.getByLabel('发送给 Muse').getAttribute('readonly'),null);
    assert.equal(await main.getByLabel('发送给 Muse').inputValue(),'保留主会话草稿');
    await fresh.getByLabel('发送给 Muse').fill('uncertain');
    await fresh.getByRole('button',{name:'发送',exact:true}).click();
    await fresh.getByRole('button',{name:'核对投递结果'}).waitFor();
    const unknownBubble=fresh.locator('.muse-transient-message[data-transient-message="user"]');
    await unknownBubble.waitFor();
    assert.equal(await unknownBubble.getAttribute('data-delivery-phase'),'unknown');
    assert.match(await unknownBubble.innerText(),/结果待核对/);
    await fresh.getByRole('button',{name:'核对投递结果'}).click();
    await page.waitForFunction(()=>!Array.from(document.querySelectorAll('.muse-composer button')).find(b=>b.textContent==='核对投递结果')?.disabled);
    assert.equal(await fresh.getByLabel('发送给 Muse').getAttribute('readonly'),'');
    await fresh.getByRole('button',{name:'已核对，继续输入'}).click();
    assert.equal(await fresh.getByLabel('发送给 Muse').inputValue(),'');
    assert.equal(await fresh.getByLabel('发送给 Muse').getAttribute('readonly'),null);
    // Route caching must retain drafts and stop active polling on suspension.
    await page.locator('[data-nav="assistant"]').click();
    await page.locator('[data-nav="muse"]').click();
    assert.equal(await page.locator('.muse-pane[data-session-id="main"]').getByLabel('发送给 Muse').inputValue(),'保留主会话草稿');
    await page.setViewportSize({width:390,height:844});
    await page.waitForFunction(()=>document.querySelector('.console-sidebar').getBoundingClientRect().right<=1);
    await page.screenshot({path:'/tmp/fleet-muse-workspace-mobile.png',fullPage:true});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1));
    assert.equal(await page.locator('.muse-message-actions').first().evaluate(node=>getComputedStyle(node).opacity),'1');
    await page.setViewportSize({width:1500,height:1000});
    await page.waitForFunction(()=>document.querySelector('.console-sidebar').getBoundingClientRect().right>1);
    let releaseImport;
    let importArrived;
    const importStarted = new Promise(resolve=>{importArrived=resolve;});
    const importGate = new Promise(resolve=>{releaseImport=resolve;});
    await page.getByRole('button',{name:'添加账号'}).click();
    await page.getByLabel('Muse 账号名称').fill('第二个账号');
    await page.getByLabel('Muse 登录态',{exact:true}).fill(JSON.stringify({cookies:[{name:'hatch_sess',value:'second-fixture',domain:'.muse.ai'}]}));
    await page.getByRole('button',{name:'导入并连接'}).click();
    await page.waitForFunction(()=>document.querySelector('.muse-account-select')?.options.length===2);
    const secondAccount=await page.getByLabel('Muse 账号',{exact:true}).inputValue();
    assert.notEqual(secondAccount,'default');
    assert.equal(await fullAccess.isChecked(),false);
    await page.route('**/api/platform/v1/muse/login', async route=>{
      const body=route.request().postDataJSON();
      if(String(body.login || '').includes('third-fixture')) { importArrived(); await importGate; }
      await route.continue();
    });
    await page.getByRole('button',{name:'添加账号'}).click();
    await page.getByLabel('Muse 账号名称').fill('第三个账号');
    await page.getByLabel('Muse 登录态',{exact:true}).fill(JSON.stringify({cookies:[{name:'hatch_sess',value:'third-fixture',domain:'.muse.ai'}]}));
    const importRequest=page.getByRole('button',{name:'导入并连接'}).click();
    await Promise.race([importStarted, page.waitForTimeout(10000).then(()=>{throw new Error('third import did not reach route');})]);
    await page.getByLabel('Muse 账号',{exact:true}).selectOption('default');
    releaseImport();
    await importRequest;
    await page.waitForFunction(()=>document.querySelector('.muse-account-select')?.value==='default');
    await page.waitForFunction(()=>document.querySelector('input[aria-label="自动同意全部权限请求"]')?.checked===true);
    assert.equal(await fullAccess.isChecked(),true);
    await page.locator('.muse-pane[data-session-id="main"]').waitFor();
    assert.equal(await page.locator('.muse-pane[data-session-id="main"]').count(),1);
    await page.getByRole('button',{name:'单窗口',exact:true}).click();
    await page.locator('.muse-session[data-session-id="main"]').click();
    const older=main.getByRole('button',{name:'加载更早消息'});
    if(await older.isVisible()) await older.click();
    await page.locator('.muse-toolbar').screenshot({path:'/tmp/fleet-muse-toolbar.png'});
    await page.screenshot({path:'/tmp/fleet-muse-workspace.png',fullPage:true});
    const disconnectButton = page.getByRole('button',{name:'断开连接',exact:true});
    if (await disconnectButton.isHidden()) await page.getByRole('button',{name:'管理登录态'}).click();
    await disconnectButton.click();
    await page.waitForFunction(()=>document.querySelector('.muse-connection')?.textContent==='未连接');
    assert.equal(await page.locator('.muse-message').count(),0);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({ok:true,checked:'import, native session panes, independent drafts, rich cards, XSS, approvals, unknown replay, route cache, mobile, disconnect'}));
  } finally {await context.close();await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
