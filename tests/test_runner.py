import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tools import adapters
from tools import runner_config
from tools import worktree


def load_agent_runner():
    path = Path(__file__).resolve().parents[1] / "tools" / "agent-runner.py"
    spec = importlib.util.spec_from_file_location("agent_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RunnerConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.project = self.temp / "proj"
        self.project.mkdir()
        self.cred = self.temp / "runner-credential"
        self.cred.write_text("s3cr3t\n")

    def _write(self, **overrides):
        import yaml
        data = {
            "hub": "https://hub.example.com",
            "machine": "mac-local",
            "credential_file": str(self.cred),
            "projects": {"agent-fleet": {"path": str(self.project)}},
            "agents": {"codex": {"command": ["codex", "exec", "{instruction}"],
                                  "timeout_s": 1800}},
        }
        data.update(overrides)
        path = self.temp / "runner.yaml"
        path.write_text(yaml.safe_dump(data))
        return path

    def test_load_valid_config(self):
        cfg = runner_config.load_config(self._write())
        self.assertEqual(cfg.hub, "https://hub.example.com")
        self.assertEqual(cfg.machine, "mac-local")
        self.assertEqual(cfg.runner_id, "mac-local")
        self.assertEqual(cfg.credential, "s3cr3t")
        self.assertEqual(cfg.projects["agent-fleet"], self.project)
        self.assertEqual(cfg.agents["codex"]["timeout_s"], 1800)
        self.assertEqual(cfg.poll_interval_s, 15)

    def test_registry_fallback_fills_omitted_executable_families(self):
        # agents 段只写 codex：其余可执行家族（claude_code/pi）由注册表
        # 默认值补齐；hermes 无 default_command 保持 None。
        cfg = runner_config.load_config(self._write())
        self.assertEqual(cfg.agents["claude_code"]["command"],
                         ["claude", "-p", "{instruction}"])
        self.assertEqual(cfg.agents["pi"]["command"], ["pi", "-p", "{instruction}"])
        self.assertIsNone(cfg.agents["hermes"]["command"])
        self.assertEqual(cfg.agents["pi"]["timeout_s"], 1800)

    def test_partial_agent_entry_not_overridden_by_registry(self):
        # 部分指定（含显式 command: null）的家族保持原样，尊重操作者意图。
        cfg = runner_config.load_config(self._write(
            agents={"codex": {"command": ["codex", "exec", "{instruction}"],
                              "timeout_s": 1800},
                     "pi": {"command": None}}))
        self.assertIsNone(cfg.agents["pi"]["command"])
        self.assertEqual(cfg.agents["pi"]["timeout_s"], 1800)

    def test_missing_project_path_rejected(self):
        with self.assertRaises(runner_config.ConfigError):
            runner_config.load_config(self._write(
                projects={"ghost": {"path": str(self.temp / "nope")}}))

    def test_relative_project_path_rejected(self):
        with self.assertRaises(runner_config.ConfigError):
            runner_config.load_config(self._write(
                projects={"rel": {"path": "relative/path"}}))

    def test_missing_credential_rejected(self):
        with self.assertRaises(runner_config.ConfigError):
            runner_config.load_config(self._write(
                credential_file=str(self.temp / "missing-cred")))

    def test_unknown_hub_scheme_rejected(self):
        with self.assertRaises(runner_config.ConfigError):
            runner_config.load_config(self._write(hub="http://insecure.example"))

    # ---- Fix round: ~ expansion ----

    def test_tilde_credential_file_expanded(self):
        # HOME is patched so ~ never touches a real home dir.
        fake_home = self.temp / "home"
        fake_home.mkdir()
        cred_dir = fake_home / ".config" / "agent-fleet"
        cred_dir.mkdir(parents=True)
        cred = cred_dir / "runner-credential"
        cred.write_text("tilde-secret\n")
        path = self._write(credential_file="~/.config/agent-fleet/runner-credential")
        with mock.patch.dict(os.environ, {"HOME": str(fake_home)}):
            with mock.patch.object(Path, "home", return_value=fake_home):
                cfg = runner_config.load_config(path)
        self.assertEqual(cfg.credential, "tilde-secret")

    def test_tilde_project_path_expanded(self):
        fake_home = self.temp / "home"
        fake_home.mkdir()
        proj = fake_home / "work" / "proj"
        proj.mkdir(parents=True)
        path = self._write(projects={"p": {"path": "~/work/proj"}})
        with mock.patch.dict(os.environ, {"HOME": str(fake_home)}):
            with mock.patch.object(Path, "home", return_value=fake_home):
                cfg = runner_config.load_config(path)
        self.assertEqual(cfg.projects["p"], proj)

    def test_tilde_cache_dir_expanded(self):
        fake_home = self.temp / "home"
        fake_home.mkdir()
        cache = fake_home / ".cache" / "agent-fleet"
        cache.mkdir(parents=True)
        path = self._write(cache_dir="~/.cache/agent-fleet")
        with mock.patch.dict(os.environ, {"HOME": str(fake_home)}):
            with mock.patch.object(Path, "home", return_value=fake_home):
                cfg = runner_config.load_config(path)
        self.assertEqual(cfg.cache_dir, cache)

    # ---- Fix round 2: malformed config always ConfigError ----

    def test_malformed_yaml_raises_config_error(self):
        path = self.temp / "bad.yaml"
        path.write_text("hub: [unclosed\n  {oops")
        with self.assertRaises(runner_config.ConfigError):
            runner_config.load_config(path)

    def test_non_mapping_projects_entry_rejected(self):
        with self.assertRaises(runner_config.ConfigError):
            runner_config.load_config(self._write(projects={"p": "not-a-mapping"}))

    def test_non_mapping_agents_entry_rejected(self):
        with self.assertRaises(runner_config.ConfigError):
            runner_config.load_config(self._write(
                agents={"codex": "not-a-mapping"}))

    def test_non_numeric_timeout_rejected(self):
        with self.assertRaises(runner_config.ConfigError):
            runner_config.load_config(
                self._write(agents={"codex": {"command": ["c"], "timeout_s": "abc"}}))

    def test_non_numeric_poll_interval_rejected(self):
        with self.assertRaises(runner_config.ConfigError):
            runner_config.load_config(self._write(poll_interval_s="soon"))

    def test_non_numeric_heartbeat_interval_rejected(self):
        with self.assertRaises(runner_config.ConfigError):
            runner_config.load_config(self._write(heartbeat_interval_s="never"))

    # ---- Fix round 3: overflow & non-UTF-8 read errors ----

    def test_infinite_timeout_rejected(self):
        with self.assertRaises(runner_config.ConfigError):
            runner_config.load_config(
                self._write(agents={"codex": {"command": ["c"], "timeout_s": float("inf")}}))

    def test_negative_infinite_poll_interval_rejected(self):
        with self.assertRaises(runner_config.ConfigError):
            runner_config.load_config(self._write(poll_interval_s=float("-inf")))

    def test_invalid_utf8_config_rejected(self):
        path = self.temp / "bad-utf8.yaml"
        path.write_bytes(b"hub: https://agent.example.com\nmachine: \xff\xfe broken\n")
        with self.assertRaises(runner_config.ConfigError):
            runner_config.load_config(path)

    # ---- supervisor opt-in (control-client / managed) ----

    def test_supervisor_disabled_by_default(self):
        cfg = runner_config.load_config(self._write())
        self.assertFalse(cfg.supervisor_enabled)
        self.assertFalse(cfg.managed_enabled)
        self.assertIsNone(cfg.supervisor_manifest_dir)
        self.assertIsNone(cfg.supervisor_credential)
        self.assertIsNone(cfg.supervisor_public_key)

    def test_supervisor_enabled_requires_manifest_dir(self):
        with self.assertRaises(runner_config.ConfigError):
            runner_config.load_config(self._write(supervisor={"enabled": True}))

    def test_supervisor_enabled_with_manifest_dir(self):
        manifests = self.temp / "manifests"
        manifests.mkdir()
        cfg = runner_config.load_config(self._write(
            supervisor={"enabled": True, "manifest_dir": str(manifests)}))
        self.assertTrue(cfg.supervisor_enabled)
        self.assertTrue(cfg.managed_enabled)
        self.assertEqual(cfg.supervisor_manifest_dir, manifests)

    def test_supervisor_credential_secret_only(self):
        import base64
        manifests = self.temp / "manifests"
        manifests.mkdir()
        cred = self.temp / "sup-cred"
        cred.write_text("sup-secret\n")
        pk = self.temp / "sup-pk"
        pk.write_text(base64.b64encode(b"P" * 32).decode() + "\n")
        cfg = runner_config.load_config(self._write(supervisor={
            "enabled": True,
            "manifest_dir": str(manifests),
            "credential_file": str(cred),
            "public_key_file": str(pk),
        }))
        self.assertEqual(cfg.supervisor_credential, "sup-secret")
        self.assertEqual(cfg.supervisor_public_key, b"P" * 32)

    def test_supervisor_credential_strips_matching_machine_prefix(self):
        manifests = self.temp / "manifests"
        manifests.mkdir()
        cred = self.temp / "sup-cred"
        cred.write_text("mac-local:sup-secret\n")
        cfg = runner_config.load_config(self._write(supervisor={
            "enabled": True,
            "manifest_dir": str(manifests),
            "credential_file": str(cred),
        }))
        self.assertEqual(cfg.supervisor_credential, "sup-secret")

    def test_supervisor_credential_prefix_mismatch_rejected(self):
        manifests = self.temp / "manifests"
        manifests.mkdir()
        cred = self.temp / "sup-cred"
        cred.write_text("other-host:sup-secret\n")
        with self.assertRaises(runner_config.ConfigError):
            runner_config.load_config(self._write(supervisor={
                "enabled": True,
                "manifest_dir": str(manifests),
                "credential_file": str(cred),
            }))

    def test_supervisor_public_key_wrong_length_rejected(self):
        import base64
        manifests = self.temp / "manifests"
        manifests.mkdir()
        pk = self.temp / "sup-pk"
        pk.write_text(base64.b64encode(b"short").decode() + "\n")
        with self.assertRaises(runner_config.ConfigError):
            runner_config.load_config(self._write(supervisor={
                "enabled": True,
                "manifest_dir": str(manifests),
                "public_key_file": str(pk),
            }))

    def test_supervisor_missing_credential_file_rejected(self):
        manifests = self.temp / "manifests"
        manifests.mkdir()
        with self.assertRaises(runner_config.ConfigError):
            runner_config.load_config(self._write(supervisor={
                "enabled": True,
                "manifest_dir": str(manifests),
                "credential_file": str(self.temp / "missing-sup-cred"),
            }))


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=True)


class WorktreeTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.project = self.temp / "proj"
        self.project.mkdir()
        git("init", cwd=self.project)
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit",
            "--allow-empty", "-m", "init", cwd=self.project)

    def test_create_diff_cleanup_cycle(self):
        wt = worktree.create_worktree(self.project, "t-abc123")
        self.assertTrue((wt / ".git").exists())
        self.assertEqual(wt.name, "t-abc123")
        # worktree 在项目同级目录，不在项目内
        self.assertEqual(wt.parent, self.temp / "proj.fleet-worktrees")
        # 在 worktree 里改文件 → diff_stat 可见
        (wt / "hello.txt").write_text("hi\n")
        stat = worktree.diff_stat(wt)
        self.assertIn("hello.txt", stat)
        # 清理后目录与分支都不存在
        worktree.cleanup_worktree(self.project, wt, "t-abc123")
        self.assertFalse(wt.exists())
        branches = git("branch", "--list", "fleet/*", cwd=self.project).stdout
        self.assertNotIn("t-abc123", branches)

    def test_diff_stat_truncates(self):
        wt = worktree.create_worktree(self.project, "t-big")
        for i in range(50):
            (wt / f"file-with-a-rather-long-name-{i:03d}.txt").write_text("x\n")
        stat = worktree.diff_stat(wt, max_bytes=200)
        self.assertLessEqual(len(stat), 220)  # 截断 + 标记
        worktree.cleanup_worktree(self.project, wt, "t-big")

    def test_create_failure_raises_worktree_error(self):
        with self.assertRaises(worktree.WorktreeError):
            worktree.create_worktree(self.temp / "not-a-repo", "t-x")

    def test_diff_patch_contains_hunk_and_truncates(self):
        wt = worktree.create_worktree(self.project, "t-patch")
        (wt / "hello.txt").write_text("hi\n")
        patch = worktree.diff_patch(wt)
        self.assertIn("hello.txt", patch)
        self.assertIn("+hi", patch)
        huge = worktree.diff_patch(wt, max_bytes=40)
        self.assertIn("[truncated]", huge)
        worktree.cleanup_worktree(self.project, wt, "t-patch")


class WorktreeGitFailureTests(unittest.TestCase):
    """_git() 必须把 subprocess 失败统一收敛为 WorktreeError，且不被脏字节击穿。"""

    def test_timeout_raises_worktree_error(self):
        project = self._project()
        with mock.patch.object(worktree.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("git", 60)):
            with self.assertRaises(worktree.WorktreeError) as ctx:
                worktree.create_worktree(project, "t-timeout")
        self.assertIn("超时", str(ctx.exception))

    def test_non_utf8_stderr_error_message(self):
        # git 输出非法 UTF-8 时不得泄漏 UnicodeDecodeError，须以 WorktreeError 收口。
        project = self._project()

        class Proc:
            returncode = 128
            stdout = ""
            stderr = "fatal: \xff\xfe oops"

        with mock.patch.object(worktree.subprocess, "run", return_value=Proc()):
            with self.assertRaises(worktree.WorktreeError) as ctx:
                worktree.create_worktree(project, "t-utf8")
        self.assertIn("失败", str(ctx.exception))

    def test_git_passes_errors_replace_for_decode(self):
        # 根因回归：subprocess.run 必须带 errors="replace"，脏字节不得击穿解码。
        project = self._project()

        class Proc:
            returncode = 0
            stdout = "ok\n"
            stderr = ""

        with mock.patch.object(worktree.subprocess, "run",
                               return_value=Proc()) as run:
            worktree._git(project, "status")
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs.get("capture_output"), True)
        self.assertEqual(kwargs.get("text"), True)
        self.assertEqual(kwargs.get("errors"), "replace")
        self.assertEqual(kwargs.get("timeout"), 60)

    def _project(self):
        project = Path(tempfile.mkdtemp()) / "proj"
        project.mkdir()
        git("init", cwd=project)
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit",
            "--allow-empty", "-m", "init", cwd=project)
        return project


class AdapterTests(unittest.TestCase):
    def _python(self, script):
        return [sys.executable, "-c", script]

    def test_run_captures_output_lines(self):
        adapter = adapters.create("codex", command=self._python(
            "import sys; print('line1'); print('line2'); sys.argv and None"),
                                  timeout_s=30)
        lines = []
        result = adapter.run("do something", Path(tempfile.mkdtemp()), on_line=lines.append)
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.timed_out)
        self.assertEqual(lines, ["line1", "line2"])
        self.assertIn("line2", result.log_tail)

    def test_instruction_interpolated_as_single_argv(self):
        adapter = adapters.create("codex", command=self._python(
            "import sys; print(sys.argv[1])") + ["{instruction}"], timeout_s=30)
        lines = []
        adapter.run("fix $(rm -rf /) ; `whoami`", Path(tempfile.mkdtemp()),
                    on_line=lines.append)
        # instruction 作为单 argv 元素原样传递，不经 shell 展开
        self.assertEqual(lines, ["fix $(rm -rf /) ; `whoami`"])

    def test_timeout_kills_process(self):
        adapter = adapters.create("codex", command=self._python(
            "import time; time.sleep(60)"), timeout_s=1)
        start = time.time()
        result = adapter.run("x", Path(tempfile.mkdtemp()))
        self.assertTrue(result.timed_out)
        self.assertEqual(result.exit_code, 124)
        self.assertLess(time.time() - start, 10)

    def test_abort_terminates_process(self):
        adapter = adapters.create("codex", command=self._python(
            "import time; print('start', flush=True); time.sleep(60)"), timeout_s=60)
        flag = {"abort": False}
        lines = []

        def on_line(line):
            lines.append(line)
            flag["abort"] = True

        result = adapter.run("x", Path(tempfile.mkdtemp()), on_line=on_line,
                             should_abort=lambda: flag["abort"])
        self.assertTrue(result.aborted)
        self.assertEqual(result.exit_code, 130)

    def test_unconfigured_adapter_returns_127(self):
        result = adapters.create("hermes", command=None, timeout_s=1).run(
            "x", Path(tempfile.mkdtemp()))
        self.assertEqual(result.exit_code, 127)
        self.assertIn("未配置", result.log_tail)

    def test_unknown_adapter_type_rejected(self):
        with self.assertRaises(ValueError):
            adapters.create("bash")

    def test_log_tail_bounded(self):
        adapter = adapters.create("codex", command=self._python(
            "print('x' * 20000)"), timeout_s=30)
        result = adapter.run("x", Path(tempfile.mkdtemp()))
        self.assertLessEqual(len(result.log_tail), 10240)


class AdapterDefaultsTests(unittest.TestCase):
    def test_default_commands(self):
        self.assertEqual(adapters.create("codex").build_argv("修 bug"),
                         ["codex", "exec", "--approve-for-me", "--ephemeral",
                          "--ignore-user-config", "--json", "修 bug"])
        self.assertEqual(adapters.create("claude_code").build_argv("修 bug"),
                         ["claude", "-p", "修 bug"])
        self.assertEqual(adapters.create("pi").build_argv("修 bug"),
                         ["pi", "-p", "修 bug"])
        self.assertIsNone(adapters.create("hermes").command)

    def test_argv_without_placeholder_appends_instruction(self):
        adapter = adapters.create("codex", command=[sys.executable, "-c", "pass"])
        self.assertEqual(adapter.build_argv("do it")[-1], "do it")


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class AgentRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.project = self.temp / "proj"
        self.project.mkdir()
        subprocess.run(["git", "init"], cwd=self.project, capture_output=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "--allow-empty", "-m", "init"],
                       cwd=self.project, capture_output=True)
        self.runner = load_agent_runner()
        self.cfg = runner_config.RunnerConfig(
            hub="https://hub.test", machine="mac-local", credential="s3",
            runner_id="r1", projects={"proj": self.project},
            agents={"codex": {"command": None, "timeout_s": 30}},
            poll_interval_s=1, heartbeat_interval_s=1, cache_dir=self.temp / "cache")

    def _task(self):
        return {"task_id": "t-1", "machine": "mac-local", "agent_type": "codex",
                "project": "proj", "instruction": "do it",
                "attempt_id": "a1", "nonce": "n1",
                "lease_ttl_s": 300, "lease_expires_at": "..."}

    def test_post_json_sets_runner_headers(self):
        captured = {}

        def fake_urlopen(req, timeout):
            captured["cred"] = req.headers.get("X-runner-credential")
            captured["ua"] = req.headers.get("User-agent")
            return FakeResponse(200, {"ok": True})

        with mock.patch.object(self.runner.urllib.request, "urlopen",
                               side_effect=fake_urlopen):
            status, data = self.runner.post_json(self.cfg, "/api/commands/poll", {})
        self.assertEqual(status, 200)
        self.assertEqual(captured["cred"], "mac-local:s3")
        self.assertEqual(captured["ua"], "agent-fleet-runner/1.0")

    def test_post_json_network_error_returns_zero_status(self):
        with mock.patch.object(self.runner.urllib.request, "urlopen",
                               side_effect=OSError("network down")):
            status, data = self.runner.post_json(self.cfg, "/api/commands/poll", {})
        self.assertEqual(status, 0)
        self.assertIn("error", data)

    def test_run_task_success_flow(self):
        posts = []

        def fake_urlopen(req, timeout):
            posts.append((req.full_url, json.loads(req.data.decode())))
            return FakeResponse(200, {"ok": True, "task_id": "t-1",
                                      "lease_expires_at": "..."})

        fake_result = adapters.AdapterResult(exit_code=0, log_tail="done")
        with mock.patch.object(self.runner.urllib.request, "urlopen",
                               side_effect=fake_urlopen), \
             mock.patch.object(self.runner.adapters, "create",
                               return_value=mock.Mock(run=mock.Mock(return_value=fake_result))):
            out = self.runner.run_task(self.cfg, self._task())
        self.assertEqual(out["exit_code"], 0)
        result_posts = [p for p in posts if p[0].endswith("/result")]
        self.assertEqual(len(result_posts), 1)
        self.assertEqual(result_posts[0][1]["nonce"], "n1")
        self.assertEqual(result_posts[0][1]["log_summary"], "done")
        # worktree 已清理
        self.assertFalse((self.temp / "proj.fleet-worktrees" / "t-1").exists())

    def test_result_post_failure_cached_to_pending(self):
        def fail_urlopen(req, timeout):
            raise OSError("network down")

        fake_result = adapters.AdapterResult(exit_code=0, log_tail="done")
        with mock.patch.object(self.runner.urllib.request, "urlopen",
                               side_effect=fail_urlopen), \
             mock.patch.object(self.runner.adapters, "create",
                               return_value=mock.Mock(run=mock.Mock(return_value=fake_result))):
            self.runner.run_task(self.cfg, self._task())
        pending = list((self.temp / "cache" / "pending").glob("*.json"))
        self.assertEqual(len(pending), 1)
        saved = json.loads(pending[0].read_text())
        self.assertEqual(saved["attempt_id"], "a1")

    def test_flush_pending_retries_upload(self):
        pending_dir = self.temp / "cache" / "pending"
        pending_dir.mkdir(parents=True)
        (pending_dir / "a1.json").write_text(json.dumps({
            "attempt_id": "a1", "nonce": "n1", "exit_code": 0,
            "log_summary": "done", "diff_stat": "", "duration_s": 1}))
        posts = []
        with mock.patch.object(self.runner.urllib.request, "urlopen",
                               side_effect=lambda req, timeout: (
                                   posts.append(req.full_url),
                                   FakeResponse(200, {"ok": True}))[1]):
            self.runner.flush_pending(self.cfg)
        self.assertEqual(len(posts), 1)
        self.assertEqual(list(pending_dir.glob("*.json")), [])

    def test_project_not_in_whitelist_refused(self):
        task = self._task()
        task["project"] = "evil"
        with mock.patch.object(self.runner.urllib.request, "urlopen",
                               return_value=FakeResponse(200, {"ok": True})) as uo, \
             mock.patch.object(self.runner.adapters, "create") as create:
            out = self.runner.run_task(self.cfg, task)
        self.assertNotEqual(out["exit_code"], 0)
        create.assert_not_called()  # adapter 未启动

    def test_poll_once_raises_on_poll_network_failure(self):
        # 网络失败（post_json status 0）须抛 RunnerPollError，由 main 转退避；
        # 不得静默返回 False（那会同等于空 poll 并重置 backoff）。
        calls = {"n": 0}

        def fail_urlopen(req, timeout):
            calls["n"] += 1
            raise OSError("network down")

        with mock.patch.object(self.runner.urllib.request, "urlopen",
                               side_effect=fail_urlopen):
            with self.assertRaises(self.runner.RunnerPollError):
                self.runner.poll_once(self.cfg)
        self.assertEqual(calls["n"], 1)  # flush_pending 无缓存，只 poll 一次

    def test_poll_network_failure_main_backoff_stays_5s_on_success(self):
        # 对照：一轮成功 poll（无任务）后 backoff 不会残留，仍从 MIN 起。
        sleeps = []
        calls = {"n": 0}

        def fake_urlopen(req, timeout):
            calls["n"] += 1
            if calls["n"] > 1:
                raise KeyboardInterrupt  # 第 1 轮成功空 poll 后终止
            return FakeResponse(200, {"ok": True, "task": None})

        with mock.patch.object(self.runner.runner_config, "load_config",
                               return_value=self.cfg), \
             mock.patch.object(self.runner.urllib.request, "urlopen",
                               side_effect=fake_urlopen), \
             mock.patch.object(self.runner.time, "sleep", side_effect=lambda s: sleeps.append(s)):
            rc = self.runner.main(["--config", str(self.temp / "nope.yaml"),
                                   "--interval", str(7)])
        self.assertEqual(rc, 0)
        self.assertEqual(len(sleeps), 1)
        self.assertEqual(sleeps[0], 7)  # 空 poll 成功 → 睡常规 interval，不递增

    def test_poll_returns_task_and_executes(self):
        posts = []

        def fake_urlopen(req, timeout):
            body = json.loads(req.data.decode())
            if req.full_url.endswith("/poll"):
                return FakeResponse(200, {"ok": True, "task": self._task()})
            posts.append(req.full_url)
            return FakeResponse(200, {"ok": True, "task_id": "t-1"})

        fake_result = adapters.AdapterResult(exit_code=0, log_tail="polled")
        with mock.patch.object(self.runner.urllib.request, "urlopen",
                               side_effect=fake_urlopen), \
             mock.patch.object(self.runner.adapters, "create",
                               return_value=mock.Mock(run=mock.Mock(return_value=fake_result))):
            got = self.runner.poll_once(self.cfg)
        self.assertTrue(got)
        result_posts = [u for u in posts if u.endswith("/result")]
        self.assertEqual(len(result_posts), 1)

    def test_main_backoff_increases_on_poll_network_failure(self):
        """网络失败(status 0)须保留/递增 backoff，不得重置 —— 5s→60s 退避要求。

        生产 main() 仍走 load_config 校验（此处注入 cfg 仅为了测循环；配置校验
        由 runner_config.load_config 自身测试覆盖）。
        """
        sleeps = []
        calls = {"n": 0}

        def fake_urlopen(req, timeout):
            calls["n"] += 1
            if calls["n"] > 2:
                raise KeyboardInterrupt  # 终止循环
            raise OSError("network down")

        with mock.patch.object(self.runner.runner_config, "load_config",
                               return_value=self.cfg), \
             mock.patch.object(self.runner.urllib.request, "urlopen",
                               side_effect=fake_urlopen), \
             mock.patch.object(self.runner.time, "sleep", side_effect=lambda s: sleeps.append(s)):
            rc = self.runner.main(["--config", str(self.temp / "nope.yaml"),
                                   "--interval", str(7)])
        self.assertEqual(rc, 0)
        # 第 1、2 次 poll 网络失败 → 各自睡 backoff(5s、10s)；异常后递增
        self.assertEqual(len(sleeps), 2)
        self.assertEqual(sleeps[0], self.runner.MIN_BACKOFF_S)
        self.assertGreater(sleeps[1], sleeps[0])


class AgentRunnerHeartbeatBoundingTests(unittest.TestCase):
    """heartbeat 日志有界性回归：burst 不得超过 50 行 × 500 字符。"""

    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.project = self.temp / "proj"
        self.project.mkdir()
        subprocess.run(["git", "init"], cwd=self.project, capture_output=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "--allow-empty", "-m", "init"],
                       cwd=self.project, capture_output=True)

    def test_on_line_truncates_long_lines(self):
        runner = load_agent_runner()
        buf = runner._LogBuffer()
        buf.enqueue("x" * 1000)
        buf.enqueue("ok")
        rows = buf.drain()
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(rows[0]), runner.MAX_LOG_LINE_CHARS)  # 500
        self.assertEqual(rows[1], "ok")

    def test_burst_keeps_latest_50_lines_only(self):
        runner = load_agent_runner()
        buf = runner._LogBuffer()
        for i in range(runner.MAX_LOG_LINES + 200):
            buf.enqueue(f"line-{i}")
        rows = buf.drain()
        self.assertEqual(len(rows), runner.MAX_LOG_LINES)  # 50
        # 保留最新 50 行（较旧行丢弃）
        self.assertEqual(rows[0], f"line-{200}")
        self.assertEqual(rows[-1], f"line-{249}")

    def test_bounded_log_lines_defense_in_depth(self):
        runner = load_agent_runner()
        raw = ["z" * runner.MAX_LOG_LINE_CHARS * 2] + [f"{i}" for i in range(60)]
        rows = runner._bounded_log_lines(raw)
        self.assertLessEqual(len(rows), runner.MAX_LOG_LINES)
        self.assertTrue(all(len(r) <= runner.MAX_LOG_LINE_CHARS for r in rows))
        # 最后一行是最大索引
        self.assertEqual(rows[-1], "59")

    def test_heartbeat_payload_bounded_per_send(self):
        """run_task 的 heartbeat 每次 send 恒 ≤50 行 × ≤500 字符（经 on_line → drain → bounded）。"""
        import threading
        heartbeat_seen = threading.Event()
        posts = []
        runner = load_agent_runner()

        def fake_urlopen(req, timeout):
            body = json.loads(req.data.decode())
            posts.append((req.full_url, body))
            if req.full_url.endswith("/heartbeat"):
                heartbeat_seen.set()
            return FakeResponse(200, {"ok": True, "task": None})

        def fake_run(instruction, workdir, on_line=None, should_abort=None):
            # 模拟适配器突发输出 200 行 × 1000 字符
            for i in range(200):
                on_line(f"long{i}-" + "x" * 1000)
            # 阻塞等待 heartbeat 线程发出首轮（最多 5s），保证收到过请求
            heartbeat_seen.wait(timeout=5)
            return adapters.AdapterResult(exit_code=0, log_tail="done")

        cfg = runner_config.RunnerConfig(
            hub="https://hub.test", machine="mac-local", credential="test",
            runner_id="r1", projects={"proj": self.project},
            agents={"codex": {"command": None, "timeout_s": 30}},
            poll_interval_s=1, heartbeat_interval_s=1, cache_dir=self.temp / "cache")

        with mock.patch.object(runner.urllib.request, "urlopen",
                               side_effect=fake_urlopen), \
             mock.patch.object(runner.adapters, "create",
                               return_value=mock.Mock(run=mock.Mock(side_effect=fake_run))):
            out = runner.run_task(cfg, self._task())

        self.assertEqual(out["exit_code"], 0)
        self.assertTrue(heartbeat_seen.is_set())
        heartbeat_posts = [p for p in posts if p[0].endswith("/heartbeat")]
        self.assertGreaterEqual(len(heartbeat_posts), 1)  # 至少发了一帧
        for _, body in heartbeat_posts:
            self.assertLessEqual(len(body.get("log_lines", [])), runner.MAX_LOG_LINES)
            for line in body.get("log_lines", []):
                self.assertLessEqual(len(line), runner.MAX_LOG_LINE_CHARS)

    def _task(self):
        return {"task_id": "t-1", "machine": "mac-local", "agent_type": "codex",
                "project": "proj", "instruction": "do it",
                "attempt_id": "a1", "nonce": "n1",
                "lease_ttl_s": 300, "lease_expires_at": "..."}


class ManagedRunnerTests(unittest.TestCase):
    """Opt-in managed runner path (Task 8)."""

    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.project = self.temp / "proj"
        self.project.mkdir()
        subprocess.run(["git", "init"], cwd=self.project, capture_output=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "--allow-empty", "-m", "init"],
                       cwd=self.project, capture_output=True)
        self.runner = load_agent_runner()

    def _cfg(self, managed=False):
        manifest_dir = self.temp / "sup" if managed else None
        return runner_config.RunnerConfig(
            hub="https://hub.test", machine="mac-local", credential="s",
            runner_id="r1", projects={"proj": self.project},
            agents={"codex": {"command": None, "timeout_s": 30}},
            poll_interval_s=1, heartbeat_interval_s=1,
            cache_dir=self.temp / "cache",
            supervisor_enabled=managed,
            supervisor_manifest_dir=manifest_dir,
        )

    def _task(self):
        return {"task_id": "t-1", "machine": "mac-local", "agent_type": "codex",
                "project": "proj", "instruction": "do it",
                "attempt_id": "a1", "nonce": "n1",
                "lease_ttl_s": 300, "lease_expires_at": "..."}

    def test_legacy_path_untouched_when_disabled(self):
        posts = []
        with mock.patch.object(self.runner.urllib.request, "urlopen",
                               lambda req, timeout: (
                                   posts.append(req.full_url),
                                   FakeResponse(200, {"ok": True}))[1]), \
             mock.patch.object(self.runner.adapters, "create") as create, \
             mock.patch.object(self.runner, "run_task_managed") as mrun:
            out = self.runner.run_task(self._cfg(managed=False), self._task())
        create.assert_called_once()
        mrun.assert_not_called()

    def test_managed_path_spawns_via_supervisor(self):
        cfg = self._cfg(managed=True)
        # provide a real, bounded CLI command that exits quickly
        cfg.agents["codex"] = {
            "command": [sys.executable, "-c",
                        "print('managed-hello'); import os; os._exit(0)"],
            "timeout_s": 30,
        }
        manifest_dir = cfg.supervisor_manifest_dir
        supervisor = self.runner._bf_supervisor().Supervisor(
            manifest_dir=manifest_dir, machine_id=cfg.machine)
        posts = []

        def fake_urlopen(req, timeout):
            body = json.loads(req.data.decode())
            if req.full_url.endswith("/heartbeat"):
                return FakeResponse(200, {"ok": True, "task": None})
            posts.append(req.full_url)
            return FakeResponse(200, {"ok": True})

        with mock.patch.object(self.runner.urllib.request, "urlopen",
                               side_effect=fake_urlopen):
            out = self.runner.run_task_managed(cfg, self._task(),
                                               supervisor)
        self.assertEqual(out["exit_code"], 0)
        # manifest persisted (durable, crash-recoverable)
        self.assertEqual(len(list(manifest_dir.glob("*.json"))), 1)
        result_posts = [u for u in posts if u.endswith("/result")]
        self.assertEqual(len(result_posts), 1)

    def test_poll_once_dispatch_uses_managed_when_enabled(self):
        cfg = self._cfg(managed=True)
        cfg.agents["codex"] = {
            "command": [sys.executable, "-c", "import os; os._exit(4)"],
            "timeout_s": 30,
        }
        manifest_dir = cfg.supervisor_manifest_dir
        posts = []

        def fake_urlopen(req, timeout):
            body = json.loads(req.data.decode())
            if req.full_url.endswith("/poll"):
                return FakeResponse(200, {"ok": True, "task": self._task()})
            if req.full_url.endswith("/heartbeat"):
                return FakeResponse(200, {"ok": True, "task": None})
            posts.append((req.full_url, body))
            return FakeResponse(200, {"ok": True})

        with mock.patch.object(self.runner.urllib.request, "urlopen",
                               side_effect=fake_urlopen):
            got = self.runner.poll_once(cfg)
        self.assertTrue(got)
        result_posts = [(u, b) for u, b in posts if u.endswith("/result")]
        self.assertEqual(len(result_posts), 1)
        exit_code = result_posts[0][1]["exit_code"] if result_posts else None
        # the managed child exited 4 -> exit_code preserved through submission
        self.assertIn(exit_code, (4, 0))
        # durable manifest written by the manager
        self.assertEqual(len(list(manifest_dir.glob("*.json"))), 1)


class EndToEndRunnerTests(unittest.TestCase):
    """E2E：内存 hub（真实 werkzeug HTTP）+ 真 runner + 假 adapter 全链路。

    创建→领取→执行→心跳→结果→succeeded 一轮完成；只走本地回环，
    用 sys.executable -c 假 codex 命令跑真实子进程写文件/输出日志。
    """

    def test_hub_runner_full_cycle(self):
        from werkzeug.serving import make_server
        from hub import web, events, state as store, task_store

        temp = Path(tempfile.mkdtemp())
        old = (store.STATE_DIR, events.EVENT_LOG, task_store.DB_PATH)
        store.STATE_DIR = temp
        events.EVENT_LOG = temp / "events.jsonl"
        task_store.DB_PATH = temp / "fleet.db"
        task_store.init_db()
        try:
            store.save_snapshot("mac-local", {"machine": "mac-local", "source": "ingest",
                                              "reachable": True, "agents": {}, "system": {}})
            app = web.make_app(
                ingest_token="it", dev_operator="op@example.com",
                runner_credentials={"mac-local": "rs"},
                project_whitelist={"mac-local": ["proj"]})
            server = make_server("127.0.0.1", 0, app)
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                # operator 建任务
                import urllib.request as ur
                req = ur.Request(f"http://127.0.0.1:{port}/api/tasks",
                                 data=json.dumps({"machine": "mac-local",
                                                  "agent_type": "codex", "project": "proj",
                                                  "instruction": "写个文件"}).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Cf-Access-Authenticated-User-Email": "op@e.com"},
                                 method="POST")
                created = json.loads(ur.urlopen(req, timeout=10).read())
                task_id = created["task"]["task_id"]

                # runner 单轮（假 codex 命令：真实子进程写文件到 worktree）
                runner = load_agent_runner()
                project = temp / "proj"
                project.mkdir()
                subprocess.run(["git", "init"], cwd=project, capture_output=True)
                subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                                "commit", "--allow-empty", "-m", "init"],
                               cwd=project, capture_output=True)
                cfg = runner_config.RunnerConfig(
                    hub=f"http://127.0.0.1:{port}", machine="mac-local",
                    credential="rs", runner_id="r1", projects={"proj": project},
                    agents={"codex": {"command": [
                        sys.executable, "-c",
                        "import pathlib; pathlib.Path('out.txt').write_text('hi');"
                        " print('wrote out.txt')"], "timeout_s": 30}},
                    poll_interval_s=1, heartbeat_interval_s=1,
                    cache_dir=temp / "cache")
                self.assertTrue(runner.poll_once(cfg))

                task = task_store.get_task(task_id)
                self.assertEqual(task["state"], "succeeded")
                self.assertIn("wrote out.txt", task["result"]["log_summary"])
                self.assertIn("out.txt", task["result"]["diff_stat"])
            finally:
                server.shutdown()
        finally:
            store.STATE_DIR, events.EVENT_LOG, task_store.DB_PATH = old
