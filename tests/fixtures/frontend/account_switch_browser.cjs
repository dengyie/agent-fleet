const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
(async () => {
 const origin = process.argv[2];
 const browser = await chromium.launch({channel: process.env.FLEET_BROWSER_CHANNEL || 'chrome', headless:true});
 try {
  const scenarios = ['focus', 'pageshow', 'timer'].map(trigger => ({trigger, from: 'admin@example.test', to: 'user@example.test'}));
  scenarios.push({trigger: 'focus', from: 'user@example.test', to: 'user2@example.test'});
  for (const {trigger, from, to} of scenarios) {
   const context = await browser.newContext();
   // Drive the real timer callback on demand, without a minute-long sleep.
   await context.addInitScript(() => {
    const setInterval = window.setInterval.bind(window);
    window.setInterval = (fn, delay, ...args) => {
     if (delay === 60000) window.runSessionTimer = fn;
     return setInterval(fn, delay, ...args);
    };
   });
   const page = await context.newPage(); page.setDefaultTimeout(5000);
   await page.goto(origin + '/account');
   await page.getByLabel('邮箱', {exact:true}).fill(from);
   await page.getByLabel('密码', {exact:true}).fill('Review-password-123!');
   await page.getByRole('button', {name:'登录', exact:true}).click();
   await page.getByRole('heading', {name:'个人资料', exact:true}).waitFor();
   await page.waitForFunction(email => document.querySelector('#route-view').textContent.includes(email), from);
   await context.request.post(origin + '/api/accounts/logout', {data:{}, headers:{Origin:origin}});
   await context.request.post(origin + '/api/accounts/login', {data:{email:to, password:'Review-password-123!'}, headers:{Origin:origin}});
   let checks = 0;
   page.on('response', r => { if (r.url() === origin + '/api/operator/session') checks++; });
   await page.evaluate(async trigger => {
    if (trigger === 'timer') await window.runSessionTimer();
    else if (trigger === 'pageshow') window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}));
    else window.dispatchEvent(new Event('focus'));
   }, trigger);
   await page.waitForFunction(({from, to}) => {
    const root = document.querySelector('#route-view');
    return root && root.textContent.includes(to) && !root.textContent.includes(from);
   }, {from, to}, {timeout:5000});
   assert.ok(checks > 0);
   assert.equal(await page.getByRole('heading', {name:'账号管理', exact:true}).count(), 0);
   await context.close();
  }
  const context = await browser.newContext();
  const page = await context.newPage(); page.setDefaultTimeout(5000);
  await page.goto(origin + '/account');
  await page.getByLabel('邮箱', {exact:true}).fill('admin@example.test');
  await page.getByLabel('密码', {exact:true}).fill('Review-password-123!');
  await page.getByRole('button', {name:'登录', exact:true}).click();
  const row = page.locator('.account-row').filter({hasText:'user2@example.test'});
  await row.waitFor();
  const users = await (await context.request.get(origin + '/api/accounts/users')).json();
  const target = users.users.find(user => user.email === 'user2@example.test');
  await context.request.post(origin + '/api/accounts/users/' + target.id, {
   data:{active:false, revision:target.revision}, headers:{Origin:origin},
  });
  page.once('dialog', dialog => dialog.accept());
  const [sent] = await Promise.all([
   page.waitForRequest(request => request.url().endsWith('/users/' + target.id) && request.method() === 'POST'),
   row.getByRole('button', {name:'设为管理员', exact:true}).click(),
  ]);
  assert.deepEqual(sent.postDataJSON(), {revision:target.revision, role:'admin'});
  await page.getByText('账号信息已被修改，请查看最新状态后重试', {exact:true}).waitFor();
  await row.getByText('user2@example.test · 用户 · 已停用', {exact:true}).waitFor();
  page.once('dialog', dialog => dialog.accept());
  await row.getByRole('button', {name:'设为管理员', exact:true}).click();
  await row.getByText('user2@example.test · 管理员 · 已停用', {exact:true}).waitFor();
  await context.close();
  console.log('PASS: identity switching, partial admin updates and stale-revision recovery');
 } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
