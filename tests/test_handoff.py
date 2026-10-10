"""Deterministic, redacted handoff and transactional retry contracts."""
import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, local

import pytest

from hub.domain.handoff import HandoffConflict, build_handoff_snapshot, render_handoff_prompt
from hub.application.handoff_service import HandoffService
from hub.application.task_service import ApplicationError, TaskHostPolicy, TaskService
from hub.infrastructure.task_repository import SqliteTaskRepository


def event(sequence, kind, payload):
    return {"sequence": sequence, "event_id": f"evt_{sequence}", "kind": kind,
            "capture_quality": "structured", "payload": payload}


def test_empty_snapshot_is_explicit_and_deterministic():
    snapshot = build_handoff_snapshot({}, [], None)
    assert set(snapshot) == {"objective", "current_state", "completed", "changed_files",
                             "tests", "last_conclusion", "open_items", "source_event_refs",
                             "capture_quality"}
    assert snapshot["tests"] == {}
    assert snapshot["changed_files"] == []
    assert snapshot["open_items"]
    assert render_handoff_prompt(snapshot) == render_handoff_prompt(snapshot)


def test_order_duplicates_paths_redaction_and_no_source_record():
    events = [event(1, "user_message", {"text": "Repair authentication"}),
              event(2, "tool_call", {"tool_name": "write_file", "call_id": "a",
                                     "arguments": json.dumps({"path": "src/main.py"})}),
              event(3, "tool_result", {"tool_name": "write_file", "call_id": "a", "status": "completed"}),
              event(4, "assistant_message", {"text": "old conclusion"}),
              event(5, "assistant_message", {"text": "API_KEY=super-private-value"})]
    events[-1]["source_record"] = {"text": "RAW-MUST-NOT-LEAK"}
    snapshot = build_handoff_snapshot({"capture_quality": "structured"}, events[::-1] + [events[2]], None)
    assert snapshot["objective"] == "Repair authentication"
    assert snapshot["changed_files"] == ["src/main.py"]
    assert len(snapshot["completed"]) == 1
    assert "super-private-value" not in json.dumps(snapshot)
    assert "RAW-MUST-NOT-LEAK" not in json.dumps(snapshot)


@pytest.mark.parametrize("path", ["../x.py", "/tmp/x.py", "C:/tmp/x.py", ".env", "src/secret.json",
                                 "src/passwords.json", "x" * 70 + ".py"])
def test_rejects_unsafe_paths(path):
    snapshot = build_handoff_snapshot({}, [event(1, "tool_call", {
        "tool_name": "write_file", "arguments": json.dumps({"path": path})})], None)
    assert snapshot["changed_files"] == []


def test_symlink_path_is_never_included():
    snapshot = build_handoff_snapshot({}, [event(1, "tool_call", {
        "tool_name": "write_file", "arguments": json.dumps({"path": "src/link.py", "is_symlink": True})})], None)
    assert snapshot["changed_files"] == []


def test_bounds_include_long_prompt_and_file_limit():
    events = [event(i + 1, "tool_call", {"tool_name": "write_file", "call_id": str(i),
              "arguments": json.dumps({"path": f"src/file{i}.py"})}) for i in range(120)]
    events.append(event(200, "user_message", {"text": "界" * 20000}))
    snapshot = build_handoff_snapshot({}, events, None, max_files=3, max_chars=1200)
    assert len(snapshot["changed_files"]) <= 3
    assert len(json.dumps(snapshot, ensure_ascii=False)) <= 1200
    assert len(render_handoff_prompt(snapshot, "continue")) <= 2000


class Sessions:
    def __init__(self, row):
        self.row = row

    def get_session(self, session_id):
        return dict(self.row) if session_id == self.row["session_id"] else None


class Transcript:
    def read_handoff_events(self, session_id, *, limit=1000):
        return [event(1, "user_message", {"text": "Fix bug"})], False

    def read_handoff_signals(self, session_id):
        return []


class Observations:
    def read_current(self, machine):
        return {"reachable": True} if machine == "mac-local" else None


class Publisher:
    def emit(self, *args, **kwargs):
        return None


@pytest.fixture
def services(tmp_path):
    repo = SqliteTaskRepository(tmp_path / "tasks.db")
    repo.init()
    source, _ = repo.create_task(machine="mac-local", project="agent-fleet", agent_type="codex",
                                instruction="Fix bug", requested_by="operator")
    row = {"session_id": "source-session", "managed": True, "machine_id": "mac-local",
           "attempt_id": source["attempt_id"], "capture_quality": "structured"}
    tasks = TaskService(repo, Observations(), Publisher(),
                        TaskHostPolicy(project_whitelist={"mac-local": ["agent-fleet", "other"]}))
    service = HandoffService(Sessions(row), Transcript(), tasks)
    return service, repo, row


def payload(**kwargs):
    return {"target_machine": "mac-local", "target_project": "agent-fleet",
            "target_agent": "codex", "instruction_override": "", "client_token": "retry-1", **kwargs}


def test_preview_does_not_create_and_unmanaged_cannot_create(services):
    service, repo, row = services
    assert service.preview("source-session")["snapshot"]["objective"] == "Fix bug"
    assert len(repo.list_tasks()) == 1
    row["managed"] = False
    assert service.preview("source-session")["can_create"] is False
    with pytest.raises(ApplicationError, match="受管"):
        service.create("source-session", payload(), "operator")
    assert len(repo.list_tasks()) == 1


def test_concurrent_retry_same_receipt_and_confirmation_gate(services):
    service, repo, row = services
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: service.create("source-session", payload(), "operator"), range(8)))
    assert all(item == responses[0] for item in responses)
    receipt = responses[0]
    assert receipt["attempt_id"] != row["attempt_id"]
    assert len(repo.list_tasks()) == 2
    task = repo.get_task(receipt["task_id"])
    assert task["gate"]["state"] == "pending"
    assert task["client_token"] != "retry-1"
    repo.confirm_task(receipt["task_id"], actor="operator")
    repo.lease_task(machine="mac-local", runner_id="runner")
    repo.lease_task(machine="mac-local", runner_id="runner")
    assert service.create("source-session", payload(), "operator") == receipt


def test_conflicting_token_and_cross_project_fail_closed(services):
    service, repo, _ = services
    service.create("source-session", payload(), "operator")
    with pytest.raises(ApplicationError) as error:
        service.create("source-session", payload(instruction_override="different"), "operator")
    assert error.value.code == "handoff_token_conflict"
    with pytest.raises(ApplicationError) as error:
        service.create("source-session", payload(client_token="new", target_project="other"), "operator")
    assert error.value.code == "handoff_project_mismatch"
    assert len(repo.list_tasks()) == 2


def test_atomic_repository_conflict_rolls_back_and_keeps_typed_error(services):
    _, repo, _ = services
    arguments = {"machine": "mac-local", "project": "agent-fleet", "agent_type": "codex",
                 "instruction": "continue", "requested_by": "operator", "client_token": "atomic",
                 "confirm": True}
    repo.create_task(**arguments, handoff={"source_session_id": "source-session", "request_digest": "first"})
    with pytest.raises(HandoffConflict) as caught:
        repo.create_task(**arguments, handoff={"source_session_id": "source-session", "request_digest": "second"})
    assert caught.value.code == "handoff_token_conflict"
    assert len(repo.list_tasks()) == 2


def test_conflicting_requests_racing_at_commit_return_one_receipt_and_one_409(services, monkeypatch):
    service, repo, _ = services
    initial_reads = Barrier(2)
    worker = local()
    read = repo.get_handoff_receipt
    def synchronized_read(token):
        receipt = read(token)
        if not getattr(worker, "first_read", False):
            worker.first_read = True
            initial_reads.wait(timeout=5)
        return receipt
    monkeypatch.setattr(repo, "get_handoff_receipt", synchronized_read)
    def submit(instruction):
        try:
            return service.create("source-session", payload(instruction_override=instruction), "operator")
        except ApplicationError as error:
            assert error.status == 409 and error.code == "handoff_token_conflict"
            assert isinstance(error.__cause__, HandoffConflict)
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(submit, ["first request", "different request"]))
    assert sum(receipt is not None for receipt in receipts) == 1
    assert len(repo.list_tasks()) == 2


def test_override_preserves_context_and_rejects_oversize(services):
    service, repo, _ = services
    receipt = service.create("source-session", payload(instruction_override="Continue with tests"), "operator")
    instruction = repo.get_task(receipt["task_id"])["instruction"]
    assert "Continue with tests" in instruction and "Fix bug" in instruction
    assert "测试结果" in instruction and "待核对" in instruction
    assert len(instruction) <= 2000
    with pytest.raises(ApplicationError):
        service.create("source-session", payload(instruction_override="x" * 501), "operator")


def test_unknown_test_numbers_and_unmanaged_state_are_not_facts():
    snapshot = build_handoff_snapshot({"managed": False, "capture_quality": "exact", "current_state": "completed"},
        [], {"test_summary": {"passed": True, "failed": 2**63, "duration_s": float("nan")}})
    assert snapshot["capture_quality"] == "best_effort" and snapshot["current_state"] == "unknown"
    assert snapshot["tests"] == {}


def test_structured_test_results_survive_snapshot_budget():
    summary = {"passed": 12, "failed": 0, "skipped": 2, "errors": 0,
               "framework": "pytest", "duration_s": 1.25}
    snapshot = build_handoff_snapshot({}, [], {"test_summary": summary})
    assert snapshot["tests"] == summary
    assert "pytest" in render_handoff_prompt(snapshot)


def test_latest_complete_message_and_bounded_policy_signals():
    snapshot = build_handoff_snapshot({"handoff_signals": [{"severity": "warn", "reason": "API_KEY=hidden-value"}]},
        [event(1, "assistant_message", {"text": "complete", "is_complete": True}),
         event(2, "assistant_message", {"text": "partial", "is_complete": False})], None)
    assert snapshot["last_conclusion"] == "complete"
    assert "hidden-value" not in json.dumps(snapshot)


def test_redacted_window_preserves_first_goal_and_recent_conclusion(tmp_path):
    from hub.infrastructure.transcript_repository import TranscriptRepository
    repository = TranscriptRepository(tmp_path / "transcript.db")
    repository.init()
    with repository._connect() as conn:
        rows = []
        for sequence in range(1, 1006):
            kind = "user_message" if sequence == 1 else "assistant_message"
            text = "first goal" if sequence == 1 else f"conclusion {sequence}"
            rows.append(("source-session", f"evt_{sequence}", sequence, kind, "structured", "2026-10-11T00:00:00Z",
                         json.dumps({"payload": {"text": text}, "source_record": {"text": "RAW-EXCLUDED"}})))
        conn.executemany("INSERT INTO redacted_events (session_id,event_id,sequence,kind,capture_quality,emitted_at,redacted_json) VALUES (?,?,?,?,?,?,?)", rows)
    events, truncated = repository.read_handoff_events("source-session")
    snapshot = build_handoff_snapshot({"handoff_truncated": truncated}, events, None)
    assert len(events) == 1000 and truncated
    assert snapshot["objective"] == "first goal"
    assert snapshot["last_conclusion"] == "conclusion 1005"
    assert {"event_id": "evt_1005", "sequence": "1005"} in snapshot["source_event_refs"]
    assert "RAW-EXCLUDED" not in json.dumps(events)


def test_browser_formatter_matches_server_and_contract_rejects_unknown_state():
    if not shutil.which("node"):
        pytest.skip("Node needed for browser formatter contract")
    root = Path(__file__).resolve().parents[1]
    snapshot = build_handoff_snapshot({"current_state": "running"},
        [event(1, "user_message", {"text": "任务" * 1000}),
         event(2, "assistant_message", {"text": "状态🙂" * 500})], {"test_summary": {"passed": 2}})
    cases = [{"snapshot": snapshot, "override": override, "expected": render_handoff_prompt(snapshot, override)}
             for override in ("", "继续完成测试🙂")]
    script = """
      import fs from 'node:fs';
      import assert from 'node:assert/strict';
      import {renderHandoffPrompt} from './frontend/assets/views/session-handoff.js';
      import {parseHandoffPreview} from './frontend/assets/api/contracts.js';
      const cases = JSON.parse(fs.readFileSync(0, 'utf8'));
      for (const item of cases) assert.equal(renderHandoffPrompt(item.snapshot, item.override), item.expected);
      const valid = {snapshot:cases[0].snapshot,prompt:cases[0].expected,source_project:null,can_create:false,targets:[]};
      assert.equal(parseHandoffPreview(valid).snapshot.current_state, 'running');
      assert.throws(() => parseHandoffPreview({...valid,snapshot:{...valid.snapshot,current_state:'guessed'}}));
      assert.throws(() => parseHandoffPreview({...valid,nonce:'internal'}));
    """
    result = subprocess.run(["node", "--input-type=module", "-e", script], input=json.dumps(cases),
                            cwd=root, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
