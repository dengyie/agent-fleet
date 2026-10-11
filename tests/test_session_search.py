from __future__ import annotations

import sqlite3
import base64
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hub.application.session_search_service import (
    SessionSearchService,
    SessionSearchServiceError,
)
from hub.infrastructure.session_search_repository import (
    SessionSearchRepository,
    SessionSearchRepositoryError,
)


def event(
    *,
    session_id: str = "session-1",
    event_id: str = "event-1",
    sequence: int = 1,
    kind: str = "assistant_message",
    text: str = "部署状态已完成",
    emitted_at: str = "2026-10-11T00:00:00Z",
    machine_id: str = "machine-1",
    project_id: str = "project-1",
    agent_family: str = "codex",
) -> dict:
    payload = {"text": text} if kind.endswith("message") else {
        "tool_name": "pytest",
        "arguments": text,
        "result": "passed",
    }
    return {
        "event_id": event_id,
        "session_id": session_id,
        "sequence": sequence,
        "kind": kind,
        "emitted_at": emitted_at,
        "machine_id": machine_id,
        "project_id": project_id,
        "agent_family": agent_family,
        "payload": payload,
    }


class SessionSearchRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.repo = SessionSearchRepository(
            Path(self.tempdir.name) / "search.db", enabled=True
        )
        self.repo.init()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_incremental_index_duplicate_and_raw_exclusion(self) -> None:
        self.assertTrue(self.repo.index_event(event(text="安全消息")))
        self.assertFalse(self.repo.index_event(event(text="另一份消息")))
        self.assertFalse(
            self.repo.index_event(
                event(event_id="raw", kind="source_record", text="原始秘密")
            )
        )
        self.assertEqual(self.repo.search("安全")["items"][0]["snippet"], "安全消息")
        self.assertEqual(self.repo.search("秘密")["items"], [])

    def test_tool_fields_are_searchable_but_secrets_and_paths_are_not(self) -> None:
        self.assertTrue(
            self.repo.index_event(
                event(
                    event_id="tool-1",
                    kind="tool_call",
                    text="pytest /Users/mango/project token=super-secret",
                )
            )
        )
        self.assertEqual(self.repo.search("pytest")["items"][0]["kind"], "tool_call")
        self.assertEqual(self.repo.search("super-secret")["items"], [])
        self.assertEqual(self.repo.search("/Users")["items"], [])

    def test_cjk_two_character_search_and_filters(self) -> None:
        self.repo.index_event(event(text="修复登录流程", machine_id="machine-1"))
        self.repo.index_event(
            event(
                event_id="event-2",
                sequence=2,
                text="修复缓存流程",
                machine_id="machine-2",
            )
        )
        result = self.repo.search("登录", machine_id="machine-1")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["highlight"], [[2, 4]])
        self.assertEqual(self.repo.search("登录", machine_id="machine-2")["items"], [])

    def test_cursor_pagination_is_stable_and_bounded(self) -> None:
        for index in range(3):
            self.repo.index_event(
                event(
                    event_id=f"event-{index}",
                    sequence=index + 1,
                    text=f"release marker {index}",
                    emitted_at=f"2026-10-11T00:00:0{index}Z",
                )
            )
        first = self.repo.search("release", limit=2)
        self.assertEqual(len(first["items"]), 2)
        self.assertIsNotNone(first["next_cursor"])
        second = self.repo.search("release", limit=2, after=first["next_cursor"])
        self.assertEqual(len(second["items"]), 1)
        self.assertIsNone(second["next_cursor"])
        with self.assertRaises(SessionSearchRepositoryError) as ctx:
            self.repo.search("x")
        self.assertEqual(ctx.exception.code, "invalid_query")

    def test_gate_is_closed_without_touching_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "disabled.db"
            repo = SessionSearchRepository(path, enabled=False)
            repo.init()
            self.assertFalse(path.exists())
            with self.assertRaises(SessionSearchRepositoryError) as ctx:
                repo.search("安全")
            self.assertEqual(ctx.exception.code, "search_disabled")

    def test_invalid_filter_fails_closed(self) -> None:
        with self.assertRaises(SessionSearchRepositoryError) as ctx:
            self.repo.search("安全", machine_id="/etc/credentials")
        self.assertEqual(ctx.exception.code, "invalid_filter")

    def test_fts_initialization_failure_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = SessionSearchRepository(Path(directory) / "broken.db", enabled=True)
            with patch.object(repo, "_connect", side_effect=sqlite3.OperationalError("fts details")):
                with self.assertRaises(SessionSearchRepositoryError) as ctx:
                    repo.init()
            self.assertEqual(ctx.exception.code, "search_unavailable")
            self.assertIsInstance(ctx.exception.__cause__, sqlite3.OperationalError)

    def test_retention_and_deletion_remove_every_index(self) -> None:
        self.repo.index_event(event(text="保留内容", emitted_at="2026-10-10T00:00:00Z"))
        self.repo.index_event(event(event_id="event-2", text="保留内容", sequence=2))
        self.assertEqual(self.repo.purge_before("2026-10-10T20:00:00-04:00"), 1)
        self.assertEqual(len(self.repo.search("保留")["items"]), 1)
        self.assertEqual(self.repo.delete_session("session-1"), 1)
        self.assertEqual(self.repo.search("保留")["items"], [])
        with sqlite3.connect(self.repo.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM search_fts").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM search_bigrams").fetchone()[0], 0)

    def test_offsets_and_fractional_timestamps_sort_by_instant(self) -> None:
        self.repo.index_event(event(event_id="early", text="matching", emitted_at="2026-10-11T08:00:00+08:00"))
        self.repo.index_event(event(event_id="later", sequence=2, text="matching", emitted_at="2026-10-11T00:00:00.100Z"))
        first = self.repo.search("matching", limit=1)
        self.assertEqual(first["items"][0]["sequence"], 2)
        second = self.repo.search("matching", limit=1, after=first["next_cursor"])
        self.assertEqual(second["items"][0]["sequence"], 1)
        self.assertEqual(second["items"][0]["emitted_at"], "2026-10-11T00:00:00.000000Z")

    def test_cursor_is_not_truncated_and_invalid_input_is_bounded(self) -> None:
        for index in range(2):
            self.repo.index_event(event(session_id="s" * 250, event_id=str(index) * 250,
                                        sequence=index + 1, text="matching"))
        first = self.repo.search("matching", limit=1)
        self.assertGreater(len(first["next_cursor"]), 512)
        self.assertEqual(len(self.repo.search("matching", after=first["next_cursor"])["items"]), 1)
        for cursor in ("!!!", base64.urlsafe_b64encode(json.dumps(["no", True, "s", "e"]).encode()).decode()):
            with self.assertRaises(SessionSearchRepositoryError) as ctx:
                self.repo.search("matching", after=cursor)
            self.assertEqual(ctx.exception.code, "invalid_cursor")

    def test_casefold_two_character_and_unicode_highlight(self) -> None:
        self.repo.index_event(event(text="ABcd"))
        self.assertEqual(self.repo.search("ab")["items"][0]["highlight"], [[0, 2]])
        self.repo.index_event(event(event_id="event-2", text="😀登录完成"))
        self.assertEqual(self.repo.search("登录")["items"][0]["highlight"], [[1, 3]])
        self.assertEqual(self.repo._snippet("Straße works", "works"), ("Straße works", [[7, 12]]))

    def test_full_redaction_rules_and_integer_bounds(self) -> None:
        self.repo.index_event(event(text="Bearer abcdefsecret\npassword=correct horse battery staple\n正常内容"))
        for query in ("abcdefsecret", "horse", "staple"):
            self.assertEqual(self.repo.search(query)["items"], [])
        self.repo.index_event(event(event_id="large", sequence=(1 << 53) + 1, text="大号序列"))
        self.assertEqual(self.repo.search("序列")["items"][0]["sequence"], str((1 << 53) + 1))
        with self.assertRaises(SessionSearchRepositoryError):
            self.repo.index_event(event(event_id="overflow", sequence=1 << 63))
        with self.assertRaises(SessionSearchRepositoryError):
            self.repo.search("正常", from_time="2026-10-12T00:00:00Z", to_time="2026-10-11T00:00:00Z")

    def test_invalid_unicode_cannot_break_optional_ingest(self) -> None:
        self.assertTrue(self.repo.index_event(event(text="有效\ud800内容")))
        self.assertEqual(self.repo.search("有效")["items"][0]["snippet"], "有效内容")
        with self.assertRaises(SessionSearchRepositoryError) as ctx:
            self.repo.search("查\ud800")
        self.assertEqual(ctx.exception.code, "invalid_query")

    def test_query_budget_does_not_latch_repository_offline(self) -> None:
        for index in range(60):
            self.repo.index_event(event(event_id=f"event-{index}", sequence=index + 1, text="中文内容"))
        with patch("hub.infrastructure.session_search_repository.MAX_QUERY_STEPS", 0):
            with self.assertRaises(SessionSearchRepositoryError) as ctx:
                self.repo.search("中文")
            self.assertEqual(ctx.exception.code, "search_unavailable")
        self.assertTrue(self.repo.available)
        self.assertEqual(len(self.repo.search("中文")["items"]), 50)


class SessionSearchServiceTests(unittest.TestCase):
    def test_index_hook_isolates_optional_index_failure(self) -> None:
        class BrokenRepository:
            enabled = True
            available = False

            def index_event(self, *args, **kwargs):
                raise SessionSearchRepositoryError("search_unavailable")

        result = SessionSearchService(BrokenRepository()).index_event(event())
        self.assertEqual(result, {"indexed": False, "code": "search_unavailable"})

    def test_query_exposes_stable_service_error(self) -> None:
        class DisabledRepository:
            enabled = False

            def search(self, *args, **kwargs):
                raise SessionSearchRepositoryError("search_disabled")

        with self.assertRaises(SessionSearchServiceError) as ctx:
            SessionSearchService(DisabledRepository()).search("安全")
        self.assertEqual(ctx.exception.code, "search_disabled")
        self.assertEqual(ctx.exception.status, 503)
        self.assertIsInstance(ctx.exception.__cause__, SessionSearchRepositoryError)

    def test_query_aliases_match_public_filter_names(self) -> None:
        class RecordingRepository:
            def search(self, query, **filters):
                return {"query": query, "filters": filters}

        result = SessionSearchService(RecordingRepository()).search(
            "安全", machine="machine-1", project="project-1", agent="codex",
            **{"from": "2026-10-01T00:00:00Z", "to": "2026-10-11T00:00:00Z"},
        )
        self.assertEqual(result["filters"]["machine_id"], "machine-1")
        self.assertEqual(result["filters"]["project_id"], "project-1")
        self.assertEqual(result["filters"]["agent_family"], "codex")


class SessionSearchApiTests(unittest.TestCase):
    def setUp(self) -> None:
        from hub import events, state, task_store
        from hub.bootstrap import create_app
        from hub.config import FleetConfig

        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.previous = (events.EVENT_LOG, state.STATE_DIR, task_store.DB_PATH)
        self.addCleanup(self.restore_globals)
        root = Path(self.tempdir.name)
        self.app = create_app(FleetConfig.from_root(
            root, ingest_token="ingest-test", dev_operator="operator@example.com",
            session_repositories_enabled=True, session_search_enabled=True,
            session_encryption_raw=b"k" * 32,
        ))
        self.client = self.app.test_client()

    def restore_globals(self) -> None:
        from hub import events, state, task_store
        events.EVENT_LOG, state.STATE_DIR, task_store.DB_PATH = self.previous

    def ingest(self, text: str = "上线登录完成", seq: int = 1) -> dict:
        row = event(event_id=f"evt-{seq}", sequence=seq, text=text)
        row.pop("agent_family")
        row.pop("project_id")
        row.update(schema_version=1, stream_id="stream-1", capture_quality="structured",
                   source="bridge", process_group_id="group-1", attempt_id="attempt-1")
        response = self.client.post("/api/session-events", json=[row],
                                    headers={"X-Agent-Fleet-Token": "ingest-test"})
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()

    def test_real_ingest_search_operator_auth_and_v1(self) -> None:
        self.ingest("上线登录完成\nBearer unrecoverablesecret")
        result = self.client.get("/api/session-search?q=登录&machine=machine-1")
        self.assertEqual(result.status_code, 200, result.get_json())
        self.assertEqual(result.get_json()["items"][0]["sequence"], 1)
        self.assertEqual(self.client.get("/api/v1/session-search?q=登录").status_code, 200)
        self.assertEqual(self.client.get("/api/session-search?q=unrecoverablesecret").get_json()["items"], [])
        self.assertEqual(self.client.get("/api/session-search?q=登录", headers={"X-Agent-Fleet-Token": "ingest-test"}).status_code, 401)
        self.assertEqual(self.client.get("/api/session-search?q=登录&q=安全").status_code, 400)

    def test_failed_index_never_rolls_back_transcript(self) -> None:
        search = self.app.extensions["fleet"]["services"]["session_search"]
        with patch.object(search.repository, "index_event", side_effect=SessionSearchRepositoryError("search_unavailable")):
            result = self.ingest()
        self.assertEqual(result["accepted_through"], 1)
        rows = self.client.get("/api/sessions/session-1/events").get_json()["events"]
        self.assertEqual(len(rows), 1)

    def test_batch_index_failure_queues_remaining_durable_events(self) -> None:
        search = self.app.extensions["fleet"]["services"]["session_search"]
        repository = search.repository
        connect = repository._connect
        failed = {"once": False}

        def transient_once():
            if not failed["once"]:
                failed["once"] = True
                raise sqlite3.OperationalError("transient index failure")
            return connect()

        rows = []
        for sequence in range(1, 4):
            row = event(event_id=f"evt-{sequence}", sequence=sequence,
                        text=f"恢复批次{sequence}")
            row.pop("agent_family")
            row.pop("project_id")
            row.update(schema_version=1, stream_id="stream-1",
                       capture_quality="structured", source="bridge",
                       process_group_id="group-1", attempt_id="attempt-1")
            rows.append(row)

        with patch.object(repository, "_connect", side_effect=transient_once):
            response = self.client.post(
                "/api/session-events", json=rows,
                headers={"X-Agent-Fleet-Token": "ingest-test"})

        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["accepted_through"], 3)
        result = self.client.get("/api/session-search?q=恢复批次3")
        self.assertEqual(result.status_code, 200, result.get_json())
        self.assertEqual([item["sequence"] for item in result.get_json()["items"]], [3])

    def test_index_reads_persisted_projection_instead_of_input(self) -> None:
        sessions = self.app.extensions["fleet"]["services"]["sessions"]
        original = sessions.transcript_repo.read_redacted_events
        def projection(session_id, event_ids):
            rows = original(session_id, event_ids)
            for row in rows:
                row["payload"] = {"text": "持久内容"}
            return rows
        with patch.object(sessions.transcript_repo, "read_redacted_events", side_effect=projection):
            self.ingest("入站内容")
        self.assertEqual(self.client.get("/api/session-search?q=入站").get_json()["items"], [])
        self.assertEqual(len(self.client.get("/api/session-search?q=持久").get_json()["items"]), 1)

    def test_raw_retention_does_not_delete_redacted_search(self) -> None:
        self.ingest()
        sessions = self.app.extensions["fleet"]["services"]["sessions"]
        sessions.transcript_repo.purge_expired_raw("2099-01-01T00:00:00Z")
        self.assertEqual(len(self.client.get("/api/session-search?q=登录").get_json()["items"]), 1)

    def test_project_and_agent_resolve_from_existing_attempt(self) -> None:
        search = self.app.extensions["fleet"]["services"]["session_search"]
        task, _ = search.task_repository.create_task(machine="machine-1", agent_type="codex",
            project="project-1", instruction="test", requested_by="operator@example.com", client_token="search-task")
        sessions = self.app.extensions["fleet"]["services"]["sessions"]
        row = event(text="项目内容")
        row.pop("agent_family")
        row.pop("project_id")
        row.update(schema_version=1, stream_id="stream-1", capture_quality="structured",
                   source="bridge", process_group_id="group-1", attempt_id=task["attempt_id"])
        sessions.ingest_events([row])
        result = self.client.get("/api/session-search?q=项目&project=project-1&agent=codex")
        self.assertEqual(result.status_code, 200, result.get_json())
        self.assertEqual(len(result.get_json()["items"]), 1)

    def test_default_off_and_missing_fts_leave_ingest_available(self) -> None:
        from hub.bootstrap import create_app
        from hub.config import FleetConfig

        root = Path(self.tempdir.name) / "gated"
        disabled = create_app(FleetConfig.from_root(root,
            session_repositories_enabled=True, dev_operator="operator@example.com"))
        self.assertFalse((root / "var/sessions/search.db").exists())
        self.assertEqual(disabled.test_client().get("/api/session-search?q=登录").get_json()["error"], "search_disabled")
        broken = SessionSearchRepository(root / "broken.db", enabled=True)
        with patch.object(broken, "_connect", side_effect=sqlite3.OperationalError("fts unavailable")):
            unavailable = create_app(FleetConfig.from_root(root,
                session_repositories_enabled=True, session_search_enabled=True,
                dev_operator="operator@example.com"), repositories={"session_search": broken})
        self.assertEqual(unavailable.config["SESSION_SEARCH_DIAGNOSTIC"], "search_unavailable")
        response = unavailable.test_client().get("/api/session-search?q=登录")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["items"], [])
        self.assertEqual(unavailable.test_client().get("/api/sessions").status_code, 200)

    def test_search_does_not_require_task_store(self) -> None:
        from hub.bootstrap import create_app
        from hub.config import FleetConfig

        app = create_app(FleetConfig.from_root(Path(self.tempdir.name) / "no-tasks",
            session_repositories_enabled=True, session_search_enabled=True,
            tasks_enabled=False, dev_operator="operator@example.com"))
        sessions = app.extensions["fleet"]["services"]["sessions"]
        row = event(text="独立搜索")
        row.pop("agent_family")
        row.pop("project_id")
        row.update(schema_version=1, stream_id="stream-1", capture_quality="structured",
                   source="bridge", process_group_id="group-1", attempt_id="attempt-1")
        self.assertEqual(sessions.ingest_events([row])["accepted_through"], 1)
        result = app.test_client().get("/api/session-search?q=独立")
        self.assertEqual(len(result.get_json()["items"]), 1)


class SessionSearchFrontendContractTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_parser_bounds_and_large_sequence(self) -> None:
        path = Path(__file__).resolve().parents[1] / "frontend/assets/api/contracts.js"
        script = """
const {parseSessionSearch, ContractError} = await import(process.env.SEARCH_CONTRACTS_URL);
const assert = await import('node:assert/strict');
const row = {session_id:'s-1', sequence:'9007199254740993', kind:'assistant_message',
 snippet:'😀登录', highlight:[[1,3]], machine_id:'node', project_id:null, agent_family:'codex', emitted_at:'2026-10-11T00:00:00Z'};
assert.equal(parseSessionSearch({items:[row],next_cursor:null}).items[0].sequence, row.sequence);
for (const patch of [{highlight:[[0,8]]},{highlight:[[1,3],[0,1]]},{snippet:'a'.repeat(401)},
 {sequence:9007199254740993},{kind:'source_record'},{token:'forbidden'}]) {
 assert.throws(() => parseSessionSearch({items:[{...row,...patch}],next_cursor:null}), ContractError);
}
assert.throws(() => parseSessionSearch({items:Array(51).fill(row),next_cursor:null}), ContractError);
"""
        result = subprocess.run(["node", "--input-type=module", "-e", script],
                                env={**os.environ, "SEARCH_CONTRACTS_URL": path.as_uri()},
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
