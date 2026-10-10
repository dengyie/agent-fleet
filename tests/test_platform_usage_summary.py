import json
import os
import sqlite3
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread

import pytest
from werkzeug.serving import make_server

from hub.application.task_service import ApplicationError
from hub.application.usage_summary_service import UsageSummaryService
from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.infrastructure.platform_db import PlatformRepository
from hub.infrastructure.usage_repository import UsageRepository


OWNER = "owner@example.test"
START = datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()
RANGE = {"from": "2026-10-01", "to": "2026-10-03"}
PRICING = {"currency": "USD", "input": "1.5", "output": "3", "cache_read": "1.5", "cache_write": "1.5"}
ROOT = Path(__file__).resolve().parents[1]


def ledger(tmp_path, *, pricing=None):
    db = tmp_path / "platform.db"
    platform = PlatformRepository(db)
    platform.init()
    platform.upsert_model(OWNER, {"profile_id": "model-a", "provider": "deterministic", "model": "actual-model",
                                 "provider_config": {"pricing": pricing} if pricing else {}})
    platform.upsert_workspace(OWNER, {"workspace_id": "project-a", "root_path": str(tmp_path / "work")})
    platform.upsert_node(OWNER, {"node_id": "machine-a"})
    platform.create_conversation(OWNER, "conv-a", title="", workspace_id="project-a")
    platform.append_turn(OWNER, "conv-a", "msg-a", "run-a", text="x", client_token="ct-a",
                         config_snapshot={"workspace_id": "project-a", "execution_node_id": "machine-a",
                                          "agent_family": "codex", "model_profile_id": "model-a"}, now=START)
    repo = UsageRepository(db)
    repo.init()
    return repo, platform


def settle(repo, *, step=1, now=START, usage=None, metadata=None, owner=OWNER, run="run-a"):
    reservation = repo.admit(owner, "model-a", run, 1, step, now=now)
    repo.settle(reservation, usage, "succeeded" if usage else "unknown", now=now, metadata=metadata)
    return reservation


def summary(repo, **params):
    return UsageSummaryService(repo, clock=lambda: START).summary(OWNER, {**RANGE, **params})


def test_settlement_is_idempotent_and_snapshots_configured_decimal_cost(tmp_path):
    repo, platform = ledger(tmp_path, pricing=PRICING)
    reservation = settle(repo, usage={"input_tokens": 2, "output_tokens": 3})
    repo.settle(reservation, {"input_tokens": 9, "output_tokens": 9}, "succeeded", metadata={"cost": {"amount": "99", "currency": "USD", "source": "provider"}})
    platform.upsert_model(OWNER, {"profile_id": "model-a", "provider": "deterministic", "model": "changed-model",
                                 "provider_config": {"pricing": {**PRICING, "input": "100"}}})
    item, = summary(repo, group_by="day,project,machine,agent,model")["items"]
    assert item == {"day": "2026-10-01", "project": "project-a", "machine": "machine-a", "agent": "codex", "model": "actual-model",
                    "requests": 1, "input_tokens": 2, "output_tokens": 3, "total_tokens": 5,
                    "estimated_cost": "0.000012", "cost_source": "configured", "unknown_count": 0}


def test_provider_cost_has_priority_and_preserves_decimal_precision(tmp_path):
    repo, _ = ledger(tmp_path, pricing=PRICING)
    settle(repo, usage={"input_tokens": 2, "output_tokens": 3}, metadata={
        "response_model": "returned-model", "cost": {"amount": "0.123456789012", "currency": "USD", "source": "provider"}})
    item, = summary(repo, group_by="model")["items"]
    assert item["model"] == "returned-model"
    assert (item["estimated_cost"], item["cost_source"]) == ("0.123456789012", "provider")


@pytest.mark.parametrize("usage", [None, {}, {"input_tokens": 5}, {"total_tokens": 5}])
def test_incomplete_usage_is_unknown_never_fabricated_zero(tmp_path, usage):
    repo, _ = ledger(tmp_path, pricing=PRICING)
    settle(repo, usage=usage)
    item, = summary(repo)["items"]
    assert item["input_tokens"] is item["output_tokens"] is item["total_tokens"] is None
    assert item["estimated_cost"] is None
    assert item["cost_source"] == "unknown" and item["unknown_count"] == 1


def test_unknown_bucket_does_not_present_partial_totals_as_complete(tmp_path):
    repo, _ = ledger(tmp_path, pricing=PRICING)
    settle(repo, usage={"input_tokens": 2, "output_tokens": 3})
    settle(repo, step=2)
    item, = summary(repo)["items"]
    assert item["requests"] == 2 and item["unknown_count"] == 1
    assert item["input_tokens"] is None and item["estimated_cost"] is None


def test_complete_tokens_without_price_and_provider_cost_without_tokens(tmp_path):
    repo, _ = ledger(tmp_path)
    settle(repo, usage={"input_tokens": 0, "output_tokens": 0})
    first, = summary(repo)["items"]
    assert first["total_tokens"] == 0 and first["estimated_cost"] is None
    settle(repo, now=START + 86400, step=2, metadata={"cost": {"amount": "0", "currency": "USD", "source": "provider"}})
    _, second = summary(repo)["items"]
    assert second["total_tokens"] is None and second["estimated_cost"] == "0"
    assert second["cost_source"] == "provider" and second["unknown_count"] == 1


def test_unsupported_provider_pricing_never_falls_back_to_configured_price(tmp_path):
    repo, _ = ledger(tmp_path, pricing=PRICING)
    settle(repo, usage={"input_tokens": 3, "output_tokens": 4}, metadata={"cost_unavailable_reason": "unsupported_modality"})
    assert summary(repo)["items"][0]["estimated_cost"] is None


def test_dimension_filters_owner_isolation_and_utc_exclusive_boundaries(tmp_path):
    repo, _ = ledger(tmp_path, pricing=PRICING)
    settle(repo, now=START - 1, step=1, usage={"input_tokens": 1, "output_tokens": 1})
    settle(repo, now=START, step=2, usage={"input_tokens": 1, "output_tokens": 1})
    settle(repo, now=START + 2 * 86400, step=3, usage={"input_tokens": 1, "output_tokens": 1})
    settle(repo, owner="other@example.test", run="run-other", usage={"input_tokens": 9, "output_tokens": 9})
    result = summary(repo, project="project-a", machine="machine-a", agent="codex", model="actual-model")
    assert result["items"][0]["requests"] == 1
    assert summary(repo, model="different")["items"] == []
    offset = summary(repo, **{"from": "2026-10-01T08:00:00+08:00", "to": "2026-10-03T08:00:00+08:00"})
    assert offset["items"] == summary(repo)["items"]


@pytest.mark.parametrize("params", [
    {"from": "2026-01-01", "to": "2026-04-02"}, {"from": "2026-10-04"},
    {"from": "2026-10-01T00:00:00"}, {"group_by": "day,day"}, {"group_by": "owner"},
    {"owner_id": OWNER}, {"pricing": "0"}, {"model": "x" * 161},
])
def test_summary_rejects_unbounded_or_untrusted_queries(tmp_path, params):
    repo, _ = ledger(tmp_path)
    with pytest.raises(ApplicationError) as error:
        summary(repo, **params)
    assert error.value.status == 400


def test_summary_accepts_exactly_90_days_and_limits_buckets(tmp_path):
    repo, _ = ledger(tmp_path)
    assert summary(repo, **{"from": "2026-01-01", "to": "2026-04-01"})["items"] == []
    with sqlite3.connect(repo.db_path) as conn:
        conn.executemany("INSERT INTO platform_usage_events(event_id,reservation_id,owner_id,model_key,run_id,attempt,step,outcome,occurred_at,summary_model) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         [(f"e{i}", f"r{i}", OWNER, "model-a", f"run{i}", 1, 1, "unknown", START, f"model-{i}") for i in range(1001)])
    with pytest.raises(ApplicationError) as error:
        summary(repo, group_by="model")
    assert error.value.code == "usage_bucket_limit"


def test_migration_is_idempotent_and_old_uncertain_rows_remain_unknown(tmp_path):
    repo, _ = ledger(tmp_path)
    with sqlite3.connect(repo.db_path) as conn:
        conn.execute("INSERT INTO platform_usage_events(event_id,reservation_id,owner_id,model_key,run_id,attempt,step,outcome,occurred_at) VALUES('old','old',?,'model-a','old-run',1,1,'succeeded',?)", (OWNER, START))
    repo.init(); repo.init()
    item, = summary(repo)["items"]
    assert item["requests"] == 1 and item["total_tokens"] is None


def test_summary_migration_failure_does_not_disable_existing_ledger(tmp_path, monkeypatch):
    repo = UsageRepository(tmp_path / "platform.db")
    def unavailable():
        raise sqlite3.OperationalError("private migration detail")
    monkeypatch.setattr(repo, "_migrate_summary", unavailable)
    repo.init()
    settle(repo, usage={"input_tokens": 2, "output_tokens": 3})
    assert repo.get_run_usage(OWNER, "run-a")["total_tokens"] == 5
    with pytest.raises(ApplicationError) as error:
        summary(repo)
    assert error.value.code == "usage_unavailable" and error.value.status == 503
    assert error.value.__cause__.__cause__.args == ("private migration detail",)


def test_operator_api_uses_authenticated_owner_and_fixed_errors(tmp_path):
    app = create_app(FleetConfig.from_root(tmp_path, platform_enabled=True, dev_operator=OWNER))
    repo = app.extensions["fleet"]["services"]["usage_repository"]
    settle(repo, usage={"input_tokens": 1, "output_tokens": 2}, run="api-run")
    client = app.test_client()
    response = client.get("/api/platform/v1/usage", query_string=RANGE)
    assert response.status_code == 200 and response.json["items"][0]["requests"] == 1
    invalid = client.get("/api/platform/v1/usage", query_string={**RANGE, "owner_id": "other@example.test"})
    assert invalid.status_code == 400 and "other@example.test" not in invalid.get_data(as_text=True)
    duplicated = client.get("/api/platform/v1/usage?group_by=day&group_by=model")
    assert duplicated.status_code == 400
    app.config["DEV_OPERATOR"] = "other@example.test"
    assert client.get("/api/platform/v1/usage", query_string=RANGE).json["items"] == []
    app.config["DEV_OPERATOR"] = None
    for headers in ({}, {"X-Ingest-Token": "not-operator"}, {"X-Runner-Token": "not-operator"}):
        assert client.get("/api/platform/v1/usage", headers=headers).status_code == 401


def test_runtime_preserves_missing_tokens_and_provider_cost(tmp_path):
    from tools.platform.providers.base import ModelResponse
    from tools.platform.runtime.native import NativeAssistantRuntime

    repo, _ = ledger(tmp_path)
    class Provider:
        def complete(self, messages, tools, *, request_observer=None):
            return ModelResponse(kind="final", text="done", usage=None,
                                 metadata={"cost": {"amount": "0.01", "currency": "USD", "source": "provider"}})

    runtime = NativeAssistantRuntime(Provider(), None, usage_meter=repo, model_key="model-a", clock=lambda: START)
    runtime.run(run_id="run-a", owner_id=OWNER, epoch=1, messages=[], tools=[])
    item, = summary(repo)["items"]
    assert item["total_tokens"] is None and item["estimated_cost"] == "0.01"
    assert item["unknown_count"] == 1


def test_stream_request_events_are_not_double_counted_and_legacy_metadata_is_reused(tmp_path):
    repo, platform = ledger(tmp_path)
    metadata = {"attempt": 1, "step": 1, "request_index": 1, "model": "stream-model",
                "usage": {"input_tokens": 2, "output_tokens": 3},
                "cost": {"amount": "0.03", "currency": "USD", "source": "provider"}}
    with sqlite3.connect(repo.db_path) as conn:
        for sequence in range(1, 5):
            conn.execute("INSERT INTO run_events(run_id,sequence,kind,payload,created_at) VALUES(?,?,?,?,?)",
                         ("run-a", sequence, "provider_request_finished", json.dumps(metadata), START))
    settle(repo, usage={"input_tokens": 2, "output_tokens": 3})
    item, = summary(repo, group_by="model")["items"]
    assert item["requests"] == 1 and item["estimated_cost"] == "0.03" and item["model"] == "stream-model"


def test_expired_reservation_reissue_and_late_settle_count_one_request(tmp_path):
    repo, _ = ledger(tmp_path, pricing=PRICING)
    old = repo.admit(OWNER, "model-a", "run-a", 1, 1, now=START)
    repo.admit(OWNER, "model-a", "cleanup-run", 1, 1, now=START + 3601)
    replacement = repo.admit(OWNER, "model-a", "run-a", 1, 1, now=START + 3602)
    assert replacement.reservation_id != old.reservation_id
    repo.settle(old, {"input_tokens": 2, "output_tokens": 3}, "succeeded", now=START + 3603)
    repo.settle(replacement, {"input_tokens": 8, "output_tokens": 8}, "succeeded", now=START + 3604)
    item, = summary(repo)["items"]
    assert item["requests"] == 1 and item["total_tokens"] == 5
    with sqlite3.connect(repo.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM platform_usage_reservations WHERE run_id='run-a'").fetchone()[0] == 0


def test_usage_contract_rejects_unsafe_amounts_fields_and_large_numbers():
    if not shutil.which("node"):
        pytest.skip("Node is optional for the production frontend")
    module = (ROOT / "frontend/assets/api/contracts.js").as_uri()
    code = """
import assert from 'node:assert/strict';
const {parseUsageSummary, ContractError} = await import(process.argv[1]);
const value = {ok:true, from:'2026-10-01T00:00:00Z', to:'2026-10-03T00:00:00Z', group_by:['model'], currency:'USD',
  items:[{model:'x', requests:1, input_tokens:'9007199254740992', output_tokens:1, total_tokens:'9007199254740993',
    estimated_cost:'0.123456789012', cost_source:'provider', unknown_count:0}]};
assert.equal(parseUsageSummary(value).items[0].total_tokens, '9007199254740993');
assert.equal(parseUsageSummary(value).items[0].estimated_cost, '0.123456789012');
for (const mutate of [
  v => { v.items[0].raw = 'secret'; }, v => { v.owner_id = 'other'; },
  v => { v.items[0].cost_source = 'actual'; }, v => { v.items[0].estimated_cost = 0.1; },
  v => { v.items[0].input_tokens = 9007199254740992; }, v => { v.items[0].total_tokens = null; },
  v => { v.items[0].cost_source = 'unknown'; }, v => { v.items = Array(1001).fill(v.items[0]); },
  v => { v.group_by = ['raw']; }, v => { v.items[0].unknown_count = 2; },
]) { const copy = structuredClone(value); mutate(copy); assert.throws(() => parseUsageSummary(copy), ContractError); }
const unknown = structuredClone(value); Object.assign(unknown.items[0], {input_tokens:null,output_tokens:null,total_tokens:null,
  estimated_cost:null,cost_source:'unknown',unknown_count:1});
assert.equal(parseUsageSummary(unknown).items[0].estimated_cost, null);
"""
    result = subprocess.run(["node", "--input-type=module", "-e", code, module], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_usage_summary_in_real_browser(tmp_path):
    if not os.environ.get("FLEET_PLAYWRIGHT_MODULE"):
        pytest.skip("Set FLEET_PLAYWRIGHT_MODULE to run Chromium acceptance")
    app = create_app(FleetConfig.from_root(tmp_path, frontend_dir=ROOT / "frontend", dev_operator=OWNER, platform_enabled=True))
    repo = app.extensions["fleet"]["services"]["usage_repository"]
    platform = app.extensions["fleet"]["platform_repository"]
    platform.upsert_model(OWNER, {"profile_id": "model-a", "provider": "deterministic", "model": "configured-model", "provider_config": {"pricing": PRICING}})
    platform.upsert_workspace(OWNER, {"workspace_id": "project-a", "root_path": str(tmp_path / "work")})
    platform.upsert_node(OWNER, {"node_id": "machine-a"})
    platform.create_conversation(OWNER, "browser-conversation", title="用量验收", workspace_id="project-a")
    for run in ("known-run", "unknown-run", "configured-run"):
        platform.append_turn(OWNER, "browser-conversation", "msg-" + run, run, text="用量验收", client_token=run,
                             config_snapshot={"workspace_id": "project-a", "execution_node_id": "machine-a", "agent_family": "codex", "model_profile_id": "model-a"}, now=START)
    settle(repo, run="known-run", usage={"input_tokens": 2, "output_tokens": 3}, metadata={
        "response_model": "known-model", "cost": {"amount": "0.123456789012", "currency": "USD", "source": "provider"}})
    settle(repo, run="unknown-run", metadata={"response_model": "unknown-model"})
    settle(repo, run="configured-run", usage={"input_tokens": 2, "output_tokens": 3})
    server = make_server("127.0.0.1", 0, app, threaded=True)
    serving = Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        result = subprocess.run(["node", str(ROOT / "tests/fixtures/frontend/usage_browser.cjs"),
                                 f"http://127.0.0.1:{server.server_port}"], capture_output=True, text=True, timeout=90)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.shutdown(); server.server_close(); serving.join(5)
