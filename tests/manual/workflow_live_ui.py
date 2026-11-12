"""Restore the saved real pilot for read-only desktop/mobile UI verification.

Reuses Chrome's existing CDP listener, with a new disposable browser context.
Does not invoke a model, accept delivery, start workers or change production.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from workflow_live_pilot import ROOT, WORKSPACE, CookieClient, hub_server  # noqa: E402
from hub.bootstrap import create_app  # noqa: E402
from hub.config import FleetConfig  # noqa: E402
from hub.application.workflow_templates import RegisteredWorkflowTemplate, WorkflowTemplateCatalog, coding_delivery_definition  # noqa: E402


BROWSER = r"""
const {chromium} = require(process.env.FLEET_PLAYWRIGHT_MODULE);
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const [origin, root, cdp, outputRoot] = process.argv.slice(2);
const access = JSON.parse(fs.readFileSync(path.join(root, 'private/hub-access.json')));
const pilot = JSON.parse(fs.readFileSync(process.env.FLEET_PILOT_RESULT || path.join(root, 'result.json')));
(async () => {
  const browser = await chromium.connectOverCDP(cdp);
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}, acceptDownloads: true});
  const errors = [], mutations = [], downloads = [];
  let outcome = {};
  try {
    const login = await context.request.post(origin + '/api/accounts/login', {headers: {Origin: origin}, data: {email: access.email, password: access.password}});
    assert.equal(login.status(), 200);
    const page = await context.newPage(); page.setDefaultTimeout(10000);
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => {
      if (request.url().startsWith(origin + '/api/platform/v1/workflows') && request.method() !== 'GET') {
        mutations.push({method: request.method(), path: new URL(request.url()).pathname});
      }
    });
    await page.route('**/api/platform/v1/workflows**', async route => {
      if (route.request().method() !== 'GET') return route.abort('blockedbyclient');
      return route.continue();
    });
    await page.goto(origin + '/assistant', {waitUntil: 'domcontentloaded'});
    const panel = page.getByRole('region', {name: '工作流', exact: true});
    for (const mode of ['desktop', 'mobile']) {
      if (mode === 'mobile') {
        await page.setViewportSize({width: 390, height: 844});
        await page.reload({waitUntil: 'domcontentloaded'});
        const toggle = page.getByRole('button', {name: '工作面板', exact: true});
        await toggle.waitFor();
        if (await toggle.getAttribute('aria-expanded') !== 'true') await toggle.click();
      }
      await page.getByRole('tab', {name: '流程', exact: true}).click();
      await panel.locator('[data-workflow-id="' + pilot.workflow_id + '"]').click();
      await panel.locator('.assistant-workflow-state').getByText('等待人工验收', {exact: true}).waitFor();
      await panel.locator('.assistant-workflow-meta').getByText('校验：通过', {exact: true}).waitFor();
      await panel.getByRole('button', {name: '接受交付', exact: true}).waitFor();
      assert.equal(await panel.getByText('已交付', {exact: true}).count(), 0);
      const detailResponse = await context.request.get(origin + '/api/platform/v1/workflows/' + pilot.workflow_id);
      assert.equal(detailResponse.status(), 200);
      const detail = (await detailResponse.json()).workflow;
      assert.equal(detail.delivery_state, 'awaiting_acceptance');
      assert.equal(detail.delivery_digest, pilot.delivery_digest);
      assert.equal(detail.revision, pilot.revision);
      if (mode === 'desktop') {
        for (const step of detail.steps) {
          for (const output of step.outputs) {
            const link = panel.locator('[data-step="' + step.step_key + '"]').getByRole('link', {name: output.output_key, exact: true});
            const pending = page.waitForEvent('download');
            await link.press('Enter');
            const download = await pending;
            assert.equal(await download.failure(), null);
            const raw = fs.readFileSync(await download.path());
            assert.equal(raw.length, output.size);
            assert.equal(crypto.createHash('sha256').update(raw).digest('hex'), output.sha256);
            downloads.push({step: step.step_key, key: output.output_key, size: raw.length, sha256: output.sha256});
          }
        }
      }
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false);
      await page.screenshot({path: path.join(outputRoot, 'ui-' + mode + '.png'), fullPage: true});
    }
    assert.deepEqual(errors, []);
    assert.deepEqual(mutations, []);
    assert.equal(downloads.length, 5);
    assert.equal((await context.request.post(origin + '/api/accounts/logout', {headers: {Origin: origin}, data: {}})).status(), 200);
    assert.equal((await context.request.get(origin + '/api/platform/v1/workflows/' + pilot.workflow_id)).status(), 401);
    outcome = {status: 'passed', workflow_id: pilot.workflow_id, views: ['desktop', 'mobile_reload'],
      downloads, page_errors: errors, workflow_mutations: mutations, human_acceptance: 'not_performed',
      browser: browser.version(), chrome_connection: 'existing CDP, disposable context only',
      delivery_digest: pilot.delivery_digest, revision: pilot.revision};
    fs.writeFileSync(path.join(outputRoot, 'ui-result.json'), JSON.stringify(outcome, null, 2), {flag: 'wx', mode: 0o600});
  } finally {
    await context.close();
    // Disconnect by exiting this client; never close the user's Chrome process.
  }
  console.log(JSON.stringify(outcome));
  process.exit(0);
})().catch(error => {
  fs.writeFileSync(path.join(outputRoot, 'ui-failure.json'), JSON.stringify({status: 'failed', error: error.message, stack: error.stack}, null, 2), {flag: 'wx', mode: 0o600});
  console.error(error.message);
  process.exit(1);
});
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--playwright-module", type=Path, required=True)
    parser.add_argument("--ui-output-dir", type=Path, required=True)
    parser.add_argument("--cdp", default="http://127.0.0.1:9222")
    parser.add_argument("--pilot-result", type=Path, help="independently verified original-run result; never rewrites a failed report")
    args = parser.parse_args()
    root = args.evidence_root.resolve(strict=True)
    output_root = args.ui_output_dir.expanduser().resolve()
    output_root.mkdir(mode=0o700, parents=True, exist_ok=False)
    result_path = args.pilot_result.resolve(strict=True) if args.pilot_result is not None else root / "result.json"
    result = json.loads(result_path.read_bytes())
    assert result["status"] == "automation_passed_awaiting_human"
    access = json.loads((root / "private/hub-access.json").read_bytes())
    registration = json.loads((root / "registration.json").read_bytes())
    owner = registration["owner"]
    catalog = WorkflowTemplateCatalog((RegisteredWorkflowTemplate(owner, WORKSPACE, coding_delivery_definition()),))
    app = create_app(FleetConfig.from_root(root / "hub", platform_enabled=True, platform_workflows_enabled=True,
        serve_frontend=True, frontend_dir=ROOT / "frontend", session_repositories_enabled=True,
        session_encryption_raw=base64.b64decode(access["transcript_key_b64"]),
        platform_command_signing_raw=base64.b64decode(access["signing_key_b64"]),
        runner_credentials={"pilot-local": access["runner_credential"]}, project_whitelist={"pilot-local": ["pilot-repo"]},
        accounts={"enabled": True, "origin": "http://127.0.0.1", "registration": "invite"}),
        repositories={"workflow_templates": catalog})
    platform = app.extensions["fleet"]["platform_repository"]
    defaults = platform.get_defaults(owner)
    if defaults.get("workspace_id") != WORKSPACE:
        platform.update_defaults(owner, {"workspace_id": WORKSPACE}, defaults["revision"])
    script = output_root / "ui-check.cjs"
    with script.open("x", encoding="utf-8") as stream:
        stream.write(BROWSER)
    with hub_server(app) as origin:
        process = subprocess.run(["node", str(script), origin, str(root), args.cdp, str(output_root)],
            env={**os.environ, "FLEET_PLAYWRIGHT_MODULE": str(args.playwright_module.resolve(strict=True)),
                 "FLEET_PILOT_RESULT": str(result_path)}, timeout=90)
        # Independently verify that browser recovery did not accept or modify it.
        client = CookieClient(origin)
        assert client.request("/api/accounts/login", {"email": access["email"], "password": access["password"]})[0] == 200
        detail = client.request("/api/platform/v1/workflows/" + result["workflow_id"])[1]["workflow"]
        assert detail["delivery_state"] == "awaiting_acceptance" and detail["revision"] == result["revision"]
        assert detail["delivery_digest"] == result["delivery_digest"]
        assert client.request("/api/accounts/logout", {})[0] == 200
        return process.returncode


if __name__ == "__main__":
    raise SystemExit(main())
