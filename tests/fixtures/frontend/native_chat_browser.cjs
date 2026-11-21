const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const fs = require('node:fs');
const [origin, scenario, sid] = process.argv.slice(2);
(async () => {
  const browser = await chromium.launch({channel:process.env.FLEET_BROWSER_CHANNEL || 'chrome', headless:true});
  fs.mkdirSync('/tmp/fleet-native-web', {recursive:true});
  try {
    for (const width of [1440,390]) {
      const page = await browser.newPage({viewport:{width,height:1000}});
      page.setDefaultTimeout(9000);
      const errors = []; page.on('pageerror', error => errors.push(error.message));
      let writes = 0;
      page.on('request', request => {if(request.method() === 'POST' && /\/messages$|\/control$|\/api\/tasks$/.test(request.url())) writes++;});
      await page.route('**/api/operator/session', async route => {
        const response = await route.fetch(), body = await response.json();
        await route.fulfill({response, json:{...body, authenticated:true, user:{id:'browser-owner',role:'admin',name:'Browser Owner'}}});
      });
      if (scenario === 'directory' || scenario === 'directory-pagination') {
        await page.goto(origin + '/assistant?view=sessions');
        await page.locator('.native-session-card').first().waitFor();
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
        await page.locator('.native-session-card').first().click();
        await page.locator('.native-chat-view').waitFor();
        await page.getByText('Native answer from persisted transcript', {exact:true}).waitFor();
      } else if (['create', 'task-recovery', 'missing-targets', 'refresh-targets'].includes(scenario)) {
        let offline = true;
        if (scenario === 'missing-targets') await page.route('**/api/machines/*/task-targets', route =>
          route.fulfill({status:404,contentType:'application/json',body:JSON.stringify({error:'not_found'})}));
        if (scenario === 'refresh-targets') await page.route('**/api/status', async route => {
          const response = await route.fetch(), body = await response.json();
          if (offline) for (const machine of body.machines) machine.online = false;
          await route.fulfill({response, json: body});
        });
        await page.goto(origin + '/assistant?view=computer');
        if (scenario === 'missing-targets') {
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
        await page.goto(origin + '/session/' + sid + '?view=chat');
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
