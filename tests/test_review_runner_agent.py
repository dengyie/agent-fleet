"""Review regressions: agent-runner reliability (finding 8 / attempt_id validation)."""
import json
import logging
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import yaml

from tools import adapters, result_files, runner_config
from tests.test_runner import load_agent_runner


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", "init"], cwd=path, check=True)
    return path


class _Base(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.project = _git_repo(self.temp / "proj")
        self.runner = load_agent_runner()
        self.posts = []

    def _cfg(self, managed=False, command=None):
        return runner_config.RunnerConfig(
            hub="https://hub.test", machine="mac-local", credential="s",
            runner_id="r1", projects={"proj": self.project},
            agents={"codex": {"command": command, "timeout_s": 30}},
            poll_interval_s=1, heartbeat_interval_s=1,
            cache_dir=self.temp / "cache",
            supervisor_enabled=managed,
            supervisor_manifest_dir=(self.temp / "sup") if managed else None,
        )

    def _task(self, **over):
        task = {"task_id": "t-1", "machine": "mac-local", "agent_type": "codex",
                "project": "proj", "instruction": "do it",
                "attempt_id": "a1", "nonce": "n1"}
        task.update(over)
        return task

    def _fake_post(self, responder=None):
        def post(cfg, path, body, timeout=15):
            self.posts.append((path, body))
            if responder is not None:
                return responder(path, body)
            return 200, {"ok": True}
        return mock.patch.object(self.runner, "post_json", post)

    def _results(self):
        return [b for p, b in self.posts if p.endswith("/result")]


class _FakeAdapter:
    command = ["fake"]

    def __init__(self, exit_code=0, run=None):
        self._exit = exit_code
        self._run = run

    def run(self, instruction, workdir, on_line=None, should_abort=None):
        if self._run is not None:
            return self._run(should_abort)
        return adapters.AdapterResult(exit_code=self._exit, log_tail="ok")


class PostStepTests(_Base):
    def test_post_step_failure_keeps_real_exit_code(self):
        with self._fake_post(), \
                mock.patch.object(self.runner.adapters, "create",
                                  return_value=_FakeAdapter(exit_code=3)), \
                mock.patch.object(self.runner.worktree, "diff_stat",
                                  side_effect=RuntimeError("diff boom")):
            out = self.runner.run_task(self._cfg(), self._task())
        self.assertEqual(out["exit_code"], 3)
        self.assertEqual([b["exit_code"] for b in self._results()], [3])


class PendingTests(_Base):
    def _pending(self, cfg, attempt="a1"):
        d = cfg.cache_dir / "pending"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{attempt}.json"
        f.write_text(json.dumps({"attempt_id": attempt, "nonce": "n", "exit_code": 0}),
                     encoding="utf-8")
        return f

    def test_terminal_4xx_is_quarantined(self):
        cfg = self._cfg()
        f = self._pending(cfg)
        with self._fake_post(lambda p, b: (422, {"ok": False})):
            self.runner.flush_pending(cfg)
        self.assertFalse(f.exists())
        self.assertTrue((cfg.cache_dir / "pending" / "rejected" / "a1.json").exists())

    def test_retryable_4xx_is_kept(self):
        cfg = self._cfg()
        f = self._pending(cfg)
        with self._fake_post(lambda p, b: (429, {"ok": False})):
            self.runner.flush_pending(cfg)
        self.assertTrue(f.exists())

    def test_409_is_logged_before_delete(self):
        cfg = self._cfg()
        f = self._pending(cfg)
        with self._fake_post(lambda p, b: (409, {"ok": False})), \
                self.assertLogs("agent-fleet.runner", level=logging.WARNING) as logs:
            self.runner.flush_pending(cfg)
        self.assertFalse(f.exists())
        self.assertTrue(any("409" in line and "a1.json" in line for line in logs.output))

    def test_pending_write_is_atomic(self):
        cfg = self._cfg()
        real_write = pathlib.Path.write_text

        def torn_write(self_path, data, *a, **kw):
            real_write(self_path, data[: len(data) // 2], *a, **kw)
            raise OSError("disk full")

        result = adapters.AdapterResult(exit_code=0, log_tail="x" * 200)
        with self._fake_post(lambda p, b: (0, {"error": "down"})), \
                mock.patch.object(pathlib.Path, "write_text", torn_write):
            try:
                self.runner._submit_result(cfg, "a1", "n1", result, "", 1.0)
            except OSError:
                pass
        target = cfg.cache_dir / "pending" / "a1.json"
        self.assertFalse(target.exists(), "half-written pending file must never appear")

    def test_invalid_attempt_id_never_reaches_url_or_disk(self):
        cfg = self._cfg()
        with self._fake_post(lambda p, b: (200, {"ok": True, "task": self._task(
                attempt_id="../../evil")})), \
                mock.patch.object(self.runner, "run_task") as run_task:
            got = self.runner.poll_once(cfg)
        self.assertFalse(got)
        run_task.assert_not_called()
        status = self.runner._submit_result(
            cfg, "../x", "n", adapters.AdapterResult(exit_code=0, log_tail=""), "", 0.0)
        self.assertEqual(status, 0)
        self.assertFalse(any("../" in p for p, _ in self.posts))


class LeaseDeadlineTests(_Base):
    def test_lease_clock(self):
        now = [0.0]
        clock = self.runner._LeaseClock(deadline_s=10, clock=lambda: now[0])
        now[0] = 9
        self.assertFalse(clock.expired())
        now[0] = 11
        self.assertTrue(clock.expired())
        clock.renewed()
        self.assertFalse(clock.expired())

    def test_heartbeat_outage_aborts_before_hub_lease_expiry(self):
        def long_run(should_abort):
            end = time.monotonic() + 15
            while time.monotonic() < end:
                if should_abort():
                    return adapters.AdapterResult(exit_code=130, log_tail="aborted",
                                                  aborted=True)
                time.sleep(0.05)
            return adapters.AdapterResult(exit_code=0, log_tail="finished")

        def responder(path, body):
            if path.endswith("/heartbeat"):
                return 0, {"error": "network"}
            return 200, {"ok": True}

        started = time.monotonic()
        with self._fake_post(responder), \
                mock.patch.object(self.runner, "LOCAL_LEASE_DEADLINE_S", 1.5), \
                mock.patch.object(self.runner.adapters, "create",
                                  return_value=_FakeAdapter(run=long_run)):
            out = self.runner.run_task(self._cfg(), self._task())
        self.assertEqual(out["exit_code"], 130)
        self.assertLess(time.monotonic() - started, 10)


class IntervalTests(unittest.TestCase):
    def test_zero_intervals_clamped_to_one_second(self):
        temp = Path(tempfile.mkdtemp())
        cred = temp / "cred"
        cred.write_text("s\n")
        (temp / "proj").mkdir()
        path = temp / "runner.yaml"
        path.write_text(yaml.safe_dump({
            "hub": "https://hub.example.com", "machine": "m1",
            "credential_file": str(cred),
            "projects": {"p": {"path": str(temp / "proj")}},
            "poll_interval_s": 0, "heartbeat_interval_s": 0,
        }))
        cfg = runner_config.load_config(path)
        self.assertEqual(cfg.poll_interval_s, 1)
        self.assertEqual(cfg.heartbeat_interval_s, 1)


class ResultFilesTests(unittest.TestCase):
    def test_large_file_is_stat_checked_and_read_bounded(self):
        root = Path(tempfile.mkdtemp())
        big = root / "big.txt"
        with big.open("wb") as fh:
            fh.write(b"a" * (result_files.MAX_FILE_BYTES * 8))
        with mock.patch.object(pathlib.Path, "read_bytes",
                               side_effect=AssertionError("unbounded read_bytes")):
            files = result_files.collect_result_files(root)
        self.assertEqual(len(files), 1)
        self.assertLessEqual(len(files[0]["content"].encode("utf-8")),
                             result_files.MAX_FILE_BYTES)

    @unittest.skipIf(os.name == "nt", "symlink semantics")
    def test_symlink_is_not_followed(self):
        root = Path(tempfile.mkdtemp())
        outside = Path(tempfile.mkdtemp()) / "secret.txt"
        outside.write_text("secret")
        (root / "link.txt").symlink_to(outside)
        self.assertEqual(result_files.collect_result_files(root), [])


class ManagedPathTests(_Base):
    def _supervisor(self, cfg):
        return self.runner._bf_supervisor().Supervisor(
            manifest_dir=cfg.supervisor_manifest_dir, machine_id=cfg.machine)

    def test_leading_dash_instruction_rejected_before_launch(self):
        cfg = self._cfg(managed=True, command=[sys.executable, "-c", "print(1)"])
        sup = self._supervisor(cfg)
        with self._fake_post(), mock.patch.object(sup, "launch") as launch:
            out = self.runner.run_task_managed(cfg, self._task(instruction="--help"), sup)
        launch.assert_not_called()
        self.assertEqual(out["exit_code"], 64)

    def test_child_env_is_allow_listed(self):
        out_file = self.temp / "env.json"
        cmd = [sys.executable, "-c",
               "import json,os,sys; json.dump(dict(os.environ), open(sys.argv[1],'w'))",
               str(out_file)]
        cfg = self._cfg(managed=True, command=cmd + ["{instruction}"])
        sup = self._supervisor(cfg)
        with self._fake_post(), mock.patch.dict(os.environ, {
                "AGENT_FLEET_RUNNER_CREDENTIAL": "leak", "GITHUB_TOKEN": "leak"}):
            self.runner.run_task_managed(cfg, self._task(), sup)
        env = json.loads(out_file.read_text())
        self.assertFalse([k for k in env if k.startswith("AGENT_FLEET_") or k.endswith("_TOKEN")])

    @unittest.skipIf(os.name == "nt", "POSIX process groups")
    def test_group_is_dead_before_worktree_cleanup(self):
        cfg = self._cfg(managed=True,
                        command=[sys.executable, "-c", "import time; time.sleep(60)"])
        sup = self._supervisor(cfg)
        seen = {}
        real_launch = sup.launch

        def launch(**kw):
            manifest = real_launch(**kw)
            seen["pgid"] = sup._entries[manifest.session_id].handle.proc.pid
            return manifest

        def cleanup(project, wt, task_id):
            try:
                os.killpg(int(seen["pgid"]), 0)
                seen["alive_at_cleanup"] = True
            except ProcessLookupError:
                seen["alive_at_cleanup"] = False

        try:
            with self._fake_post(), \
                    mock.patch.object(sup, "launch", side_effect=launch), \
                    mock.patch.object(sup, "wait_session",
                                      side_effect=RuntimeError("wait boom")), \
                    mock.patch.object(self.runner.worktree, "cleanup_worktree",
                                      side_effect=cleanup):
                out = self.runner.run_task_managed(cfg, self._task(), sup)
        finally:
            if "pgid" in seen:
                try:
                    os.killpg(int(seen["pgid"]), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.assertEqual(out["exit_code"], 125)
        self.assertIs(seen.get("alive_at_cleanup"), False)


if __name__ == "__main__":
    unittest.main()
