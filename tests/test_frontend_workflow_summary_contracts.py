"""Workflow summary frontend parsers accept and strictly validate real HTTP DTOs."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from test_workflow_http import ORIGIN, http_setup
from test_workflow_repository import WORKSPACE

NODE = shutil.which("node")
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(NODE is None, reason="node unavailable")
def test_workflow_summary_parsers_accept_real_dtos_and_reject_contract_drift(tmp_path: Path) -> None:
    _, client, _ = http_setup(tmp_path)
    url = "/api/platform/v1/workflow-summaries"
    payload = {"workspace_id": WORKSPACE, "summary_id": "summary-a", "title": "人工摘要", "content": "完整内容",
        "client_token": "publish-a", "expected_epoch": 0}
    published = client.post(url, json=payload, headers=ORIGIN)
    assert published.status_code == 201
    version = published.get_json()
    listed = client.get(url + f"?workspace_id={WORKSPACE}&limit=1").get_json()
    historical = client.get(url + f"/version?workspace_id={WORKSPACE}&summary_id=summary-a&revision=1").get_json()
    head = client.get(url + f"/head?workspace_id={WORKSPACE}&summary_id=summary-a").get_json()
    toggled = client.post(url + "/enabled", json={"workspace_id": WORKSPACE, "summary_id": "summary-a",
        "expected_epoch": 1, "enabled": False}, headers=ORIGIN).get_json()

    script = r'''
import assert from 'node:assert/strict';
const c = await import(process.env.API_URL);
const data = JSON.parse(process.env.DTOS);
assert.equal(c.parseWorkflowSummaryPublishResponse(data.published).summary.revision, 1);
assert.deepEqual(c.parseWorkflowSummaryPage(data.listed).summaries[0], data.head.summary);
assert.equal(c.parseWorkflowSummaryVersionResponse(data.historical).summary_id, 'summary-a');
assert.equal(c.parseWorkflowSummaryHeadResponse(data.head).enabled, true);
assert.equal(c.parseWorkflowSummaryHeadResponse(data.toggled).enabled, false);
for (const mutate of [
  value => { value.idempotent_replay = true; },
  value => { value.summary.sha256 = 'bad'; },
  value => { value.summary.schema_version = 2; },
]) {
  const invalid = structuredClone(data.published); mutate(invalid);
  assert.throws(() => c.parseWorkflowSummaryPublishResponse(invalid), c.ContractError);
}
const invalidCursor = structuredClone(data.listed); invalidCursor.next_cursor = 'missing';
assert.throws(() => c.parseWorkflowSummaryPage(invalidCursor), c.ContractError);
const invalidHead = structuredClone(data.head); invalidHead.summary.epoch = 0;
assert.throws(() => c.parseWorkflowSummaryHeadResponse(invalidHead), c.ContractError);
const invalidVersion = structuredClone(data.historical); invalidVersion.summary.created_by = 'forged';
assert.throws(() => c.parseWorkflowSummaryVersionResponse(invalidVersion), c.ContractError);
'''
    dtos = {"published": version, "listed": listed, "historical": historical, "head": head, "toggled": toggled}
    result = subprocess.run([NODE, "--input-type=module", "-e", script], env={**os.environ,
        "API_URL": (ROOT / "frontend/assets/api/workflow-summaries.js").as_uri(), "DTOS": json.dumps(dtos)},
        text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
