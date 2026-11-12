"""Public workflow parser checks use real scoped HTTP payloads and Node."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from test_workflow_http import http_setup
from test_workflow_control import control
from test_workflow_repository import OWNER
from hub.application.workflow_budget import WorkflowBudgetService
from hub.infrastructure.workflow_budget import WorkflowBudgetRepository
from hub.infrastructure.usage_repository import UsageRepository

NODE = shutil.which("node")
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(NODE is None, reason="node unavailable")
def test_workflow_detail_parser_accepts_real_http_and_rejects_internal_fields(tmp_path: Path) -> None:
    _, client, _ = http_setup(tmp_path)
    payload = client.get("/api/platform/v1/workflows/workflow-a").get_json()
    script = """
import assert from 'node:assert/strict';
const c = await import(process.env.CONTRACTS_URL);
const data = JSON.parse(process.env.WORKFLOW_PAYLOAD);
const parsed = c.parseWorkflowDetail(data);
assert.equal(parsed.workflow.workflow_id, 'workflow-a');
for (const mutate of [
  d => { d.workflow.lease_owner = 'secret'; },
  d => { d.workflow.steps[0].owner_id = 'secret'; },
  d => { d.workflow.steps[0].subject_digest = 'invalid'; },
  d => { d.workflow.steps[0].generation = 0; },
  d => { d.workflow.delivery_digest = 'z'.repeat(64); }
]) {
  const copy = structuredClone(data); mutate(copy);
  assert.throws(() => c.parseWorkflowDetail(copy), c.ContractError);
}
const budget = structuredClone(data);
budget.workflow.budget = [{step_key: 'develop', reserved_tokens: '9007199254740992', reserved_requests: 0,
 charged_tokens: null, charged_requests: null, settlement: 'unknown', cost: null, cost_state: 'unknown'}];
assert.equal(c.parseWorkflowDetail(budget).workflow.budget[0].reserved_tokens, '9007199254740992');
budget.workflow.budget[0].reserved_tokens = 9007199254740992;
assert.throws(() => c.parseWorkflowDetail(budget), c.ContractError);
assert.throws(() => c.parseWorkflowOperation({ok: true, operation: {action: 'pause', revision: 2, lease_owner: 'secret'}}), c.ContractError);
"""
    environment = {**os.environ, "CONTRACTS_URL": (ROOT / "frontend/assets/api/contracts.js").as_uri(), "WORKFLOW_PAYLOAD": json.dumps(payload)}
    result = subprocess.run([NODE, "--input-type=module", "-e", script], env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(NODE is None, reason="node unavailable")
def test_workflow_events_parser_accepts_durable_control_and_usage_events(tmp_path: Path) -> None:
    _, client, setup = http_setup(tmp_path)
    usage = UsageRepository(setup.repository.db_path, enforce_limits=False)
    usage.init()
    for _ in range(3):
        setup.tick()
    control(setup, "pause")
    control(setup, "resume")
    pending = usage.admit(OWNER, "model", setup.run_id(), 1, 1, now=setup.clock())
    usage.settle(pending, {"input_tokens": 4, "output_tokens": 6}, "succeeded", now=setup.clock())
    setup.platform.set_run_state(OWNER, setup.run_id(), "succeeded", now=setup.clock())
    setup.tick()
    asyncio.run(WorkflowBudgetService(WorkflowBudgetRepository(setup.repository), clock=setup.clock).tick(limit=8))
    control(setup, "invalidate", step="develop")
    payload = client.get("/api/platform/v1/workflows/workflow-a/events?limit=100").get_json()
    script = """
import assert from 'node:assert/strict';
const c = await import(process.env.CONTRACTS_URL);
const data = JSON.parse(process.env.WORKFLOW_PAYLOAD);
const events = c.parseWorkflowEvents(data).events;
for (const kind of ['workflow.pause_requested', 'workflow.resume_requested', 'step.usage_projected', 'workflow.generations_created']) {
  assert.ok(events.some(event => event.kind === kind), kind);
}
assert.ok(events.some(event => event.payload.charged_tokens === 10));
assert.equal(JSON.stringify(events).includes('dispatch_token'), false);
const invalid = structuredClone(data); invalid.events[0].payload.dispatch_token = 'secret';
assert.throws(() => c.parseWorkflowEvents(invalid), c.ContractError);
invalid.events[0].payload = {}; invalid.events.push(structuredClone(invalid.events[0]));
assert.throws(() => c.parseWorkflowEvents(invalid), c.ContractError);
"""
    result = subprocess.run([NODE, "--input-type=module", "-e", script], env={**os.environ,
        "CONTRACTS_URL": (ROOT / "frontend/assets/api/contracts.js").as_uri(), "WORKFLOW_PAYLOAD": json.dumps(payload)},
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(NODE is None, reason="node unavailable")
def test_workflow_template_parser_bounds_fixed_code_revision_inputs(tmp_path: Path) -> None:
    _, client, _ = http_setup(tmp_path)
    payload = client.get("/api/platform/v1/workflow-templates").get_json()
    script = """
import assert from 'node:assert/strict';
const c = await import(process.env.CONTRACTS_URL);
const data = JSON.parse(process.env.WORKFLOW_PAYLOAD);
assert.deepEqual(c.parseWorkflowTemplates(data).templates[0].input_refs, []);
const ref = {ref_key: 'source', kind: 'code_revision', repository_id: 'repo-a', base_commit: '1'.repeat(40),
  head_commit: '2'.repeat(40), tree_id: '3'.repeat(40), source_bundle: {artifact_id: 'abcdefghijklmnopqrstuvwx', sha256: 'a'.repeat(64), size: 10240}};
data.templates[0].input_refs = [ref];
assert.deepEqual(c.parseWorkflowTemplates(data).templates[0].input_refs, [ref]);
for (const mutate of [
  ref => { ref.root_path = '/private/source'; },
  ref => { ref.kind = 'shell'; },
  ref => { ref.head_commit = 'main'; },
  ref => { ref.tree_id = '3'.repeat(64); },
  ref => { ref.source_bundle.size = Number.MAX_SAFE_INTEGER + 1; }
]) {
  const invalid = structuredClone(data); mutate(invalid.templates[0].input_refs[0]);
  assert.throws(() => c.parseWorkflowTemplates(invalid), c.ContractError);
}
data.templates[0].input_refs.push(structuredClone(ref));
assert.throws(() => c.parseWorkflowTemplates(data), c.ContractError);
"""
    result = subprocess.run([NODE, "--input-type=module", "-e", script], env={**os.environ,
        "CONTRACTS_URL": (ROOT / "frontend/assets/api/contracts.js").as_uri(), "WORKFLOW_PAYLOAD": json.dumps(payload)},
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(NODE is None, reason="node unavailable")
def test_external_agent_provenance_roundtrips_real_http_without_granting_validation(tmp_path: Path) -> None:
    from hub.bootstrap import create_app
    from hub.config import FleetConfig
    from test_external_agent_workflow import make_external
    from test_muse_browser_bridge import message
    from test_workflow_repository import WORKSPACE

    setup = make_external(tmp_path)
    first = setup.dispatch()
    setup.apply(first)
    journal = setup.adapter.bridge.lookup(first.binding.token, setup.resolved.snapshot.decode(), scope=WORKSPACE)
    setup.transport.messages.append(message("done", "assistant", "Done\n" + journal["completion_marker"]))
    setup.apply(setup.dispatch())
    config = FleetConfig.from_root(tmp_path, platform_enabled=True, platform_workflows_enabled=True,
        platform_db=setup.repository.db_path, dev_operator=OWNER, serve_frontend=False)
    client = create_app(config).test_client()
    events = client.get("/api/platform/v1/workflows/workflow-a/events?limit=100").get_json()
    detail = client.get("/api/platform/v1/workflows/workflow-a").get_json()
    script = """
import assert from 'node:assert/strict';
const c = await import(process.env.CONTRACTS_URL);
const data = JSON.parse(process.env.WORKFLOW_PAYLOAD);
const events = c.parseWorkflowEvents(data.events).events;
assert.ok(events.some(event => event.payload.evidence_source === 'agent_reported'));
const detail = c.parseWorkflowDetail(data.detail).workflow;
assert.equal(detail.acceptance_state, 'pending');
assert.equal(detail.validation_state, 'not_requested');
assert.deepEqual(detail.steps[0].evidence, []);
const invalid = structuredClone(data.events);
invalid.events[0].payload.evidence_source = 'user_approved';
assert.throws(() => c.parseWorkflowEvents(invalid), c.ContractError);
assert.equal(JSON.stringify(events).includes('target_snapshot'), false);
"""
    result = subprocess.run([NODE, "--input-type=module", "-e", script], env={**os.environ,
        "CONTRACTS_URL": (ROOT / "frontend/assets/api/contracts.js").as_uri(),
        "WORKFLOW_PAYLOAD": json.dumps({"events": events, "detail": detail})}, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
