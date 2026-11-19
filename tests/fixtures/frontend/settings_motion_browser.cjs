const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const [origin, viewport, scenario] = process.argv.slice(2);
(async () => {
  const browser = await chromium.launch({channel: process.env.FLEET_BROWSER_CHANNEL || 'chrome', headless: true});
  try {
    const context = await browser.newContext({viewport: viewport === 'mobile' ? {width:390,height:844} : {width:1440,height:1100}});
    const login = await context.request.post(origin + '/api/accounts/login', {data:{email:'admin@example.test',password:'safe-password-123!'},headers:{Origin:origin}});
    assert.equal(login.status(),200);
    const page = await context.newPage();
    const errors = []; page.on('pageerror', e => errors.push(e.message));
    await page.goto(origin + '/assistant');
    await page.getByText('请先配置可用模型和工作区', {exact:true}).waitFor();
    const navigate = path => page.evaluate(path => window.dispatchEvent(new CustomEvent('fleet:navigate',{detail:{path}})), path);
    await navigate('/settings/models');
    const dialog = page.locator('.settings-dialog[open]');
    await dialog.waitFor();
    if (scenario === 'interrupted-opening') {
      const samples = await dialog.evaluate(node => {
        node.getBoundingClientRect();
        for (const animation of node.getAnimations()) {
          animation.pause(); animation.currentTime = 50;
        }
        const before = {opacity: getComputedStyle(node).opacity, transform: getComputedStyle(node).transform};
        node.querySelector('.settings-close').click();
        const after = {opacity: getComputedStyle(node).opacity, transform: getComputedStyle(node).transform};
        return {before, after};
      });
      assert.equal(samples.after.opacity, samples.before.opacity, 'closing during entry must not jump opacity');
      assert.equal(samples.after.transform, samples.before.transform, 'closing during entry must not jump geometry');
      await dialog.waitFor({state:'detached'});
      assert.equal(await page.locator('.assistant-view').isVisible(),true);
    } else {
      for (const section of ['appearance', 'models', 'runtime', 'appearance']) {
        await navigate('/settings/' + section);
        const motion = await dialog.evaluate(node => ({
          animation: getComputedStyle(node).animationName,
          backdrop: getComputedStyle(node,'::backdrop').animationName,
          opacity: getComputedStyle(node).opacity,
        }));
        assert.equal(motion.animation,'none','category navigation must not replay the dialog entrance');
        assert.equal(motion.backdrop,'none','category navigation must not flash the background overlay');
        assert.equal(motion.opacity,'1');
      }
      await page.keyboard.press('Escape');
      await dialog.waitFor({state:'detached'});
      await navigate('/settings/appearance');
      assert.equal(await dialog.evaluate(node => getComputedStyle(node).animationName),'settings-dialog-in','reopening cached settings should animate');
      await page.emulateMedia({colorScheme:'dark'});
      await page.locator('html[data-resolved-theme="dark"]').waitFor({state:'attached'});
      assert.equal(await dialog.evaluate(node => getComputedStyle(node,'::backdrop').backgroundColor),'rgba(0, 0, 0, 0.44)');
      await page.emulateMedia({reducedMotion:'reduce'});
      await page.keyboard.press('Escape');
      await dialog.waitFor({state:'detached'});
      await navigate('/settings/models');
      assert.equal(await dialog.evaluate(node => getComputedStyle(node).animationDuration),'1e-05s');
      // Navigating away while exit is pending must cancel the old close callback.
      await dialog.evaluate(node => node.querySelector('.settings-close').click());
      await navigate('/settings/account');
      await page.waitForTimeout(250);
      assert.equal(new URL(page.url()).pathname,'/settings/account');
      assert.equal(await dialog.count(),1);
    }
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({viewport,scenario,pageErrors:errors}));
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
