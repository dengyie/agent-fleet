"""Generated-summary frontend contracts against original HTTP responses."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from test_workflow_summary_generation import generation_http
from test_workflow_repository import OWNER


@pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")
def test_generated_summary_frontend_parses_real_run_and_version_and_rejects_forgery(tmp_path):
    _, client, platform, values = generation_http(tmp_path)
    url = "/api/platform/v1/workflow-summary-generations"
    headers = {"Origin": "http://localhost"}
    queued = client.post(url, json=values, headers=headers).get_json()
    run_id = queued["generation"]["run_id"]
    platform.set_run_state(OWNER, run_id, "succeeded", now=102, result_text="完整模型摘要\n")
    succeeded = client.get(url + "/run", query_string={"run_id": run_id}).get_json()
    version = client.post(url + "/publish", json={"run_id": run_id}, headers=headers).get_json()
    published = client.get(url + "/run", query_string={"run_id": run_id}).get_json()
    script = r'''
import assert from 'node:assert/strict';
const c = await import(process.env.API_URL);
const data = JSON.parse(process.env.DTOS);
assert.equal(c.parseWorkflowSummaryGenerationResponse(data.queued).state, 'queued');
assert.equal(c.parseWorkflowSummaryGenerationResponse(data.succeeded).candidate, data.succeeded.generation.candidate);
assert.equal(c.parseWorkflowSummaryGenerationResponse(data.succeeded).candidate.endsWith(String.fromCharCode(10)), true);
assert.equal(c.parseWorkflowSummaryGenerationResponse(data.published).published_revision, 1);
assert.equal(c.parseWorkflowSummaryVersionResponse(data.version).source_kind, 'model');
for (const change of [
  v => { v.generation.extra = true; },
  v => { v.generation.state = 'accepted'; },
  v => { v.generation.source_run_ids.push(v.generation.source_run_ids[0]); },
  v => { v.generation.published_revision = 0; },
  v => { v.generation.source_sha256 = 'bad'; },
]) {
  const value = structuredClone(data.queued); change(value);
  assert.throws(() => c.parseWorkflowSummaryGenerationResponse(value), c.ContractError);
}
for (const change of [
  v => { v.summary.source_kind = 'operator'; },
  v => { v.summary.schema_version = 1; },
  v => { v.summary.generation.provider_config = {secret: 'forged'}; },
  v => { v.summary.generation.source_run_ids = []; },
  v => { v.summary.generation.provider_digest = 'bad'; },
]) {
  const value = structuredClone(data.version); change(value);
  assert.throws(() => c.parseWorkflowSummaryVersionResponse(value), c.ContractError);
}
'''
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([shutil.which("node"), "--input-type=module", "-e", script], capture_output=True,
        text=True, timeout=15, env={**os.environ, "API_URL": (root / "frontend/assets/api/workflow-summaries.js").as_uri(),
            "DTOS": json.dumps({"queued": queued, "succeeded": succeeded, "published": published, "version": version})})
    assert result.returncode == 0, result.stderr
