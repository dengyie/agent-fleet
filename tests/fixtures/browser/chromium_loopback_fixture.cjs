'use strict';

const http = require('node:http');
const net = require('node:net');
const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);

const MAX_REQUEST_BYTES = 16 * 1024;
const MAX_RESULT_BYTES = 16 * 1024;
const MAX_SCREENSHOT_BYTES = 256 * 1024;
const MAX_RESPONSE_HEADERS = 64 * 1024;
const MAX_RESPONSE_BYTES = 16 * 1024 * 1024;
const MAX_SESSION_BYTES = 64 * 1024 * 1024;
const MAX_IN_FLIGHT = 16;
const REQUEST_DEADLINE_MS = 20_000;

function failure(code) {
  return Object.assign(new Error(code), {code});
}

function literalLoopbackUrl(value, fixtureOrigin) {
  let url;
  try {
    url = new URL(value);
  } catch {
    throw failure('invalid_url');
  }
  const host = url.hostname.replace(/^\[|\]$/g, '');
  const ipv4Loopback = net.isIP(host) === 4 && host.startsWith('127.');
  const ipv6Loopback = net.isIP(host) === 6 && host.toLowerCase() === '::1';
  if (url.protocol !== 'http:' || url.username || url.password || url.hash
      || !(ipv4Loopback || ipv6Loopback) || url.origin !== fixtureOrigin) {
    throw failure('origin_forbidden');
  }
  return url;
}

function canonicalFixtureOrigin(value) {
  const url = literalLoopbackUrl(value, new URL(value).origin);
  if (url.pathname !== '/' || url.search) throw failure('invalid_fixture_origin');
  return url.origin;
}

function fetchFixture(value, method, scope) {
  const url = literalLoopbackUrl(value, scope.origin);
  if (method !== 'GET' && method !== 'HEAD') throw failure('method_forbidden');
  if (scope.active >= MAX_IN_FLIGHT) throw failure('request_limit_exceeded');
  scope.active += 1;
  scope.requests += 1;

  return new Promise((resolve, reject) => {
    let settled = false;
    let response;
    let request;
    let bodyBytes = 0;
    const chunks = [];
    const finish = (error, result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (error) reject(error);
      else resolve(result);
    };
    const timer = setTimeout(() => {
      response?.destroy();
      request?.destroy();
      finish(failure('timeout'));
    }, REQUEST_DEADLINE_MS);

    request = http.request(url, {
      method,
      agent: false,
      maxHeaderSize: MAX_RESPONSE_HEADERS,
      headers: {'accept-encoding': 'identity'},
    }, incoming => {
      response = incoming;
      const headerBytes = incoming.rawHeaders.reduce((size, item) =>
        size + Buffer.byteLength(item) + 2, 0);
      if (headerBytes > MAX_RESPONSE_HEADERS) {
        response.destroy();
        finish(failure('response_too_large'));
        return;
      }
      const status = incoming.statusCode || 0;
      if (status >= 300 && status < 400) {
        response.destroy();
        finish(failure('redirect_denied'));
        return;
      }
      const declaredLength = Number(incoming.headers['content-length']);
      if (Number.isFinite(declaredLength) && declaredLength > MAX_RESPONSE_BYTES) {
        response.destroy();
        finish(failure('response_too_large'));
        return;
      }
      incoming.on('data', chunk => {
        if (settled) return;
        bodyBytes += chunk.length;
        if (bodyBytes > MAX_RESPONSE_BYTES
            || scope.bytes + chunk.length > MAX_SESSION_BYTES) {
          response.destroy();
          finish(failure('response_too_large'));
          return;
        }
        scope.bytes += chunk.length;
        chunks.push(chunk);
      });
      incoming.on('end', () => finish(null, {
        status,
        body: Buffer.concat(chunks, bodyBytes),
        contentType: String(incoming.headers['content-type'] || 'application/octet-stream').slice(0, 256),
      }));
      incoming.on('error', () => finish(failure('fixture_unavailable')));
    });
    request.on('error', () => finish(failure('fixture_unavailable')));
    request.end();
  }).finally(() => {
    scope.active -= 1;
  });
}

async function runScenario(fixtureOrigin, redirectUrl, forbiddenUrl) {
  const origin = canonicalFixtureOrigin(fixtureOrigin);
  const redirect = literalLoopbackUrl(redirectUrl, origin);
  let forbidden;
  try {
    forbidden = new URL(forbiddenUrl);
  } catch {
    throw failure('invalid_url');
  }
  literalLoopbackUrl(forbiddenUrl, forbidden.origin);
  if (forbidden.origin === origin) throw failure('invalid_fixture_scenario');
  const scope = {origin, active: 0, bytes: 0, requests: 0};
  const browser = await chromium.launch({
    channel: process.env.FLEET_BROWSER_CHANNEL || 'chrome',
    headless: true,
    timeout: 15_000,
    args: [
      '--disable-background-networking',
      '--disable-component-update',
      '--disable-sync',
      '--no-proxy-server',
      '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1',
    ],
  });
  let context;
  let page;
  let routeFailure = null;
  try {
    context = await browser.newContext({
      acceptDownloads: false,
      javaScriptEnabled: false,
      serviceWorkers: 'block',
      viewport: {width: 1024, height: 768},
    });
    context.setDefaultTimeout(5_000);
    context.setDefaultNavigationTimeout(10_000);
    await context.route('**/*', async route => {
      try {
        const request = route.request();
        if (request.isNavigationRequest() && request.frame() !== page.mainFrame()) {
          throw failure('frame_forbidden');
        }
        const reply = await fetchFixture(request.url(), request.method(), scope);
        await route.fulfill({
          status: reply.status,
          body: reply.body,
          headers: {
            'cache-control': 'no-store',
            'content-type': reply.contentType,
          },
        });
      } catch (cause) {
        routeFailure = cause.code || 'request_denied';
        try {
          await route.abort('blockedbyclient');
        } catch {
          routeFailure = 'request_interceptor_failed';
        }
      }
    });
    page = await context.newPage();
    page.on('download', download => download.cancel().catch(() => {}));
    await page.goto(literalLoopbackUrl(`${origin}/`, origin).href, {waitUntil: 'load'});

    const snapshot = await page.locator('body').evaluate(body => ({
      title: document.title.slice(0, 256),
      text: body.innerText.slice(0, 8 * 1024),
    }));
    if (Buffer.byteLength(JSON.stringify(snapshot), 'utf8') > MAX_RESULT_BYTES) {
      throw failure('result_too_large');
    }
    const rendered = await page.evaluate(async () => {
      const image = document.querySelector('img');
      await image.decode();
      return {
        bodyBackground: getComputedStyle(document.body).backgroundColor,
        imageLoaded: image.complete && image.naturalWidth > 0,
        imageWidth: image.naturalWidth,
        imageHeight: image.naturalHeight,
      };
    });

    const screenshot = await page.screenshot({
      type: 'png',
      fullPage: false,
      mask: [page.locator('input, textarea, select, [contenteditable]')],
      maskColor: '#000000',
    });
    if (screenshot.length > MAX_SCREENSHOT_BYTES) throw failure('capture_too_large');
    const maskedInputCenterPixel = await page.evaluate(async base64 => {
      const image = new Image();
      image.src = `data:image/png;base64,${base64}`;
      await image.decode();
      const bounds = document.querySelector('input').getBoundingClientRect();
      const canvas = document.createElement('canvas');
      canvas.width = image.naturalWidth;
      canvas.height = image.naturalHeight;
      const context = canvas.getContext('2d');
      if (!context) throw new Error('canvas_unavailable');
      context.drawImage(image, 0, 0);
      const scaleX = image.naturalWidth / window.innerWidth;
      const scaleY = image.naturalHeight / window.innerHeight;
      const x = Math.floor((bounds.x + bounds.width / 2) * scaleX);
      const y = Math.floor((bounds.y + bounds.height / 2) * scaleY);
      return Array.from(context.getImageData(x, y, 1, 1).data.slice(0, 3));
    }, screenshot.toString('base64'));

    let crossOriginNavigation;
    try {
      await page.goto(forbidden.href, {waitUntil: 'load'});
      crossOriginNavigation = 'unexpectedly_allowed';
    } catch {
      crossOriginNavigation = routeFailure || 'navigation_failed';
    }

    routeFailure = null;
    let redirectNavigation;
    try {
      await page.goto(redirect.href, {waitUntil: 'load'});
      redirectNavigation = 'unexpectedly_allowed';
    } catch {
      redirectNavigation = routeFailure === 'redirect_denied'
        ? 'redirect_denied'
        : routeFailure || 'navigation_failed';
    }

    return {
      title: snapshot.title,
      text: snapshot.text,
      ...rendered,
      maskedInputCenterPixel,
      pngBase64: screenshot.toString('base64'),
      crossOriginNavigation,
      redirectNavigation,
      fixtureRequestCount: scope.requests,
    };
  } finally {
    if (context) await context.close();
    await browser.close();
  }
}

async function main() {
  const args = process.argv.slice(2);
  if (args.length !== 3 || Buffer.byteLength(args.join('\n')) > MAX_REQUEST_BYTES) {
    throw failure('invalid_request');
  }
  const result = await runScenario(...args);
  const output = JSON.stringify({...result, closed: true});
  if (Buffer.byteLength(output, 'utf8') > MAX_SCREENSHOT_BYTES * 2) {
    throw failure('result_too_large');
  }
  process.stdout.write(`${output}\n`);
}

main().catch(cause => {
  process.stderr.write(`${cause.code || 'fixture_failed'}\n`);
  process.exitCode = 1;
});
