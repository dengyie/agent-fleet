const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const [origin, provider, failure] = process.argv.slice(2);
(async () => {
  const browser = await chromium.launch({channel: process.env.FLEET_BROWSER_CHANNEL || 'chrome', headless: true});
  try {
    const page = await browser.newPage(); page.setDefaultTimeout(7000);
    const errors = [], loads = [], sends = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => { if (request.url().endsWith('/api/accounts/code')) sends.push(request.postDataJSON()); });
    const held = new Map();
    await page.route(/^https:\/\/(?:js\.hcaptcha\.com|challenges\.cloudflare\.com)\//, route => {
      const name = route.request().url().includes('hcaptcha.com') ? 'hcaptcha' : 'turnstile';
      loads.push(name); held.set(name, route);
    });
    const release = async name => {
      await page.waitForFunction(name => document.querySelector(`script[src*="${name === 'hcaptcha' ? 'js.hcaptcha.com' : 'challenges.cloudflare.com'}"]`), name);
      // The provider stand-in uses the real render/getResponse/reset/remove contract.
      await held.get(name).fulfill({contentType: 'text/javascript', body: `
        window.${name} = {
          render(box, options) { box.dataset.provider = '${name}'; box.textContent = options.sitekey;
            window['${name}Renders'] = (window['${name}Renders'] || 0) + 1; return 0; },
          getResponse(id) { if (id !== 0) throw Error('wrong widget'); return '${name}-solved'; },
          reset(id) { if (id !== 0) throw Error('wrong reset');
            window['${name}Resets'] = (window['${name}Resets'] || 0) + 1; },
          remove(id) { if (id !== 0) throw Error('wrong remove');
            window['${name}Removes'] = (window['${name}Removes'] || 0) + 1; }
        };`});
      held.delete(name);
    };
    await page.goto(origin + '/login', {waitUntil: 'domcontentloaded'});
    await page.getByRole('button', {name: '注册账号', exact: true}).waitFor();
    assert.deepEqual(loads, [], 'plain login must not request optional captcha providers');
    await page.screenshot({timeout: 2000});
    await page.getByRole('button', {name: '注册账号', exact: true}).click();
    const hasTurnstile = ['turnstile', 'both'].includes(provider);
    const hasHcaptcha = ['hcaptcha', 'both'].includes(provider);
    if (hasTurnstile) {
      // Leaving while the script is pending must not render into the next form.
      await page.waitForFunction(() => document.querySelector('script[src*="challenges.cloudflare.com"]'));
      await page.getByRole('button', {name: '返回登录', exact: true}).click();
      await release('turnstile');
      await page.waitForFunction(() => window.turnstile);
      assert.equal(await page.locator('[data-provider]').count(), 0);
      assert.equal(await page.evaluate(() => window.turnstileRenders || 0), 0);
      await page.getByRole('button', {name: '注册账号', exact: true}).click();
      await page.locator('[data-provider="turnstile"]').waitFor();
      await page.evaluate(() => { window.turnstile.getResponse = () => ''; });
      await page.getByLabel('邮箱', {exact: true}).fill('unsolved@example.test');
      await page.getByRole('button', {name: '发送验证码', exact: true}).click();
      await page.getByText('请先完成人机验证。', {exact: true}).waitFor();
      assert.equal(sends.length, 0);
      assert.equal(await page.evaluate(() => window.turnstileResets || 0), 0);
      await page.evaluate(() => { window.turnstile.getResponse = () => 'turnstile-solved'; });
    }
    const send = page.getByRole('button', {name: '发送验证码', exact: true});
    for (let attempt = 0; attempt < 3; attempt++) {
      await page.getByLabel('邮箱', {exact: true}).fill(`captcha${attempt}@example.test`);
      const response = page.waitForResponse(origin + '/api/accounts/code');
      await send.click(); assert.equal((await response).status(), 200);
      await page.getByText('验证码请求已受理。请查看邮箱；若未收到，请稍后重试或联系管理员。', {exact: true}).waitFor();
    }
    if (hasHcaptcha) {
      await page.waitForFunction(() => document.querySelector('script[src*="js.hcaptcha.com"]'));
      if (failure === 'timeout') await page.getByRole('button', {name: '重新加载人机验证', exact: true}).waitFor({timeout: 12000});
      await held.get('hcaptcha').abort('failed'); held.delete('hcaptcha');
      await page.getByRole('button', {name: '重新加载人机验证', exact: true}).click();
      await release('hcaptcha');
      await page.locator('[data-provider="hcaptcha"]').waitFor();
      await page.getByLabel('邮箱', {exact: true}).fill('captcha3@example.test');
      const response = page.waitForResponse(origin + '/api/accounts/code');
      await send.click(); assert.equal((await response).status(), 200);
      assert.equal(sends.at(-1).hcaptcha_response, 'hcaptcha-solved');
    }
    if (hasTurnstile) assert.ok(sends.every(data => data.turnstile_response === 'turnstile-solved'));
    assert.equal(loads.filter(name => name === 'turnstile').length, hasTurnstile ? 1 : 0);
    assert.equal(loads.filter(name => name === 'hcaptcha').length, hasHcaptcha ? 2 : 0);
    await page.getByRole('button', {name: '返回登录', exact: true}).click();
    assert.equal(await page.locator('[data-provider]').count(), 0);
    for (const name of ['hcaptcha', 'turnstile']) {
      if (provider === name || provider === 'both') assert.equal(await page.evaluate(name => window[name + 'Removes'], name), 1);
    }
    if (process.env.FLEET_SCREENSHOTS) {
      await fs.mkdir(process.env.FLEET_SCREENSHOTS, {recursive: true});
      await page.screenshot({path: process.env.FLEET_SCREENSHOTS + '/captcha-' + provider + '-' + failure + '.png'});
    }
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
