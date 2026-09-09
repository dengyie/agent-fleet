"""tests/test_task_api.py — Phase 2 任务 API + 认证域守卫测试

- AuthDecoratorTests  —— decorator 单元级守卫（Task 4）
- AuthIsolationTests —— 路由级隔离：ingest/runner/operator 三域互不越权（Task 4 写，
                        Task 5 挂载 tasks/commands 蓝图后转绿）
- TaskCrudApiTests   —— operator 任务 API（创建幂等/校验/列表/详情/取消/重试，Task 5）
"""
import json
import tempfile
import unittest
from pathlib import Path

from flask import Flask, g, jsonify

from hub import auth
from hub import events
from hub import state as store
from hub import task_store


class TaskApiTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.old_state_dir = store.STATE_DIR
        self.old_event_log = events.EVENT_LOG
        self.old_db = task_store.DB_PATH
        store.STATE_DIR = self.temp_dir
        events.EVENT_LOG = self.temp_dir / "events.jsonl"
        task_store.DB_PATH = self.temp_dir / "fleet.db"
        task_store.init_db()
        # 让 mac-local 在线
        store.save_snapshot("mac-local", {
            "machine": "mac-local", "source": "ingest", "reachable": True,
            "agents": {}, "system": {},
        })
        from hub import web
        self.app = web.make_app(
            ingest_token="ingest-secret",
            dev_operator="op@example.com",
            runner_credentials={"mac-local": "runner-secret"},
            project_whitelist={"mac-local": ["agent-fleet"]},
        )
        self.client = self.app.test_client()

    def tearDown(self):
        store.STATE_DIR = self.old_state_dir
        events.EVENT_LOG = self.old_event_log
        task_store.DB_PATH = self.old_db


class AuthIsolationTests(TaskApiTestBase):
    def test_ingest_token_cannot_create_task(self):
        resp = self.client.post("/api/tasks", json={
            "machine": "mac-local", "agent_type": "codex",
            "project": "agent-fleet", "instruction": "x",
        }, headers={"X-Agent-Fleet-Token": "ingest-secret"})
        self.assertEqual(resp.status_code, 401)

    def test_runner_credential_cannot_create_task(self):
        resp = self.client.post("/api/tasks", json={
            "machine": "mac-local", "agent_type": "codex",
            "project": "agent-fleet", "instruction": "x",
        }, headers={"X-Runner-Credential": "mac-local:runner-secret"})
        self.assertEqual(resp.status_code, 401)

    def test_operator_cannot_poll_commands(self):
        resp = self.client.post("/api/commands/poll", json={},
                                headers={"Cf-Access-Authenticated-User-Email": "op@example.com"})
        self.assertEqual(resp.status_code, 403)

    def test_ingest_token_cannot_poll_commands(self):
        resp = self.client.post("/api/commands/poll", json={},
                                headers={"X-Agent-Fleet-Token": "ingest-secret"})
        self.assertEqual(resp.status_code, 403)

    def test_runner_credential_machine_mismatch_rejected(self):
        resp = self.client.post("/api/commands/poll", json={},
                                headers={"X-Runner-Credential": "mac-local:wrong"})
        self.assertEqual(resp.status_code, 403)

    def test_operator_without_any_identity_401(self):
        app = self.app
        app.config["DEV_OPERATOR"] = None  # 关闭开发兜底
        resp = self.client.post("/api/tasks", json={
            "machine": "mac-local", "agent_type": "codex",
            "project": "agent-fleet", "instruction": "x"})
        self.assertEqual(resp.status_code, 401)


class TaskStoreDegradationTests(unittest.TestCase):
    """make_app 的 TASKS_ENABLED 降级：任务 API 503，观测路由不受影响。

    所有存储（state / events / SQLite）都指向临时目录，不得触碰仓库真实 state/。
    """

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.old_state_dir = store.STATE_DIR
        self.old_event_log = events.EVENT_LOG
        self.old_db = task_store.DB_PATH
        store.STATE_DIR = self.temp_dir
        events.EVENT_LOG = self.temp_dir / "events.jsonl"
        task_store.DB_PATH = self.temp_dir / "fleet.db"

    def tearDown(self):
        store.STATE_DIR = self.old_state_dir
        events.EVENT_LOG = self.old_event_log
        task_store.DB_PATH = self.old_db

    def test_task_api_503_and_observe_still_works(self):
        from hub import web
        # 记录仓库真实 DB 指纹：本测试不得触碰 state/fleet.db（缺失时视为不存在）
        repo_db = store.FLEET_HOME / "state" / "fleet.db"
        before = (repo_db.exists(), repo_db.stat().st_mtime_ns, repo_db.stat().st_size) \
            if repo_db.exists() else (False, None, None)
        app = web.make_app(ingest_token="secret", dev_operator="op@example.com")
        client = app.test_client()
        # init_db 只在临时路径落库，仓库 state/fleet.db 不受影响
        self.assertTrue((self.temp_dir / "fleet.db").exists())
        # 模拟任务存储不可用（降级守卫命中），任务 API 503
        app.config["TASKS_ENABLED"] = False
        self.assertEqual(client.post("/api/tasks", json={
            "machine": "mac-local", "agent_type": "codex",
            "project": "agent-fleet", "instruction": "x"}).status_code, 503)
        # 观测链路不受影响：公共 status 200、公开 mask 404、公共 events 200
        self.assertEqual(client.get("/api/status").status_code, 200)
        self.assertEqual(client.get("/api/machines/ghost").status_code, 404)
        resp = client.get("/api/events")
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.get_json()["events"], list)
        # 仓库真实 DB 未被动过
        after = (repo_db.exists(), repo_db.stat().st_mtime_ns, repo_db.stat().st_size) \
            if repo_db.exists() else (False, None, None)
        self.assertEqual(after, before)


class AgentTypesApiTests(TaskApiTestBase):
    def test_agent_types_endpoint_derived_from_registry(self):
        from agent_profiles import EXECUTABLE_AGENT_TYPES
        resp = self.client.get("/api/agent-types",
                               headers={"Cf-Access-Authenticated-User-Email": "op@example.com"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["agent_types"],
                         list(EXECUTABLE_AGENT_TYPES))
        self.assertIn("pi", resp.get_json()["agent_types"])

    def test_agent_types_endpoint_requires_operator(self):
        resp = self.client.get("/api/agent-types",
                               headers={"X-Agent-Fleet-Token": "ingest-secret"})
        self.assertEqual(resp.status_code, 401)


class TaskCrudApiTests(TaskApiTestBase):
    def _create(self, **kw):
        body = {"machine": "mac-local", "agent_type": "codex",
                "project": "agent-fleet", "instruction": "修复登录页样式"}
        body.update(kw)
        return self.client.post("/api/tasks", json=body)

    def test_create_and_get_task(self):
        resp = self._create()
        self.assertEqual(resp.status_code, 201)
        task = resp.get_json()["task"]
        self.assertEqual(task["state"], "queued")
        got = self.client.get(f"/api/tasks/{task['task_id']}").get_json()
        self.assertEqual(got["task"]["instruction"], "修复登录页样式")
        self.assertEqual(got["task"]["requested_by"], "op@example.com")
        self.assertIsNone(got["task"]["result"])

    def test_create_idempotent_with_client_token(self):
        first = self._create(client_token="web-1")
        second = self._create(client_token="web-1")
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.get_json()["created"])
        self.assertEqual(first.get_json()["task"]["task_id"],
                         second.get_json()["task"]["task_id"])

    def test_validation_errors(self):
        self.assertEqual(self._create(machine="../x").status_code, 400)
        self.assertEqual(self._create(agent_type="bash").status_code, 400)
        self.assertEqual(self._create(project="bad proj!").status_code, 400)
        self.assertEqual(self._create(instruction="").status_code, 400)
        self.assertEqual(self._create(instruction="x" * 2001).status_code, 400)
        self.assertEqual(self._create(project="not-registered").status_code, 400)
        resp = self._create(machine="ghost-machine")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "machine_offline")

    def test_non_object_json_body_returns_400(self):
        # 数组/字符串/数字/布尔/null 等非对象 JSON → 统一 400 invalid_json，绝不 500
        for bad in ([], "str", 42, 3.14, True, None):
            resp = self.client.post("/api/tasks",
                                    data=json.dumps(bad),
                                    content_type="application/json")
            self.assertEqual(resp.status_code, 400, f"bad body={bad!r}")
            self.assertFalse(resp.get_json()["ok"])
            self.assertEqual(resp.get_json()["error"], "invalid_json")
        # 无 body 同样 400 invalid_json
        resp = self.client.post("/api/tasks", data="",
                                content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "invalid_json")

    def test_list_and_filter(self):
        self._create(instruction="任务一")
        self._create(instruction="任务二")
        payload = self.client.get("/api/tasks?machine=mac-local").get_json()
        self.assertEqual(len(payload["tasks"]), 2)
        self.assertEqual(self.client.get("/api/tasks?machine=other").get_json()["tasks"], [])

    def test_cancel_and_retry(self):
        task = self._create().get_json()["task"]
        tid = task["task_id"]
        resp = self.client.post(f"/api/tasks/{tid}/cancel")
        self.assertTrue(resp.get_json()["changed"])
        self.assertEqual(resp.get_json()["task"]["state"], "cancelled")
        resp = self.client.post(f"/api/tasks/{tid}/retry")
        self.assertTrue(resp.get_json()["changed"])
        self.assertEqual(resp.get_json()["task"]["state"], "queued")
        self.assertEqual(self.client.post("/api/tasks/t-missing/cancel").status_code, 404)

    def test_pause_and_continue(self):
        tid = self._create().get_json()["task"]["task_id"]
        resp = self.client.post(f"/api/tasks/{tid}/pause")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["task"]["state"], "paused")
        poll = self.client.post("/api/commands/poll", json={"runner_id": "r1"},
                                headers={"X-Runner-Credential": "mac-local:runner-secret"})
        self.assertIsNone(poll.get_json()["task"])
        resp = self.client.post(f"/api/tasks/{tid}/continue")
        self.assertEqual(resp.get_json()["task"]["state"], "queued")
        poll = self.client.post("/api/commands/poll", json={"runner_id": "r1"},
                                headers={"X-Runner-Credential": "mac-local:runner-secret"})
        self.assertEqual(poll.get_json()["task"]["task_id"], tid)

    def test_pause_succeeded_is_409(self):
        tid = self._create().get_json()["task"]["task_id"]
        lease = self.client.post("/api/commands/poll", json={"runner_id": "r1"},
                                 headers={"X-Runner-Credential": "mac-local:runner-secret"}).get_json()["task"]
        done = self.client.post(
            f"/api/commands/{lease['attempt_id']}/result",
            json={"nonce": lease["nonce"], "exit_code": 0, "log_summary": "ok",
                  "diff_stat": "", "duration_s": 1},
            headers={"X-Runner-Credential": "mac-local:runner-secret"})
        self.assertEqual(done.status_code, 200)
        resp = self.client.post(f"/api/tasks/{tid}/pause")
        self.assertEqual(resp.status_code, 409)

    def test_confirm_gate_blocks_poll_until_confirmed(self):
        tid = self._create(confirm=True).get_json()["task"]["task_id"]
        detail = self.client.get(f"/api/tasks/{tid}").get_json()["task"]
        self.assertEqual(detail["gate"]["state"], "pending")
        poll = self.client.post("/api/commands/poll", json={"runner_id": "r1"},
                                headers={"X-Runner-Credential": "mac-local:runner-secret"})
        self.assertIsNone(poll.get_json()["task"])
        self.assertEqual(self.client.post(f"/api/tasks/{tid}/confirm").status_code, 200)
        poll = self.client.post("/api/commands/poll", json={"runner_id": "r1"},
                                headers={"X-Runner-Credential": "mac-local:runner-secret"})
        self.assertEqual(poll.get_json()["task"]["task_id"], tid)

    def test_reject_gate_cancels(self):
        tid = self._create(confirm=True).get_json()["task"]["task_id"]
        resp = self.client.post(f"/api/tasks/{tid}/reject")
        self.assertEqual(resp.get_json()["task"]["state"], "cancelled")

    def test_diff_endpoint_and_legacy_result_without_patch(self):
        tid = self._create().get_json()["task"]["task_id"]
        self.assertEqual(self.client.get(f"/api/tasks/{tid}/diff").status_code, 404)
        lease = self.client.post("/api/commands/poll", json={"runner_id": "r1"},
                                 headers={"X-Runner-Credential": "mac-local:runner-secret"}).get_json()["task"]
        self.client.post(
            f"/api/commands/{lease['attempt_id']}/result",
            json={"nonce": lease["nonce"], "exit_code": 0, "log_summary": "ok",
                  "diff_stat": "1 file", "duration_s": 1,
                  "diff_patch": "diff --git a/x b/x\n+hello",
                  "test_summary": {"framework": "pytest", "passed": 1, "failed": 0,
                                   "skipped": 0, "errors": 0, "failed_names": []}},
            headers={"X-Runner-Credential": "mac-local:runner-secret"})
        detail = self.client.get(f"/api/tasks/{tid}").get_json()["task"]["result"]
        self.assertTrue(detail["has_diff_patch"])
        self.assertNotIn("diff_patch", detail)
        self.assertEqual(detail["test_summary"]["passed"], 1)
        diff = self.client.get(f"/api/tasks/{tid}/diff").get_json()
        self.assertIn("+hello", diff["diff_patch"])

    def test_new_task_routes_require_operator(self):
        tid = self._create().get_json()["task"]["task_id"]
        for path in (f"/api/tasks/{tid}/pause", f"/api/tasks/{tid}/continue",
                     f"/api/tasks/{tid}/confirm", f"/api/tasks/{tid}/reject"):
            resp = self.client.post(path, headers={"X-Agent-Fleet-Token": "ingest-secret"})
            self.assertEqual(resp.status_code, 401, path)
        self.assertEqual(
            self.client.get(f"/api/tasks/{tid}/diff",
                            headers={"X-Agent-Fleet-Token": "ingest-secret"}).status_code,
            401)

    def test_public_task_omits_attempt_id_and_client_token(self):
        task = self._create(client_token="secret-token").get_json()["task"]
        self.assertNotIn("attempt_id", task)
        self.assertNotIn("client_token", task)
        # 列表响应同样不泄露内部字段
        listed = self.client.get("/api/tasks").get_json()["tasks"][0]
        self.assertNotIn("attempt_id", listed)
        self.assertNotIn("client_token", listed)

    def test_detail_includes_result_when_available(self):
        tid = self._create().get_json()["task"]["task_id"]
        # 直接写一条结果，模拟 runner 已完成（不经完整 lease 流）
        conn = task_store._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO results (attempt_id, task_id, exit_code, log_summary,"
                " diff_stat, duration_s, finished_at) VALUES (?,?,?,?,?,?,?)",
                ("att-1", tid, 0, "ok", "1 file", 3.2, "2026-08-20T00:00:00Z"))
            conn.execute("UPDATE tasks SET state='succeeded' WHERE task_id=?", (tid,))
            conn.execute("COMMIT")
        finally:
            conn.close()
        detail = self.client.get(f"/api/tasks/{tid}").get_json()["task"]
        self.assertEqual(detail["result"]["log_summary"], "ok")
        self.assertNotIn("attempt_id", detail)
        # 结果对象不泄露内部 attempt_id / task_id（foreign key 不外泄）
        self.assertNotIn("attempt_id", detail["result"])
        self.assertNotIn("task_id", detail["result"])
        # 只暴露用户可读字段
        self.assertEqual(set(detail["result"].keys()),
                         {"exit_code", "log_summary", "diff_stat",
                          "duration_s", "finished_at"})

    def test_public_task_omits_session_id_when_unbound(self):
        task = self._create().get_json()["task"]
        self.assertNotIn("session_id", task)
        listed = self.client.get("/api/tasks").get_json()["tasks"][0]
        self.assertNotIn("session_id", listed)
        detail = self.client.get(f"/api/tasks/{task['task_id']}").get_json()["task"]
        self.assertNotIn("session_id", detail)


class CommandFlowApiTests(TaskApiTestBase):
    RUNNER = {"X-Runner-Credential": "mac-local:runner-secret"}

    def _create_task(self, instruction="跑测试"):
        return self.client.post("/api/tasks", json={
            "machine": "mac-local", "agent_type": "codex",
            "project": "agent-fleet", "instruction": instruction}).get_json()["task"]

    def test_full_task_flow(self):
        task = self._create_task()
        # poll 领取
        resp = self.client.post("/api/commands/poll", json={"runner_id": "r1"},
                                headers=self.RUNNER)
        leased = resp.get_json()["task"]
        self.assertEqual(leased["task_id"], task["task_id"])
        self.assertEqual(leased["lease_ttl_s"], 300)
        self.assertTrue(leased["nonce"])
        # 队列已空
        resp = self.client.post("/api/commands/poll", json={}, headers=self.RUNNER)
        self.assertIsNone(resp.get_json()["task"])
        # heartbeat（带日志行）
        resp = self.client.post(f"/api/commands/{leased['attempt_id']}/heartbeat",
                                json={"nonce": leased["nonce"],
                                      "log_lines": ["start", "running tests"]},
                                headers=self.RUNNER)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["task_id"], task["task_id"])
        # result
        resp = self.client.post(f"/api/commands/{leased['attempt_id']}/result",
                                json={"nonce": leased["nonce"], "exit_code": 0,
                                      "log_summary": "ok", "diff_stat": "1 file",
                                      "duration_s": 3.2},
                                headers=self.RUNNER)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["state"], "succeeded")
        # 任务详情反映终态与结果
        got = self.client.get(f"/api/tasks/{task['task_id']}").get_json()["task"]
        self.assertEqual(got["state"], "succeeded")
        self.assertEqual(got["result"]["log_summary"], "ok")

    def test_result_with_wrong_nonce_409(self):
        self._create_task()
        leased = self.client.post("/api/commands/poll", json={},
                                  headers=self.RUNNER).get_json()["task"]
        resp = self.client.post(f"/api/commands/{leased['attempt_id']}/result",
                                json={"nonce": "wrong", "exit_code": 0},
                                headers=self.RUNNER)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["error"], "lease_mismatch")

    def test_expired_lease_heartbeat_409_then_requeue(self):
        task = self._create_task()
        leased = self.client.post("/api/commands/poll", json={},
                                  headers=self.RUNNER).get_json()["task"]
        task_store.expire_leases(now=__import__("time").time() + 301)
        resp = self.client.post(f"/api/commands/{leased['attempt_id']}/heartbeat",
                                json={"nonce": leased["nonce"]}, headers=self.RUNNER)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["error"], "lease_expired")
        # 任务已回 queued，可被重新领取
        leased2 = self.client.post("/api/commands/poll", json={},
                                   headers=self.RUNNER).get_json()["task"]
        self.assertEqual(leased2["task_id"], task["task_id"])
        self.assertNotEqual(leased2["attempt_id"], leased["attempt_id"])

    def test_oversized_result_is_truncated(self):
        self._create_task()
        leased = self.client.post("/api/commands/poll", json={},
                                  headers=self.RUNNER).get_json()["task"]
        resp = self.client.post(f"/api/commands/{leased['attempt_id']}/result",
                                json={"nonce": leased["nonce"], "exit_code": 0,
                                      "log_summary": "x" * 20000,
                                      "diff_stat": "y" * 8000, "duration_s": 1},
                                headers=self.RUNNER)
        self.assertEqual(resp.status_code, 200)
        got = self.client.get(
            f"/api/tasks/{leased['task_id']}").get_json()["task"]["result"]
        self.assertEqual(len(got["log_summary"]), 10240)
        self.assertEqual(len(got["diff_stat"]), 5120)

    def test_commands_require_runner_auth(self):
        self.assertEqual(self.client.post("/api/commands/poll", json={}).status_code, 403)
        self.assertEqual(self.client.post(
            "/api/commands/abc/heartbeat", json={"nonce": "x"}).status_code, 403)

    def test_non_object_json_body_returns_400(self):
        # 数组/字符串/数字等非对象 JSON → 统一 400 invalid_json，绝不 500（与 tasks 域一致）
        for bad in ([], "str", 42, 3.14, True):
            body = json.dumps(bad)
            resp = self.client.post("/api/commands/poll", data=body,
                                    content_type="application/json", headers=self.RUNNER)
            self.assertEqual(resp.status_code, 400, f"poll bad body={bad!r}")
            self.assertFalse(resp.get_json()["ok"])
            self.assertEqual(resp.get_json()["error"], "invalid_json")
        # heartbeat/result 同样 400 而非 500
        for path in ("/api/commands/abc/heartbeat", "/api/commands/abc/result"):
            resp = self.client.post(path, data=json.dumps([]),
                                    content_type="application/json", headers=self.RUNNER)
            self.assertEqual(resp.status_code, 400, path)
            self.assertEqual(resp.get_json()["error"], "invalid_json")

    def test_heartbeat_log_lines_truncated_and_capped(self):
        task = self._create_task()
        leased = self.client.post("/api/commands/poll", json={},
                                  headers=self.RUNNER).get_json()["task"]
        long_line = "x" * 900
        lines = [long_line] * 60
        resp = self.client.post(
            f"/api/commands/{leased['attempt_id']}/heartbeat",
            json={"nonce": leased["nonce"], "log_lines": lines}, headers=self.RUNNER)
        self.assertEqual(resp.status_code, 200)
        # cap 50 条、每条截断 500：最后一条是第 50 条且长度 500
        recent = events.read_recent(99999)
        logs = [e for e in recent if e["event"] == "task_log"]
        self.assertLessEqual(len(logs), 50)
        self.assertEqual(len(logs[0]["extra"]["line"]), 500)
        self.assertEqual(logs[0]["extra"]["task_id"], task["task_id"])


class AuthDecoratorTests(unittest.TestCase):
    def _app(self, **config):
        app = Flask(__name__)
        app.config.update(config)

        @app.route("/op", methods=["POST"])
        @auth.require_operator
        def op():
            return jsonify({"ok": True, "operator": g.operator})

        @app.route("/run", methods=["POST"])
        @auth.require_runner
        def run():
            return jsonify({"ok": True, "machine": g.runner_machine})

        @app.route("/guarded")
        @auth.require_task_store
        def guarded():
            return jsonify({"ok": True})
        return app

    def test_operator_cf_header(self):
        client = self._app(DEV_OPERATOR=None).test_client()
        resp = client.post("/op", headers={
            "Cf-Access-Authenticated-User-Email": "op@example.com"})
        self.assertEqual(resp.get_json()["operator"], "op@example.com")

    def test_operator_dev_fallback_and_reject(self):
        self.assertEqual(self._app(DEV_OPERATOR="dev@local").test_client()
                         .post("/op").status_code, 200)
        self.assertEqual(self._app(DEV_OPERATOR=None).test_client()
                         .post("/op").status_code, 401)

    def test_runner_credential(self):
        app = self._app(RUNNER_CREDENTIALS={"mac-local": "s3"})
        client = app.test_client()
        self.assertEqual(client.post("/run", headers={
            "X-Runner-Credential": "mac-local:s3"}).get_json()["machine"], "mac-local")
        self.assertEqual(client.post("/run", headers={
            "X-Runner-Credential": "mac-local:bad"}).status_code, 403)
        self.assertEqual(client.post("/run").status_code, 403)

    def test_operator_cannot_be_impersonated_by_foreign_domain(self):
        # DEV_OPERATOR 已配置，但请求携带其他域的凭据头时不得回退成 operator
        app = self._app(DEV_OPERATOR="dev@local",
                        RUNNER_CREDENTIALS={"mac-local": "s3"})
        client = app.test_client()
        self.assertEqual(client.post("/op", headers={
            "X-Agent-Fleet-Token": "ingest-secret"}).status_code, 401)
        self.assertEqual(client.post("/op", headers={
            "X-Runner-Credential": "mac-local:s3"}).status_code, 401)
        # 无任何身份头仍回退 DEV_OPERATOR
        self.assertEqual(client.post("/op").status_code, 200)

    def test_operator_runs_with_dev_fallback_only_for_headerless_requests(self):
        self.assertEqual(self._app(DEV_OPERATOR="dev@local").test_client()
                         .post("/op").status_code, 200)

    def test_task_store_guard(self):
        self.assertEqual(self._app(TASKS_ENABLED=False).test_client()
                         .get("/guarded").status_code, 503)
        self.assertEqual(self._app(TASKS_ENABLED=True).test_client()
                         .get("/guarded").status_code, 200)


class TaskPageTests(TaskApiTestBase):
    def test_task_page_renders(self):
        task = self.client.post("/api/tasks", json={
            "machine": "mac-local", "agent_type": "codex",
            "project": "agent-fleet", "instruction": "做个页面"}).get_json()["task"]
        resp = self.client.get(f"/task/{task['task_id']}")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('data-page="task"', html)
        self.assertIn('data-task-id="' + task["task_id"], html)
        self.assertIn("做个页面", html)
        self.assertEqual(self.client.get("/task/t-missing").status_code, 404)

    def test_machine_page_has_task_section_and_create_button(self):
        self.client.post("/api/tasks", json={
            "machine": "mac-local", "agent_type": "codex",
            "project": "agent-fleet", "instruction": "任务甲"})
        html = self.client.get("/machine/mac-local").get_data(as_text=True)
        self.assertIn("task-list", html)
        self.assertIn("任务甲", html)
        self.assertIn("新建任务", html)


class LeaseReconcilerTests(TaskApiTestBase):
    def test_reconcile_once_requeues_and_emits(self):
        import time as _time
        from hub import web
        self.client.post("/api/tasks", json={
            "machine": "mac-local", "agent_type": "codex",
            "project": "agent-fleet", "instruction": "x"})
        leased = self.client.post("/api/commands/poll", json={},
                                  headers={"X-Runner-Credential": "mac-local:runner-secret"}
                                  ).get_json()["task"]
        # 人为过期
        conn = task_store._connect()
        conn.execute("UPDATE leases SET expires_at=? WHERE attempt_id=?",
                     ("2000-01-01T00:00:00Z", leased["attempt_id"]))
        conn.close()
        requeued = web.reconcile_leases_once(now=_time.time())
        self.assertEqual(requeued, [leased["task_id"]])
        task = self.client.get(f"/api/tasks/{leased['task_id']}").get_json()["task"]
        self.assertEqual(task["state"], "queued")
        # 审计可查
        conn = task_store._connect()
        actions = [r[0] for r in conn.execute(
            "SELECT action FROM audit WHERE task_id=?", (leased["task_id"],)).fetchall()]
        conn.close()
        self.assertIn("lease_expired", actions)
        # 回归: 每个被重派任务恰好 emit 一次 task_update(事件写入了已 patch 的临时 EVENT_LOG)
        updates = [e for e in events.read_recent()
                   if e.get("event") == "task_update"
                   and e.get("extra", {}).get("task_id") == leased["task_id"]]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["extra"]["state"], "queued")
        self.assertEqual(updates[0]["event"], "task_update")
        self.assertEqual(updates[0]["extra"]["task_id"], leased["task_id"])


class LifecycleIsolationTests(TaskApiTestBase):
    def test_make_app_does_not_spawn_lifecycle_daemon_threads(self):
        import threading
        from hub import web
        web.make_app(
            ingest_token="ingest-secret",
            dev_operator="op@example.com",
            runner_credentials={"mac-local": "runner-secret"},
            project_whitelist={"mac-local": ["agent-fleet"]},
        )
        names = [t.name for t in threading.enumerate()]
        self.assertNotIn("ingest-reconciler", names)
        self.assertNotIn("lease-reconciler", names)

    def test_start_background_jobs_is_entrypoint_owned_and_stoppable(self):
        import threading
        from hub.bootstrap import start_background_jobs
        jobs = start_background_jobs(self.app)
        self.assertIn("reconciliation", jobs)
        self.assertIn("lease_reconciler", jobs)
        names = [t.name for t in threading.enumerate()]
        self.assertIn("ingest-reconciler", names)
        self.assertIn("lease-reconciler", names)
        jobs["reconciliation"].set()
        jobs["lease_reconciler"].set()


class FailingTaskRepository:
    """Task store whose init/operations raise so task APIs must degrade.

    ``mode`` selects which boundary fails: ``"init"`` (DB unusable at boot),
    ``"ops"`` (DB dies after a working init), or ``"both"``.
    """

    def __init__(self, mode="ops") -> None:
        self.mode = mode
        self.db_path = None

    def init(self):
        if self.mode in ("init", "both"):
            raise OSError("cannot open task database directory")
        # working init: mimic SqliteTaskRepository for non-failing appearance
        return None

    def create_task(self, **kw):
        if self.mode in ("ops", "both"):
            raise OSError("database I/O error during create")
        return {"task_id": "t", "machine": kw.get("machine", ""), "state": "queued"}, True

    def get_task(self, task_id):
        if self.mode == "ops":
            raise OSError("database I/O error during get")
        return None

    def list_tasks(self, machine=None, state=None, limit=50):
        if self.mode == "ops":
            raise OSError("database I/O error during list")
        return []

    def cancel_task(self, task_id, actor):
        if self.mode == "ops":
            raise OSError("database I/O error during cancel")
        return None, False

    def retry_task(self, task_id, actor):
        if self.mode == "ops":
            raise OSError("database I/O error during retry")
        return None, False

    def pause_task(self, task_id, actor, now=None):
        if self.mode == "ops":
            raise OSError("database I/O error during pause")
        return None, False

    def continue_task(self, task_id, actor, now=None):
        if self.mode == "ops":
            raise OSError("database I/O error during continue")
        return None, False

    def confirm_task(self, task_id, actor, now=None):
        if self.mode == "ops":
            raise OSError("database I/O error during confirm")
        return None, False

    def reject_task(self, task_id, actor, now=None):
        if self.mode == "ops":
            raise OSError("database I/O error during reject")
        return None, False

    def lease_task(self, **kw):
        if self.mode == "ops":
            raise OSError("database I/O error during lease")
        return None

    def heartbeat(self, **kw):
        if self.mode == "ops":
            raise OSError("database I/O error during heartbeat")
        return None

    def complete_task(self, **kw):
        if self.mode == "ops":
            raise OSError("database I/O error during complete")
        return None

    def list_result_files(self, task_id):
        if self.mode == "ops":
            raise OSError("database I/O error during list files")
        return []

    def get_result_file(self, task_id, path):
        if self.mode == "ops":
            raise OSError("database I/O error during get file")
        return None

    def audit_action(self, actor, action, task_id, detail=None, now=None):
        if self.mode == "ops":
            raise OSError("database I/O error during audit")
        return None

    def expire_leases(self, now=None):
        if self.mode == "ops":
            raise OSError("database I/O error during expire leases")
        return []

    def expire_tasks(self, now=None):
        if self.mode == "ops":
            raise OSError("database I/O error during expire tasks")
        return []


def make_app_with_failing_task_repository():
    """App whose task store is broken at boot; observation/status stay healthy."""
    return _app_with_repo({"task": FailingTaskRepository(mode="init")})


def make_app_with_broken_task_repository():
    """App whose task store worked at init but fails on every operation."""
    return _app_with_repo({"task": FailingTaskRepository(mode="ops")})


class FailingEventRepository:
    """Event log whose append always raises; reads are empty."""

    def __init__(self) -> None:
        self.events = []

    def append(self, ev):
        raise OSError("event disk full")

    def read_recent(self, limit=50):
        return list(self.events[-limit:])

    def read_since(self, ts, limit=200):
        return []


def make_app_with_failing_event_repository():
    """App whose event persistence always fails; ingest must still succeed."""
    return _app_with_repo({"events": FailingEventRepository()})


def _app_with_repo(repositories, **extra):
    temp_dir = Path(tempfile.mkdtemp())
    old_state = store.STATE_DIR
    old_events = events.EVENT_LOG
    old_db = task_store.DB_PATH
    store.STATE_DIR = temp_dir
    events.EVENT_LOG = temp_dir / "events.jsonl"
    task_store.DB_PATH = temp_dir / "fleet.db"
    from hub.config import FleetConfig
    from hub.bootstrap import create_app
    root = Path(__file__).resolve().parents[1]
    token = extra.pop("ingest_token", "test-only")
    dev = extra.pop("dev_operator", "op@example.com")
    config = FleetConfig.from_root(
        root,
        state_dir=temp_dir,
        hosts_file=root / "hosts.yaml",
        event_log=temp_dir / "events.jsonl",
        task_db=temp_dir / "fleet.db",
        ingest_token=token,
        dev_operator=dev,
        runner_credentials=extra.pop("runner_credentials", None),
        project_whitelist=extra.pop("project_whitelist",
                                    {"mac-local": ["agent-fleet"]}),
    )
    # 让 mac-local 在线（任务创建需要它可达；观察链路也因此在临时快照上工作）
    store.save_snapshot("mac-local", {
        "machine": "mac-local", "source": "ingest", "reachable": True,
        "agents": {}, "system": {},
    })
    app = create_app(config, repositories=repositories)
    app._fleet_test_cleanup = (old_state, old_events, old_db, temp_dir)
    return app


class FailureIsolationTests(unittest.TestCase):
    """Task 11 Step 1: service failure isolation at the HTTP boundary."""

    def make_task_repo_app(self, mode):
        app = make_app_with_failing_task_repository() if mode == "init" \
            else make_app_with_broken_task_repository()
        self.addCleanup(self._restore, app)
        return app.test_client(), app

    def _restore(self, app):
        old_state, old_events, old_db, temp_dir = app._fleet_test_cleanup
        store.STATE_DIR = old_state
        events.EVENT_LOG = old_events
        task_store.DB_PATH = old_db

    def test_task_repository_failure_keeps_status_endpoint_available(self):
        client, app = self.make_task_repo_app("init")
        self.assertEqual(client.get("/api/status").status_code, 200)
        self.assertEqual(client.post("/api/tasks", json={}).status_code, 503)
        self.assertEqual(app.config["TASKS_ENABLED"], False)

    def test_broken_task_repository_degrades_ops_to_503(self):
        client, app = self.make_task_repo_app("ops")
        self.assertEqual(app.config["TASKS_ENABLED"], True)
        # A broken disk mid-session must degrade to a bounded 503, not a 500.
        payload = {"machine": "mac-local", "agent_type": "codex",
                   "project": "agent-fleet", "instruction": "run npm test"}
        self.assertEqual(client.post("/api/tasks", json=payload).status_code, 503)
        self.assertEqual(client.get("/api/tasks").status_code, 503)
        self.assertEqual(client.get("/api/tasks/t-1").status_code, 503)
        self.assertEqual(client.post("/api/tasks/t-1/cancel").status_code, 503)
        # runner ops also degrade with a bounded 503
        app.config["RUNNER_CREDENTIALS"] = {"mac-local": "runner-secret"}
        self.assertEqual(client.post("/api/commands/poll", json={},
                                     headers={"X-Runner-Credential": "mac-local:runner-secret"}
                                     ).status_code, 503)
        # observe/status remains healthy
        self.assertEqual(client.get("/api/status").status_code, 200)
        self.assertEqual(client.get("/api/events").status_code, 200)

    def test_event_repository_failure_does_not_fail_ingest(self):
        app = make_app_with_failing_event_repository()
        self.addCleanup(self._restore, app)
        client = app.test_client()
        response = client.post(
            "/api/ingest", json={"machine": "hk", "agents": {}},
            headers={"X-Agent-Fleet-Token": "test-only"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])

    def test_failing_task_repo_uses_valid_credentials(self):
        # auth matrix isolation must hold even when the task store is down.
        app = make_app_with_broken_task_repository()
        self.addCleanup(self._restore, app)
        client = app.test_client()
        app.config["INGEST_TOKEN"] = "ingest-secret"
        app.config["RUNNER_CREDENTIALS"] = {"mac-local": "runner-secret"}
        # ingest token cannot impersonate operator on /api/tasks (401)
        self.assertEqual(client.post("/api/tasks", json={},
                                     headers={"X-Agent-Fleet-Token": "ingest-secret"}).status_code, 401)


class TaskSessionDeepLinkApiTests(TaskCrudApiTests):
    """Phase 3: public task DTO projects session_id only from existing bindings."""

    def test_public_task_projects_session_id_when_attempt_is_bound(self):
        created = self._create().get_json()["task"]
        tid = created["task_id"]
        conn = task_store._connect()
        try:
            row = conn.execute("SELECT attempt_id FROM tasks WHERE task_id=?",
                               (tid,)).fetchone()
            attempt_id = row["attempt_id"]
        finally:
            conn.close()
        from hub.infrastructure.session_repository import SessionRepository
        session_repo = SessionRepository(self.temp_dir / "sessions-meta.db")
        session_repo.init()
        session_repo.upsert_session({
            "session_id": "sess_bound_1",
            "machine_id": "mac-local",
            "managed": True,
            "process_group_id": "grp_bound_1",
            "attempt_id": attempt_id,
            "capture_quality": "structured",
        })
        self.app.extensions["fleet"]["services"]["tasks"].session_repo = session_repo
        detail = self.client.get(f"/api/tasks/{tid}").get_json()["task"]
        self.assertEqual(detail["session_id"], "sess_bound_1")
        self.assertNotIn("attempt_id", detail)
        listed = self.client.get("/api/tasks").get_json()["tasks"][0]
        self.assertEqual(listed["session_id"], "sess_bound_1")
        self.assertNotIn("attempt_id", listed)

