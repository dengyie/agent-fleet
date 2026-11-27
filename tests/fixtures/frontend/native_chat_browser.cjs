const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const fs = require('node:fs');
const [origin, scenario, sid, taskId] = process.argv.slice(2);
(async () => {
  const browser = await chromium.launch({channel:process.env.FLEET_BROWSER_CHANNEL || 'chrome', headless:true});
  fs.mkdirSync('/tmp/fleet-native-web', {recursive:true});
  try {
    for (const width of [1440,390]) {
      const page = await browser.newPage({viewport:{width,height:1000}});
      if (['local-profile','control-confirmation'].includes(scenario)) await page.addInitScript(theme => localStorage.setItem('agent-fleet-theme',theme),width === 390 ? 'dark' : 'light');
      page.setDefaultTimeout(9000);
      const errors = []; page.on('pageerror', error => errors.push(error.message));
      let writes = 0;
      page.on('request', request => {if(request.method() === 'POST' && /\/messages$|\/control$|\/api\/tasks$/.test(request.url())) writes++;});
      await page.route('**/api/operator/session', async route => {
        const response = await route.fetch(), body = await response.json();
        await route.fulfill({response, json:{...body, authenticated:true, user:{id:'browser-owner',role:'admin',name:'Browser Owner'}}});
      });
      if (scenario === 'directory' || scenario === 'directory-pagination' || scenario === 'directory-metadata') {
        await page.goto(origin + '/assistant?view=sessions');
        await page.locator('.native-session-card').first().waitFor();
        if (scenario === 'directory-metadata') {
          const card = page.locator('.native-session-card').filter({hasText:'VM native title'}).first();
          await card.waitFor();
          const detail = await card.innerText();
          for (const fact of ['client-1.2','provider-y','model-x']) {
            assert.ok(detail.includes(fact), 'missing directory detail fact: ' + fact + '\n' + detail);
          }
          assert.deepEqual(errors,[]); await page.close(); continue;
        }
        if (scenario === 'directory-pagination') {
          const loadMore = page.getByRole('button',{name:'加载更多会话'});
          if (!await loadMore.isVisible().catch(() => false)) throw new Error('directory page has no continuation; status=' + await page.locator('.native-directory-status').innerText() + ', button=' + await page.locator('.native-directory > button').evaluate(node => node.outerHTML) + ', rows=' + await page.locator('.native-session-card').count());
          assert.equal(await page.locator('.native-session-card').count(),100);
          await page.getByRole('button',{name:'加载更多会话'}).click();
          await page.waitForFunction(() => document.querySelectorAll('.native-session-card').length >= 106);
          assert.equal(await page.getByRole('button',{name:'加载更多会话'}).count(),0,'last page removes the load-more action');
          assert.deepEqual(errors,[]); await page.close(); continue;
        }
        await page.getByLabel('筛选已载入会话').fill('no-match');
        await page.getByText('没有匹配的已载入会话', {exact:true}).waitFor();
        await page.getByLabel('筛选已载入会话').fill('codex');
        await page.getByLabel('筛选会话能力').selectOption('read');
        assert.equal(await page.locator('.native-session-card').count(),0);
        await page.getByLabel('筛选会话能力').selectOption('message');
        await page.locator('.native-session-card').first().click();
        await page.locator('.native-chat-view').waitFor();
        await page.getByText('Native answer from persisted transcript', {exact:true}).waitFor();
      } else if (['create', 'task-recovery', 'missing-targets', 'refresh-targets', 'target-links', 'offline-target'].includes(scenario)) {
        let offline = true;
        if (scenario === 'missing-targets') await page.route('**/api/machines/*/task-targets', route =>
          route.fulfill({status:404,contentType:'application/json',body:JSON.stringify({error:'not_found'})}));
        if (scenario === 'refresh-targets' || scenario === 'offline-target') await page.route('**/api/status', async route => {
          const response = await route.fetch(), body = await response.json();
          if (scenario === 'offline-target') body.machines.push({...body.machines[0],machine:'other-online',online:true});
          if (offline) for (const machine of body.machines) if (machine.machine !== 'other-online') machine.online = false;
          await route.fulfill({response, json: body});
        });
        await page.goto(origin + '/assistant?view=computer' + (scenario === 'offline-target' ? '&machine=m1' : ''));
        if (scenario === 'offline-target') {
          await page.getByText('所选节点当前离线。仍可阅读已同步会话；恢复连接后刷新，或手动选择其它节点。',{exact:true}).waitFor();
          assert.equal(await page.getByLabel('电脑节点',{exact:true}).inputValue(),'m1','an explicit offline target must never switch to another machine');
          assert.equal(await page.getByRole('button',{name:'发送给电脑 Agent'}).isDisabled(),true);
          offline = false;
          await page.getByRole('button',{name:'刷新节点与目录',exact:true}).click();
          await page.waitForFunction(() => !document.querySelector('[aria-label="发送给电脑 Agent"]').disabled);
          assert.equal(await page.getByLabel('电脑节点',{exact:true}).inputValue(),'m1');
          assert.equal(writes,0); assert.deepEqual(errors,[]); await page.close(); continue;
        } else if (scenario === 'target-links') {
          await page.getByLabel('项目',{exact:true}).selectOption('agent-fleet');
          assert.match(await page.getByRole('link',{name:'查看此节点会话',exact:true}).getAttribute('href'), /machine=m1/);
          assert.match(await page.getByRole('link',{name:'管理此节点',exact:true}).getAttribute('href'), /\/machine\/m1$/);
          await page.getByRole('link',{name:'查看此节点会话',exact:true}).click();
          await page.locator('.native-session-card').first().waitFor();
          assert.equal(await page.getByLabel('筛选电脑').inputValue(),'m1');
          assert.equal(writes,0); assert.deepEqual(errors,[]); await page.close(); continue;
        } else if (scenario === 'missing-targets') {
          await page.getByText('当前服务尚未提供节点启动目录。已有会话仍可打开和阅读。',{exact:true}).waitFor();
          assert.equal(await page.getByRole('button',{name:'发送给电脑 Agent'}).isDisabled(),true);
          assert.equal(writes,0);
        } else if (scenario === 'refresh-targets') {
          await page.getByText('暂无在线节点，请连接电脑后刷新节点与目录。', {exact:true}).waitFor();
          assert.equal(await page.getByRole('button',{name:'发送给电脑 Agent'}).isDisabled(),true);
          offline = false;
          await page.getByRole('button',{name:'刷新节点与目录',exact:true}).click();
          await page.waitForFunction(() => !document.querySelector('[aria-label="发送给电脑 Agent"]').disabled);
          await page.getByLabel('项目',{exact:true}).selectOption('agent-fleet');
          assert.equal(await page.getByLabel('电脑节点',{exact:true}).locator('option').count(),1, 'refresh must not duplicate nodes');
          assert.equal(writes,0, 'refresh only reads status and targets');
        } else {
          let rejectedTask = false;
          const taskRequests = [];
          if (scenario === 'task-recovery') {
            page.on('request', request => {if(request.method() === 'POST' && request.url().endsWith('/api/tasks')) taskRequests.push(request.postDataJSON());});
            await page.route('**/api/tasks', async route => {
              const response = await route.fetch();
              if (!rejectedTask) {rejectedTask = true; await route.abort('failed');}
              else await route.fulfill({response});
            });
          }
          await page.getByLabel('项目',{exact:true}).selectOption('agent-fleet');
          await page.getByLabel('给电脑 Agent 的消息').fill('Start a real native task');
          const sent = scenario === 'task-recovery' ? null : page.waitForResponse(response => response.request().method() === 'POST' && response.url().endsWith('/api/tasks'));
          await page.getByRole('button',{name:'发送给电脑 Agent'}).click();
          if (scenario === 'task-recovery') {
            await page.getByRole('button',{name:'查询或重试同一启动'}).waitFor();
            assert.equal(writes,1);
            await page.reload();
            await page.getByRole('button',{name:'查询或重试同一启动'}).waitFor();
            await page.getByRole('button',{name:'查询或重试同一启动'}).click();
            await page.waitForURL(/\/task\/[^?]+\?view=chat/);
            assert.equal(writes,2);
            assert.equal(taskRequests[0].client_token,taskRequests[1].client_token);
            assert.equal(taskRequests[0].instruction,taskRequests[1].instruction);
            assert.equal(taskRequests[0].machine,taskRequests[1].machine);
            await page.close(); continue;
          }
          await sent;
          await page.waitForURL(/\/task\/[^?]+\?view=chat/);
          assert.match(await page.locator('.native-delivery-card').innerText(), /启动请求已入队/);
          assert.doesNotMatch(await page.locator('.native-delivery-card').innerText(), /已交付到原会话/);
          const binding = await page.request.post(origin + '/_fixture/bind-task');
          assert.equal(binding.status(),200);
          await page.waitForURL(/\/session\/runner_fixture_[^?]+\?view=chat/,{timeout:16000});
          assert.equal(writes,1);
          await page.reload(); await page.locator('.native-chat-view').waitFor();
        }
      } else {
        if (scenario === 'local-profile' || scenario === 'profile-revocation') await page.route('**/api/machines/m1', async route => {
          const response = await route.fetch(), body = await response.json();
          body.current.local_profiles = [
            {profile_id:'local-a',family:'codex',label:'Primary provider',current:true},
            {profile_id:'local-b',family:'codex',label:'Alternate provider',current:false},
            {profile_id:'foreign',family:'claude_code',label:'Other Agent',current:false},
          ];
          await route.fulfill({response,json:body});
        });
        const sourceResponse = scenario === 'source-record' ? page.waitForResponse(response =>
          response.url().includes('/api/sessions/' + sid + '/events') &&
          response.url().includes('include_source=0')) : null;
        if (scenario === 'snapshot-revision') await page.request.post(origin + '/_fixture/native-event',{data:{reset_snapshot:true}});
        if (scenario === 'task-results-binding') {
          let release;
          const held = new Promise(resolve => {release=resolve;});
          await page.route('**/api/sessions/' + sid, async route => {
            const response = await route.fetch(); await held; await route.fulfill({response});
          },{times:1});
          await page.goto(origin + '/task/' + taskId + '?view=chat');
          if (width === 390) await page.getByRole('button',{name:'工作面板',exact:true}).click();
          await page.getByRole('tab',{name:'任务结果',exact:true}).click();
          await page.getByRole('button',{name:'查看原任务结果',exact:true}).click();
          const status = page.locator('.native-results > [role=status]');
          await status.filter({hasText:'原任务状态：已完成'}).waitFor();
          release();
          await page.getByText('Native answer from persisted transcript',{exact:true}).waitFor();
          assert.equal(await status.textContent(),'原任务状态：已完成','session detail must not invalidate a result already bound by the task response');
          assert.equal(writes,0); assert.deepEqual(errors,[]); await page.close(); continue;
        }
        if (scenario === 'native-stream-interruption') {
          const response = await page.request.post(origin + '/_fixture/native-event',{data:{phase:'start'}});
          assert.deepEqual((await response.json()).rejected,[]);
        }
        await page.goto(origin + '/session/' + sid + '?view=chat');
        if (scenario === 'native-stream-interruption') {
          const generation = width === 1440 ? 1 : 2;
          const message = page.locator('.assistant-message.assistant').filter({hasText:'LIVE_' + generation});
          const tool = page.locator('tool-call-badge[name="Read ' + generation + '"]');
          await message.waitFor(); await tool.waitFor();
          assert.equal(await message.locator('stream-markdown').getAttribute('is-streaming'),'');
          assert.equal(await tool.getAttribute('status'),'running');
          await tool.getByRole('button').click();
          const push = async phase => {
            const response = await page.request.post(origin + '/_fixture/native-event',{data:{phase}});
            assert.deepEqual((await response.json()).rejected,[]);
            await page.evaluate(() => window.dispatchEvent(new Event('focus')));
          };
          await push('delta');
          await message.locator('stream-markdown[content="LIVE_' + generation + ' 🌲 incremental"]').waitFor();
          assert.equal(await message.locator('stream-markdown').getAttribute('is-streaming'),'');
          assert.equal(await message.count(),1,'incremental text must update its existing native message');
          await push('fail');
          await message.getByText('输出已中断，已收到的内容保留。',{exact:true}).waitFor();
          assert.equal(await message.locator('stream-markdown').getAttribute('is-streaming'),null);
          assert.equal(await tool.getAttribute('status'),'unknown');
          assert.equal(await tool.getByRole('button').getAttribute('aria-expanded'),'true');
          await push('late-result');
          await tool.getByText('late <img src=x onerror=alert(1)>',{exact:true}).waitFor();
          assert.equal(await tool.getAttribute('status'),'success');
          assert.equal(await tool.locator('img').count(),0);
          assert.equal(await tool.getByRole('button').getAttribute('aria-expanded'),'true');
          await page.screenshot({path:'/tmp/fleet-native-web/native-stream-interruption-' + width + '.png',fullPage:true});
          assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),true);
          assert.equal(writes,0); assert.deepEqual(errors,[]); await page.close(); continue;
        }
        if (scenario === 'zcode-desktop-tools') {
          const notice = page.locator('.native-capture-notice');
          await notice.filter({hasText:'空闲后同步正文与工具记录'}).waitFor();
          assert.match(await notice.innerText(), /不代表完整历史/);
          const read = page.locator('tool-call-badge').filter({hasText:'Read · README.md'});
          await read.getByRole('button').click();
          await read.getByText('README <script>literal</script>',{exact:true}).waitFor();
          assert.match(await read.innerText(), /已完成/);
          assert.match(await read.getAttribute('args'), /file_path/);
          assert.equal(await page.locator('tool-call-badge').count(),width === 1440 ? 1 : 2);
          assert.equal(await read.locator('script').count(),0);
          const response = await page.request.post(origin + '/_fixture/native-event');
          assert.equal(response.status(),200);
          const bash = page.locator('tool-call-badge').filter({hasText:'Bash · npm test'});
          await bash.waitFor({timeout:16000});
          await bash.getByRole('button').click();
          await bash.getByText('Tests failed <img src=x onerror=alert(1)>',{exact:true}).waitFor();
          assert.match(await bash.innerText(), /失败/);
          assert.equal(await bash.locator('img').count(),0);
          assert.equal(await read.getByRole('button').getAttribute('aria-expanded'),'true');
          await page.request.post(origin + '/_fixture/native-event');
          await page.waitForTimeout(4500);
          assert.equal(await page.locator('tool-call-badge').count(),2,'replayed native tools remain unique');
          assert.equal(await page.getByText('PRIVATE_REASONING_SENTINEL',{exact:true}).count(),0);
          assert.equal(await page.locator('.native-capture-notice').count(),1);
          assert.equal(await notice.evaluate(node => {
            const rect = node.getBoundingClientRect();
            return rect.top >= 0 && rect.bottom <= innerHeight;
          }),true,'the sync explanation stays visible when the transcript follows new tool records');
          assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),true);
          await page.screenshot({path:'/tmp/fleet-native-web/' + scenario + '-' + width + '.png',fullPage:true});
          assert.equal(writes,0); assert.deepEqual(errors,[]); await page.close(); continue;
        }
        if (scenario === 'source-record') {
          const body = await (await sourceResponse).json();
          assert.equal(body.events.some(event => event.kind === 'source_record'), false);
          assert.equal(JSON.stringify(body).includes('PRIVATE_REASONING_SENTINEL'), false);
          assert.equal(await page.getByText('PRIVATE_REASONING_SENTINEL',{exact:true}).count(),0);
          assert.deepEqual(errors,[]); await page.close(); continue;
        }
        if (scenario === 'native-metadata') {
          await page.getByRole('heading',{name:'VM native title',exact:true}).waitFor();
          const context = page.locator('.native-context');
          const detail = await context.innerText();
          for (const fact of ['client-1.2','provider-y','model-x','原生开始','持续时间',
            '接入协议','已采集信息','历史完整度','会话正文','工具调用','工具结果',
            '原生会话名','尚未确认','操作权限以当前授权为准']) {
            assert.ok(detail.includes(fact), 'missing session detail fact: ' + fact + '\n' + detail);
          }
          await page.getByText('会话信息更新 · model-x / provider-y',{exact:true}).waitFor();
          assert.deepEqual(errors,[]); await page.close(); continue;
        }
        if (scenario === 'history-pagination') {
          await page.getByText('History page message 104',{exact:true}).waitFor();
          const loadOlder = page.getByRole('button',{name:'加载更早记录'});
          if (!await loadOlder.isVisible().catch(() => false)) throw new Error('tail page has no older-history action; notice=' + await page.locator('.native-history-notice').innerText() + ', button=' + await page.locator('.native-thread button').first().evaluate(node => node.outerHTML).catch(() => '' ) + ', messages=' + await page.locator('.assistant-message').count());
          await page.getByRole('button',{name:'加载更早记录'}).click();
          await page.getByText('History page message 0',{exact:true}).waitFor();
          assert.equal(await page.locator('.assistant-message').count() >= 105,true);
          assert.deepEqual(errors,[]); await page.close(); continue;
        }
        await page.getByText('Native answer from persisted transcript', {exact:true}).waitFor();
        if (['local-profile','stale-authority','control-confirmation','profile-revocation'].includes(scenario)) {
          if (scenario === 'stale-authority') await page.getByLabel('自定义回答').fill('preserve answer during reconnect');
          if (width === 390) await page.getByRole('button',{name:'工作面板',exact:true}).click();
          await page.getByRole('tab',{name:'操作',exact:true}).click();
          if (scenario === 'local-profile' || scenario === 'profile-revocation') {
            const picker = page.getByLabel('本机模型配置',{exact:true});
            await picker.selectOption('local-b');
            assert.equal(await picker.locator('option').count(),2,'profiles are scoped to the current Agent family');
            await page.waitForTimeout(4500);
            assert.equal(await picker.inputValue(),'local-b','read polling preserves unsent profile choice');
            if (scenario === 'profile-revocation') {
              await page.route('**/api/sessions/' + sid, async route => {
                const response = await route.fetch(), body = await response.json();
                body.session.adoption_status = 'revoked';
                body.session.allowed_actions = []; body.session.control_capability = 'unavailable';
                await route.fulfill({response,json:body});
              });
              await page.evaluate(() => window.dispatchEvent(new Event('focus')));
              await page.locator('.native-access').filter({hasText:'纳管已撤销'}).waitFor();
              assert.equal(await picker.isVisible(),false);
              assert.equal(await page.getByRole('button',{name:'暂停会话',exact:true}).count(),0);
              assert.equal(writes,0); assert.deepEqual(errors,[]); await page.close(); continue;
            }
            const posted = page.waitForRequest(request => request.method() === 'POST' && request.url().endsWith('/source/control'));
            await page.getByRole('button',{name:'应用本机配置',exact:true}).click();
            const request = await posted;
            assert.deepEqual(request.postDataJSON(), {action:'apply_local_profile',reason_code:'operator_requested',payload:{profile_id:'local-b'}});
            await page.locator('.native-operation-feedback').filter({hasText:'已入队'}).waitFor();
            assert.equal(writes,1);
          } else if (scenario === 'control-confirmation') {
            await page.getByRole('button',{name:'终止会话',exact:true}).click();
            const dialog = page.getByRole('dialog',{name:'确认会话控制',exact:true});
            await dialog.getByRole('button',{name:'取消',exact:true}).click();
            assert.equal(writes,0,'cancel does not enqueue control');
            await page.getByRole('button',{name:'终止会话',exact:true}).click();
            await page.keyboard.press('Escape');
            assert.equal(await dialog.isVisible(),false,'Escape closes the confirmation only');
            assert.equal(await page.locator('.assistant-inspector').isVisible(),true,'Escape must preserve the inspector');
            assert.equal(writes,0);
            await page.getByRole('button',{name:'终止会话',exact:true}).click();
            const posted = page.waitForRequest(request => request.method() === 'POST' && request.url().endsWith('/source/control'));
            await dialog.getByRole('button',{name:'确认执行',exact:true}).click();
            assert.deepEqual((await posted).postDataJSON(),{action:'terminate_session',reason_code:'operator_requested'});
            await page.locator('.native-operation-feedback').filter({hasText:'已入队'}).waitFor();
            assert.equal(writes,1);
            assert.equal(await page.getByRole('button',{name:'终止会话',exact:true}).isDisabled(),true);
          } else {
            await page.getByRole('button',{name:'暂停会话',exact:true}).waitFor();
            await page.route('**/api/sessions/' + sid, route => route.fulfill({status:503,json:{error:'temporarily_unavailable'}}));
            await page.evaluate(() => window.dispatchEvent(new Event('focus')));
            await page.getByText('权限状态暂不可确认，请重新连接后再操作。',{exact:true}).waitFor();
            assert.equal(await page.getByRole('button',{name:'暂停会话',exact:true}).isDisabled(),true);
            if (width === 390) await page.getByRole('button',{name:'收起工作面板',exact:true}).click();
            assert.equal(await page.getByRole('button',{name:'发送给电脑 Agent'}).isDisabled(),true);
            assert.equal(await page.getByLabel('自定义回答').isDisabled(),true,'authority failure also disables question answers');
            assert.equal(await page.getByLabel('自定义回答').inputValue(),'preserve answer during reconnect');
            assert.equal(writes,0);
          }
          await page.screenshot({path:'/tmp/fleet-native-web/' + scenario + '-' + width + '.png',fullPage:true});
          assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false,'no operation panel overflow');
          assert.deepEqual(errors,[]); await page.close(); continue;
        }
        if (scenario === 'continuation-results') {
          let turnReads = 0, patchReads = 0;
          page.on('request', request => {
            if (/\/results(?:\?|$)/.test(request.url())) turnReads++;
            if (/\/results\/cmd_/.test(request.url())) patchReads++;
          });
          if (width < 1100) await page.getByRole('button',{name:'工作面板',exact:true}).click();
          await page.getByRole('tab',{name:'任务结果',exact:true}).click();
          assert.equal(turnReads,0, 'turn results remain on demand');
          await page.getByRole('button',{name:'查看续聊成果',exact:true}).click();
          const body = page.locator('.native-turn-result-body');
          await body.locator(':scope > details').first().waitFor();
          assert.equal(await body.locator(':scope > details').count(),20);
          assert.equal(patchReads,0, 'directory never downloads patch bodies');
          await page.getByRole('button',{name:'加载更早成果',exact:true}).click();
          await page.waitForFunction(() => document.querySelectorAll('.native-turn-result-body > details').length === 23);
          assert.equal(await page.getByRole('button',{name:'加载更早成果',exact:true}).isVisible(),false);
          assert.equal(await body.getByText('cmd_turn_00',{exact:true}).count(),1);
          const first = body.locator(':scope > details').first();
          await first.locator(':scope > summary').click();
          await first.getByRole('button',{name:'读取本轮详情',exact:true}).click();
          await first.getByText('本轮 diff',{exact:true}).click();
          assert.ok((await first.innerText()).includes('<img src=x onerror=alert(1)> TURN_DETAIL'));
          assert.equal(await body.locator('img,script').count(),0);
          assert.equal(patchReads,1);
          await page.evaluate(async () => {
            const {mountTurnResults} = await import('/assets/views/agent-chat/turn-results.js');
            const host = document.createElement('div'); document.body.append(host);
            const pending = [], settle = () => new Promise(resolve => setTimeout(resolve,0));
            const row = {command_id:'cmd_fixture',execution_state:'succeeded',duration_s:1,finished_at:'2026-11-23T12:00:00Z'};
            const ui = mountTurnResults(host,{
              readContinuationResults:async () => ({results:[row],has_more:false,before_cursor:'1'}),
              readContinuationResult:(_sid,_cid,signal) => new Promise(resolve => pending.push({signal,resolve})),
            });
            ui.update('s'); host.querySelector('button').click(); await settle();
            const action = host.querySelector('.native-turn-result-body button');
            action.click(); ui.suspend();
            if (!pending[0].signal.aborted || action.disabled) throw new Error('turn detail not aborted and reenabled');
            ui.resume(); action.click();
            pending[0].resolve({collection_state:'complete',diff_patch:'STALE_TURN'}); await settle();
            if (!action.disabled || host.textContent.includes('STALE_TURN')) throw new Error('old turn request altered resumed request');
            pending[1].resolve({collection_state:'complete',diff_patch:'CURRENT_TURN'}); await settle();
            if (action.disabled || !host.textContent.includes('CURRENT_TURN')) throw new Error('current turn request not published');
            ui.dispose(); host.remove();
          });
          assert.equal(writes,0); assert.deepEqual(errors,[]); await page.close(); continue;
        }
        if (scenario === 'results' || scenario === 'result-file-concurrency') {
          let resultGets = 0;
          page.on('request', request => {if (/\/api\/tasks\/.+\/(files|diff)/.test(request.url())) resultGets++;});
          if (width < 1100) await page.getByRole('button',{name:'工作面板',exact:true}).click();
          await page.getByRole('tab',{name:'任务结果',exact:true}).click();
          assert.equal(resultGets,0,'result snapshots are fetched only on request');
          await page.getByRole('button',{name:'查看原任务结果',exact:true}).click();
          await page.getByText('pytest · 通过 12 · 失败 0 · 跳过 1 · 错误 0 · 1.2 秒',{exact:true}).waitFor();
          if (scenario === 'result-file-concurrency') {
            let release;
            const held = new Promise(resolve => {release=resolve;});
            await page.route('**/api/tasks/*/files/test.py', async route => {
              const response = await route.fetch();
              await held; await route.fulfill({response}).catch(() => {});
            });
            const first = page.locator('.native-result-files details').filter({has:page.locator('summary',{hasText:/^test.py/})});
            const second = page.locator('.native-result-files details').filter({has:page.locator('summary',{hasText:/^other.py/})});
            await first.locator('summary').click();
            const requested = page.waitForRequest('**/api/tasks/*/files/test.py');
            await first.getByRole('button',{name:'读取文件快照',exact:true}).click();
            await requested;
            await second.locator('summary').click();
            await second.getByRole('button',{name:'读取文件快照',exact:true}).click();
            await second.locator('pre').waitFor();
            release();
            await first.locator('pre').waitFor();
            assert.equal(await first.locator('pre').innerText(),'print("<img src=x onerror=alert(1)>")\n');
            assert.equal(await second.locator('pre').innerText(),'second retained snapshot\n');
            await page.evaluate(async () => {
              const {mountNativeResults} = await import('/assets/views/agent-chat/results.js');
              const host = document.createElement('div'); document.body.append(host);
              const pending = [];
              const settle = () => new Promise(resolve => setTimeout(resolve,0));
              const ui = mountNativeResults(host,{
                readResults:async () => ({task:{state:'succeeded'},files:[{path:'fixture.py'}]}),
                readResultFile:(_id,_sid,_path,signal) => new Promise(resolve => pending.push({signal,resolve})),
              });
              ui.update('t','s'); host.querySelector('button').click(); await settle();
              const action = host.querySelector('.native-result-files button');
              action.click(); ui.suspend();
              if (!pending[0].signal.aborted || action.disabled) throw new Error('file read was not cancelled on suspension');
              ui.resume(); action.click();
              pending[0].resolve({content:'STALE_FILE'}); await settle();
              if (!action.disabled || host.textContent.includes('STALE_FILE')) throw new Error('late read changed the resumed file request');
              pending[1].resolve({content:'CURRENT_FILE'}); await settle();
              if (action.disabled || !host.textContent.includes('CURRENT_FILE')) throw new Error('resumed file read did not publish');
              action.click(); ui.update('new','new-session');
              if (!pending[2].signal.aborted) throw new Error('identity change did not abort file read');
              pending[2].resolve({content:'WRONG_SESSION_FILE'}); await settle();
              if (host.textContent.includes('WRONG_SESSION_FILE')) throw new Error('file published to a different session');
              ui.dispose(); host.remove();
            });
            assert.equal(writes,0); assert.deepEqual(errors,[]); await page.close(); continue;
          }
          await page.getByText('保留的 diff',{exact:true}).click();
          assert.ok((await page.locator('.native-result-body').innerText()).includes('<img src=x onerror=alert(1)>'));
          assert.equal(await page.locator('.native-result-body img,.native-result-body script').count(),0);
          const file = page.locator('.native-result-files details').filter({has:page.locator('summary',{hasText:/^test.py/})});
          await file.locator('summary').click();
          await file.getByRole('button',{name:'读取文件快照',exact:true}).click();
          await file.locator('pre').waitFor();
          assert.equal(await file.locator('pre').innerText(),'print("<img src=x onerror=alert(1)>")\n');
          assert.equal(await page.locator('.native-result-body img,.native-result-body script').count(),0);
          assert.equal(writes,0);
          assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'result panel must fit narrow screens');
          // Exercise lifecycle with an actual module and delayed service: abort
          // plus generation guard must prevent a late result from painting.
          await page.evaluate(async () => {
            const {mountNativeResults} = await import('/assets/views/agent-chat/results.js');
            const host = document.createElement('div'); document.body.append(host);
            let release, signal;
            const pending = new Promise(resolve => {release=resolve;});
            const ui = mountNativeResults(host,{readResults:async (_id,_sid,s) => {signal=s;return pending;}});
            ui.update('old','old-session'); host.querySelector('button').click();
            ui.suspend(); if (!signal.aborted) throw new Error('suspend did not abort');
            ui.resume(); ui.update('new','new-session');
            release({task:{state:'succeeded',result:{log_summary:'STALE_RESULT'}},files:[]});
            await new Promise(resolve => setTimeout(resolve,0));
            if (host.textContent.includes('STALE_RESULT')) throw new Error('stale result painted');
            ui.dispose(); host.remove();
          });
          await page.screenshot({path:'/tmp/fleet-native-web/results-' + width + '.png',fullPage:true});
          assert.deepEqual(errors,[]); await page.close(); continue;
        }
        if (scenario === 'snapshot-revision') {
          await page.getByText('查看完整原文',{exact:true}).click();
          await page.request.post(origin + '/_fixture/native-event');
          await page.waitForFunction(() => document.querySelectorAll('.assistant-message.assistant').length === 2 &&
            document.querySelectorAll('.assistant-message.assistant')[1].querySelector('stream-markdown')?.getAttribute('content') === 'Live native update',null,{timeout:12000});
          assert.equal(await page.locator('.native-full-text').count(),0,'final short snapshot must remove the superseded full-text disclosure');
          assert.equal((await page.locator('.native-chat-view').textContent()).includes('OLD_SNAPSHOT_'),false,'superseded content must leave the DOM');
          assert.equal(writes,0); assert.deepEqual(errors,[]); await page.close(); continue;
        }
        if (scenario === 'long-message') {
          await page.getByText('查看完整原文',{exact:true}).click();
          const full = page.locator('.native-full-text');
          await page.request.post(origin + '/_fixture/native-event');
          await page.waitForFunction(() => document.querySelector('.native-full-text pre')?.textContent.endsWith('Live native update'));
          assert.equal(await full.evaluate(node => node.open),true, 'stream update must keep full-text disclosure open');
          assert.equal(await full.locator('pre').evaluate(node => node.textContent.length),33000 + 'Live native update'.length);
          assert.deepEqual(errors,[]); await page.close(); continue;
        }
        if (scenario === 'revoked') {
          assert.equal(await page.getByRole('button',{name:'发送给电脑 Agent'}).isDisabled(),true);
          assert.equal(await page.getByRole('button',{name:'暂停会话',exact:true}).count(),0);
          assert.equal(writes,0);
          await page.screenshot({path:'/tmp/fleet-native-web/' + scenario + '-' + width + '.png',fullPage:true});
          assert.deepEqual(errors,[]); await page.close(); continue;
        }
        if (scenario === 'question-draft') {
          const answer = page.getByLabel('自定义回答');
          await answer.fill('retain native question draft');
          await answer.evaluate(node => {window.originalNativeQuestion = node; node.focus(); node.setSelectionRange(3,9);});
          await page.waitForTimeout(4500);
          assert.equal(await answer.evaluate(node => node === window.originalNativeQuestion && document.activeElement === node && node.selectionStart === 3 && node.selectionEnd === 9),true,'read refresh must preserve question DOM, focus and selection');
          assert.equal(writes,0); assert.deepEqual(errors,[]); await page.close(); continue;
        }
        await page.waitForFunction(() => !document.querySelector('[aria-label="发送给电脑 Agent"]').disabled);
        if (scenario === 'delivery-disclosure') await page.route('**/api/supervisor/receipts/*', async route => {
          const response = await route.fetch(), body = await response.json();
          body.receipt.status = 'succeeded';
          await route.fulfill({response, json:body});
        });
        await page.locator('tool-call-badge').getByRole('button').click();
        await page.getByText('README contents', {exact:true}).waitFor();
        let release;
        const held = new Promise(resolve => {release=resolve;});
        let rejectedFirst = false;
        const messageRequests = [];
        page.on('request', request => {
          if (request.method() === 'POST' && request.url().endsWith('/api/sessions/' + sid + '/messages')) messageRequests.push(request.postDataJSON());
        });
        await page.route('**/api/sessions/' + sid + '/messages', async route => {
          const response = await route.fetch();
          await held;
          if (['lost-response', 'reload-recovery'].includes(scenario) && !rejectedFirst) { rejectedFirst = true; await route.abort('failed'); }
          else await route.fulfill({response});
        });
        await page.getByLabel('给电脑 Agent 的消息').fill('继续');
        await page.getByRole('button',{name:'发送给电脑 Agent'}).click();
        await page.locator('.native-delivery-text').getByText('继续',{exact:true}).waitFor();
        assert.match(await page.locator('.native-delivery-card').innerText(), /正在发送/);
        release();
        if (scenario === 'lost-response' || scenario === 'reload-recovery') {
          if (scenario === 'reload-recovery') {
            assert.equal(await page.evaluate(() => Object.keys(sessionStorage).some(key => key.includes('fleet-native-message'))), true,
              'uncertain message operation must persist before the response is lost');
            await page.reload();
            await page.getByText('Native answer from persisted transcript', {exact:true}).waitFor();
          }
          const recover = page.getByRole('button',{name:'恢复原发送'});
          if (scenario === 'reload-recovery' && !await recover.isVisible().catch(() => false)) {
            throw new Error('recovery card missing after reload; journal=' + await page.evaluate(() => JSON.stringify(Object.keys(sessionStorage).map(key => [key, sessionStorage.getItem(key)]))) + '; pending=' + await page.locator('.native-pending').innerText().catch(() => 'missing'));
          }
          await recover.waitFor();
          assert.equal(writes,1,'unknown write must not retry before explicit recovery');
          await recover.click();
          await page.waitForFunction(() => document.querySelector('.native-delivery-card')?.textContent.includes('已入队'));
          assert.equal(writes,2,'recovery is an explicit replay of the same operation');
          assert.equal(messageRequests[0].client_token, messageRequests[1].client_token);
          assert.equal(messageRequests[0].text, messageRequests[1].text);
        } else {
          if (scenario === 'receipt-failure' || scenario === 'cleanup-receipt') {
            await page.waitForFunction(() => document.querySelector('.native-delivery-card').textContent.includes('已入队'));
            const failed = await page.request.post(origin + '/_fixture/fail-command');
            assert.ok((await failed.json()).commands > 0,'fixture records a real signed command failure');
            await page.evaluate(() => window.dispatchEvent(new Event('focus')));
            await page.waitForFunction(() => document.querySelector('.native-delivery-card').textContent.includes('投递失败'));
            const reason = scenario === 'cleanup-receipt' ? 'escape_unverified' : 'native_session_unbound';
            assert.ok((await page.locator('.native-operation-feedback').textContent()).includes(reason),'node failure reason must reach the operation feedback');
            assert.ok((await page.locator('.native-delivery-card').textContent()).includes(reason),'failure reason must reach the delivery card');
            if (scenario === 'cleanup-receipt') {
              assert.match(await page.locator('.native-operation-feedback').textContent(), /尚未确认原 Agent 进程已完全退出/);
              assert.match(await page.locator('.native-delivery-card').textContent(), /以节点回执确认停止/);
            }
            await page.waitForFunction(() => !document.querySelector('[aria-label="发送给电脑 Agent"]').disabled);
            assert.equal(writes,1,'failed delivery must not retry automatically');
            assert.deepEqual(errors,[]); await page.close(); continue;
          }
          if (scenario === 'delivery-disclosure') {
            const detail = page.locator('details.native-delivery-card');
            await page.waitForFunction(() => document.querySelector('details.native-delivery-card'));
            await detail.evaluate(node => {node.open = true; window.nativeReceiptDisclosure = node;});
            await page.request.post(origin + '/_fixture/native-event');
            await page.getByText('Live native update',{exact:true}).waitFor({timeout:12000});
            assert.equal(await detail.evaluate(node => node === window.nativeReceiptDisclosure && node.open),true, 'read polling must preserve receipt disclosure identity and open state');
            assert.equal(writes,1); assert.deepEqual(errors,[]); await page.close(); continue;
          }
          await page.waitForFunction(() => document.querySelector('.native-delivery-card').textContent.includes('已入队'));
          await page.request.post(origin + '/_fixture/native-event');
          await page.getByText('Live native update',{exact:true}).waitFor({timeout:12000});
          await page.evaluate(() => window.dispatchEvent(new CustomEvent('fleet:navigate',{detail:{path:'/settings/general'}})));
          await page.getByRole('button',{name:'关闭设置',exact:true}).click();
          await page.waitForURL('**/session/' + sid + '?view=chat');
          assert.equal(writes,1,'settings overlay return must not resubmit');
          if(width===390) {
            await page.getByRole('button',{name:'工作面板',exact:true}).click();
            await page.evaluate(() => window.dispatchEvent(new CustomEvent('fleet:navigate',{detail:{path:'/assistant?view=sessions'}})));
            await page.locator('.native-directory').waitFor();
            assert.equal(await page.locator('dialog:modal').count(),0,'suspended chat cannot leave the document modal');
            await page.evaluate(sid => window.dispatchEvent(new CustomEvent('fleet:navigate',{detail:{path:'/session/' + sid + '?view=chat'}})),sid);
            await page.getByRole('button',{name:'收起工作面板',exact:true}).click();
          }
        }
      }
      await page.screenshot({path:'/tmp/fleet-native-web/' + scenario + '-' + width + '.png',fullPage:true});
      assert.deepEqual(errors,[]);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false,'no horizontal document overflow');
      await page.close();
    }
  } finally {await browser.close();}
})().catch(error => {console.error(error);process.exit(1);});
