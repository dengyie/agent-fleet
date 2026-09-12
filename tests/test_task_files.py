"""Bounded task file snapshot: runner ingest + Hub-local operator read.

GET /api/tasks/<id>/files/<path> must never reverse-connect a runner. The
runner attaches a bounded, allowlisted, redacted snapshot to the result POST;
Hub stores it and serves it locally. These tests are the failing evidence
for that protocol (path allowlist, no symlink/`..`, byte/line caps,
redaction, rate limit, audit, operator auth).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hub import events
from hub import state as store
from hub import task_store
from hub.domain.task import public_result
from tools import result_files
from tools import worktree


GITHUB_PAT = "ghp_abcdefghijklmnopqrstuvwxyz123456"


def git(*args, cwd):
    import subprocess
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


class ResultFileDomainTests(unittest.TestCase):
    def test_accepts_relative_source_path(self):
        self.assertEqual(result_files.normalize_path("src/hello.py"), "src/hello.py")

    def test_rejects_traversal_absolute_symlink_and_secrets(self):
        for bad in (
            "../secret.py",
            "/etc/passwd",
            "src/../../etc/passwd",
            ".env",
            "src/.env",
            "id_rsa",
            "credentials.json",
            "foo/bar/baz/qux/too/deep/a.py",
            "src/hello.exe",
            "src/hello.py/../../../etc/passwd",
            "",
            ".",
            "..",
            "src//hello.py",
            "src\\hello.py",
        ):
            with self.assertRaises(result_files.PathRejected, msg=bad):
                result_files.normalize_path(bad)

    def test_bound_content_truncates_bytes_and_lines(self):
        text = "\n".join(f"line-{i}" for i in range(800))
        out, truncated = result_files.bound_content(text, max_bytes=200, max_lines=10)
        self.assertTrue(truncated)
        self.assertLessEqual(len(out.encode("utf-8")), 200)
        self.assertLessEqual(out.count("\n") + (0 if out.endswith("\n") or not out else 1), 10)

    def test_redact_content_strips_token_sigils(self):
        body = f"token = {GITHUB_PAT}\nprint('ok')\n"
        redacted, report = result_files.redact_content(body)
        self.assertNotIn(GITHUB_PAT, redacted)
        self.assertIn("[REDACTED:credential]", redacted)
        self.assertGreaterEqual(report.replaced, 1)


class WorktreeSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.project = self.temp / "proj"
        self.project.mkdir()
        git("init", cwd=self.project)
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit",
            "--allow-empty", "-m", "init", cwd=self.project)

    def test_collects_allowlisted_text_and_skips_symlink_secret_binary(self):
        wt = worktree.create_worktree(self.project, "t-files1")
        (wt / "src").mkdir()
        (wt / "src" / "hello.py").write_text("print('hi')\n")
        (wt / ".env").write_text("SECRET=1\n")
        (wt / "id_rsa").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")
        (wt / "blob.bin").write_bytes(b"\x00\x01\x02\xff")
        (wt / "src" / "link.py").symlink_to(wt / "src" / "hello.py")
        files = result_files.collect_result_files(wt)
        paths = {item["path"] for item in files}
        self.assertEqual(paths, {"src/hello.py"})
        self.assertEqual(files[0]["content"], "print('hi')\n")
        self.assertFalse(files[0]["truncated"])
        worktree.cleanup_worktree(self.project, wt, "t-files1")

    def test_collect_caps_file_count_and_redacts(self):
        wt = worktree.create_worktree(self.project, "t-files2")
        for i in range(20):
            (wt / f"f{i:02d}.py").write_text(f"x = {i}\n")
        (wt / "tok.py").write_text(f"key = {GITHUB_PAT}\n")
        files = result_files.collect_result_files(wt, max_files=8)
        self.assertLessEqual(len(files), 8)
        leaked = [item for item in files if GITHUB_PAT in item["content"]]
        self.assertEqual(leaked, [])
        worktree.cleanup_worktree(self.project, wt, "t-files2")


class TaskFileApiTests(unittest.TestCase):
    RUNNER = {"X-Runner-Credential": "mac-local:runner-secret"}

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.old_state_dir = store.STATE_DIR
        self.old_event_log = events.EVENT_LOG
        self.old_db = task_store.DB_PATH
        store.STATE_DIR = self.temp_dir
        events.EVENT_LOG = self.temp_dir / "events.jsonl"
        task_store.DB_PATH = self.temp_dir / "fleet.db"
        task_store.init_db()
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

    def _create_and_lease(self):
        task = self.client.post("/api/tasks", json={
            "machine": "mac-local", "agent_type": "codex",
            "project": "agent-fleet", "instruction": "改文件",
        }).get_json()["task"]
        leased = self.client.post(
            "/api/commands/poll", json={"runner_id": "r1"},
            headers=self.RUNNER).get_json()["task"]
        return task, leased

    def _complete_with_files(self, leased, files):
        return self.client.post(
            f"/api/commands/{leased['attempt_id']}/result",
            json={
                "nonce": leased["nonce"], "exit_code": 0,
                "log_summary": "ok", "diff_stat": "1 file",
                "duration_s": 1.2,
                "files": files,
            },
            headers=self.RUNNER)

    def test_operator_reads_hub_local_snapshot_after_result(self):
        task, leased = self._create_and_lease()
        resp = self._complete_with_files(leased, [
            {"path": "src/hello.py", "content": "print('hi')\n"},
        ])
        self.assertEqual(resp.status_code, 200)
        listed = self.client.get(f"/api/tasks/{task['task_id']}/files")
        self.assertEqual(listed.status_code, 200)
        body = listed.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["files"][0]["path"], "src/hello.py")
        self.assertNotIn("content", body["files"][0])
        got = self.client.get(f"/api/tasks/{task['task_id']}/files/src/hello.py")
        self.assertEqual(got.status_code, 200)
        payload = got.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["path"], "src/hello.py")
        self.assertEqual(payload["content"], "print('hi')\n")
        self.assertNotIn("attempt_id", payload)
        detail = self.client.get(f"/api/tasks/{task['task_id']}").get_json()["task"]
        self.assertEqual(
            set(detail["result"].keys()),
            {"exit_code", "log_summary", "diff_stat", "duration_s", "finished_at"},
        )

    def test_v1_files_route_reuses_same_surface(self):
        task, leased = self._create_and_lease()
        self._complete_with_files(leased, [
            {"path": "readme.md", "content": "hi\n"},
        ])
        listed = self.client.get(f"/api/v1/tasks/{task['task_id']}/files")
        self.assertEqual(listed.status_code, 200)
        got = self.client.get(f"/api/v1/tasks/{task['task_id']}/files/readme.md")
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.get_json()["content"], "hi\n")

    def test_public_result_still_omits_file_bodies(self):
        result = public_result({
            "exit_code": 0, "log_summary": "ok", "diff_stat": "1",
            "duration_s": 1, "finished_at": "t",
            "files": [{"path": "a.py", "content": "SECRET"}],
        })
        self.assertNotIn("files", result)
        self.assertNotIn("content", json.dumps(result))

    def test_files_require_operator_auth(self):
        task, leased = self._create_and_lease()
        self._complete_with_files(leased, [
            {"path": "a.py", "content": "x = 1\n"},
        ])
        path = f"/api/tasks/{task['task_id']}/files/a.py"
        self.app.config["DEV_OPERATOR"] = None
        self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.get(
            path, headers={"X-Agent-Fleet-Token": "ingest-secret"}).status_code, 401)
        self.assertEqual(self.client.get(
            path, headers=self.RUNNER).status_code, 401)
        listed = f"/api/tasks/{task['task_id']}/files"
        self.assertEqual(self.client.get(listed).status_code, 401)

    def test_rejects_traversal_and_unknown_paths(self):
        task, leased = self._create_and_lease()
        self._complete_with_files(leased, [
            {"path": "src/hello.py", "content": "print(1)\n"},
        ])
        tid = task["task_id"]
        for rel in ("../secret.py", "src/../../etc/passwd", ".env", "missing.py"):
            resp = self.client.get(f"/api/tasks/{tid}/files/{rel}")
            self.assertIn(resp.status_code, (400, 404), rel)
            body = resp.get_json()
            self.assertFalse(body["ok"])
            self.assertNotIn("SECRET", json.dumps(body))
            self.assertNotIn("/etc/passwd", json.dumps(body))

    def test_runner_payload_is_revalidated_and_redacted(self):
        task, leased = self._create_and_lease()
        resp = self._complete_with_files(leased, [
            {"path": "../etc/passwd", "content": "root:x:0:0\n"},
            {"path": ".env", "content": "AWS_SECRET_ACCESS_KEY=abc\n"},
            {"path": "ok.py", "content": f"tok = {GITHUB_PAT}\n"},
            {"path": "skip.bin", "content": "\x00\x01"},
        ])
        self.assertEqual(resp.status_code, 200)
        listed = self.client.get(
            f"/api/tasks/{task['task_id']}/files").get_json()["files"]
        paths = {item["path"] for item in listed}
        self.assertEqual(paths, {"ok.py"})
        got = self.client.get(
            f"/api/tasks/{task['task_id']}/files/ok.py").get_json()
        self.assertNotIn(GITHUB_PAT, got["content"])
        self.assertIn("[REDACTED:credential]", got["content"])
        self.assertTrue(got["redacted"])

    def test_oversized_file_is_truncated(self):
        task, leased = self._create_and_lease()
        payload = {"path": "big.py", "content": "x" * 80000}
        self._complete_with_files(leased, [payload])
        got = self.client.get(
            f"/api/tasks/{task['task_id']}/files/big.py").get_json()
        self.assertTrue(got["truncated"])
        self.assertLessEqual(len(got["content"]), result_files.MAX_FILE_BYTES)
        self.assertLessEqual(got["bytes"], result_files.MAX_FILE_BYTES)

    def test_file_count_is_capped(self):
        task, leased = self._create_and_lease()
        files = [{"path": f"f{i:02d}.py", "content": "x\n"} for i in range(40)]
        self._complete_with_files(leased, files)
        listed = self.client.get(
            f"/api/tasks/{task['task_id']}/files").get_json()["files"]
        self.assertLessEqual(len(listed), result_files.MAX_FILES)

    def test_queued_task_has_no_files(self):
        task = self.client.post("/api/tasks", json={
            "machine": "mac-local", "agent_type": "codex",
            "project": "agent-fleet", "instruction": "x",
        }).get_json()["task"]
        listed = self.client.get(f"/api/tasks/{task['task_id']}/files")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()["files"], [])
        missing = self.client.get(f"/api/tasks/{task['task_id']}/files/a.py")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.get_json()["error"], "not_found")

    def test_missing_task_is_404(self):
        resp = self.client.get("/api/tasks/t-missing/files/a.py")
        self.assertEqual(resp.status_code, 404)

    def test_read_is_rate_limited_and_audited(self):
        task, leased = self._create_and_lease()
        # MAX_FILES = 16, so we can only attach 16 files max
        # Create files that will exceed 1MB rate limit when read sequentially
        # Each file is MAX_FILE_BYTES - 100 = ~16KB
        # Need to read enough to exceed 1MB: 1MB / 16KB = ~64 reads
        # But we can only create 16 files, so we read each file multiple times
        small_content = "x" * (result_files.MAX_FILE_BYTES - 100)
        files = [{"path": f"file{i}.txt", "content": small_content}
                 for i in range(16)]
        self._complete_with_files(leased, files)

        # Read files repeatedly until rate limit is hit
        # Need 64+ reads to exceed 1MB (16 files * 4 times = 64 reads)
        last_status = None
        for round in range(5):
            for i in range(16):
                path = f"/api/tasks/{task['task_id']}/files/file{i}.txt"
                resp = self.client.get(path)
                last_status = resp.status_code
                if resp.status_code == 429:
                    self.assertEqual(resp.get_json()["error"], "rate_limited")
                    # Verify audit trail
                    conn = task_store._connect()
                    try:
                        rows = conn.execute(
                            "SELECT action FROM audit WHERE task_id=? AND action=?",
                            (task["task_id"], "read_task_file")).fetchall()
                    finally:
                        conn.close()
                    self.assertGreaterEqual(len(rows), 1)
                    return

        self.fail(f"Should hit rate limit, last status: {last_status}")

    def test_idempotent_result_does_not_replace_files(self):
        task, leased = self._create_and_lease()
        self._complete_with_files(leased, [
            {"path": "a.py", "content": "first\n"},
        ])
        self._complete_with_files(leased, [
            {"path": "a.py", "content": "second\n"},
        ])
        got = self.client.get(
            f"/api/tasks/{task['task_id']}/files/a.py").get_json()
        self.assertEqual(got["content"], "first\n")

    def test_retry_hides_previous_attempt_files(self):
        task, leased = self._create_and_lease()
        self._complete_with_files(leased, [
            {"path": "old.py", "content": "first\n"},
        ])
        tid = task["task_id"]
        retry = self.client.post(f"/api/tasks/{tid}/retry")
        self.assertEqual(retry.status_code, 200)
        listed = self.client.get(f"/api/tasks/{tid}/files").get_json()["files"]
        self.assertEqual(listed, [])
        missing = self.client.get(f"/api/tasks/{tid}/files/old.py")
        self.assertEqual(missing.status_code, 404)


class RunnerSubmitFilesTests(unittest.TestCase):
    def test_submit_result_includes_bounded_files(self):
        import importlib.util
        path = Path(__file__).resolve().parents[1] / "tools" / "agent-runner.py"
        spec = importlib.util.spec_from_file_location("agent_runner_files", path)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)

        class Cfg:
            hub = "http://hub"
            machine = "mac-local"
            credential = "rs"
            cache_dir = Path(tempfile.mkdtemp())

        posts = []

        def fake_post(cfg, url, body, timeout=15):
            posts.append(body)
            return 200, {"ok": True}

        class Result:
            exit_code = 0
            log_tail = "done"

        runner.post_json = fake_post
        runner._submit_result(
            Cfg(), "a1", "n1", Result(), "1 file", 1.0,
            files=[{"path": "a.py", "content": "x\n", "truncated": False,
                    "redacted": False}])
        self.assertEqual(posts[0]["files"][0]["path"], "a.py")
        self.assertNotIn("attempt_id", posts[0]["files"][0])

    def test_submit_result_includes_patch_and_test_summary(self):
        import importlib.util
        path = Path(__file__).resolve().parents[1] / "tools" / "agent-runner.py"
        spec = importlib.util.spec_from_file_location("agent_runner_patch", path)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)

        class Cfg:
            hub = "http://hub"
            machine = "mac-local"
            credential = "rs"
            cache_dir = Path(tempfile.mkdtemp())

        posts = []

        def fake_post(cfg, url, body, timeout=15):
            posts.append(body)
            return 200, {"ok": True}

        class Result:
            exit_code = 1
            log_tail = "fail"

        runner.post_json = fake_post
        runner._submit_result(
            Cfg(), "a2", "n2", Result(), "1 file", 2.0,
            diff_patch="diff --git a/x b/x\n+hello",
            test_summary={"framework": "pytest", "passed": 0, "failed": 1})
        self.assertIn("+hello", posts[0]["diff_patch"])
        self.assertEqual(posts[0]["test_summary"]["failed"], 1)


class PatchAndTestSummaryTests(unittest.TestCase):
    def test_redact_patch_strips_token(self):
        patch = f"diff --git a/x b/x\n+token={GITHUB_PAT}\n"
        out, flagged = result_files.redact_patch(patch)
        self.assertNotIn(GITHUB_PAT, out)
        self.assertTrue(flagged)

    def test_collect_test_summary_allowlist(self):
        root = Path(tempfile.mkdtemp())
        (root / "test-results.json").write_text(json.dumps({
            "framework": "pytest",
            "passed": 2,
            "failed": 1,
            "secret_key": "drop",
            "failed_names": ["test_foo"],
        }))
        summary = result_files.collect_test_summary(root)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["failed_names"], ["test_foo"])
        self.assertNotIn("secret_key", summary)

    def test_collect_test_summary_missing_is_none(self):
        root = Path(tempfile.mkdtemp())
        self.assertIsNone(result_files.collect_test_summary(root))


if __name__ == "__main__":
    unittest.main()
