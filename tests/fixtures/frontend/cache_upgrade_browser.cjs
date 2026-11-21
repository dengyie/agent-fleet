const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert = require('node:assert/strict');
const [origin, expected] = process.argv.slice(2);

(async () => {
  const browser = await chromium.launch({channel: process.env.FLEET_BROWSER_CHANNEL || 'chrome', headless:true});
  try {
    const context = await browser.newContext();
    const errors = [];
    async function inspect(version) {
      const page = await context.newPage();
      page.on('pageerror', error => errors.push(error.message));
      // New document in the same context: do not route requests, clear caches,
      // force reload or create a clean context between the two releases.
      await page.goto(origin);
      await page.waitForFunction(() => window.cacheProbe);
      assert.deepEqual(await page.evaluate(() => window.cacheProbe), {
        entry:version, named:version, side:version, dynamic:version,
      });
      assert.equal(await page.locator('#probe').evaluate(node => getComputedStyle(node).width),
        version === 'new' ? '202px' : '101px');
      await page.close();
    }
    await inspect('old');
    assert.equal((await context.request.post(origin + '/_fixture/deploy')).status(),204);
    await inspect(expected);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({afterDeploy:expected,pageErrors:errors}));
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error);process.exit(1);});
