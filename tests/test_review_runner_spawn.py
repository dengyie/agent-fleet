"""Review regressions: child spawn policy (findings 1-3)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import adapters, worktree
from tools.supervisor import supervisor as sup_mod


class LegacyEnvTests(unittest.TestCase):
    def test_legacy_adapter_child_env_has_no_fleet_or_token_keys(self):
        workdir = Path(tempfile.mkdtemp())
        out = workdir / "env.json"
        adapter = adapters.create("codex", command=[
            sys.executable, "-c",
            "import json,os,sys; json.dump(dict(os.environ), open(sys.argv[1],'w'))",
            str(out), "{instruction}"], timeout_s=30)
        leaked = {"AGENT_FLEET_RUNNER_CREDENTIAL": "x", "GITHUB_TOKEN": "x",
                  "ANTHROPIC_AUTH_TOKEN": "x", "SOME_RANDOM_SECRET": "x"}
        with mock.patch.dict(os.environ, leaked):
            result = adapter.run("hello", workdir)
        self.assertEqual(result.exit_code, 0, result.log_tail)
        env = json.loads(out.read_text())
        bad = [k for k in env if k.startswith("AGENT_FLEET_") or k.endswith("_TOKEN")
               or k == "SOME_RANDOM_SECRET"]
        self.assertEqual(bad, [])
        self.assertIn("PATH", env)


class LeadingDashTests(unittest.TestCase):
    def test_default_codex_inserts_end_of_options(self):
        argv = adapters.create("codex").build_argv("--help")
        self.assertEqual(argv[-2:], ["--", "--help"])

    def test_default_claude_inserts_end_of_options(self):
        argv = adapters.create("claude_code").build_argv("-p")
        self.assertEqual(argv[-2:], ["--", "-p"])

    def test_pi_and_custom_commands_refuse(self):
        workdir = Path(tempfile.mkdtemp())
        marker = workdir / "ran"
        custom = adapters.create("codex", command=[
            sys.executable, "-c", "import sys; open(sys.argv[1],'w').write('x')",
            str(marker), "{instruction}"], timeout_s=30)
        result = custom.run("--help", workdir)
        self.assertEqual(result.exit_code, 64)
        self.assertFalse(marker.exists())
        pi = adapters.create("pi", command=[sys.executable, "-c", "pass", "{instruction}"])
        self.assertEqual(pi.run("-x", workdir).exit_code, 64)

    def test_plain_instruction_unchanged(self):
        argv = adapters.create("codex").build_argv("fix bug")
        self.assertEqual(argv[-1], "fix bug")
        self.assertNotIn("--", argv)

    def test_native_resume_guards_leading_dash(self):
        self.assertEqual(sup_mod._native_resume_argv("codex", "codex", "tok", "--help")[-2:],
                         ["--", "--help"])
        self.assertEqual(sup_mod._native_resume_argv("claude_code", "claude", "tok", "-x")[-2:],
                         ["--", "-x"])
        self.assertEqual(sup_mod._native_resume_argv("pi", "pi", "tok", "--help"), [])
        self.assertEqual(sup_mod._native_resume_argv("codex", "codex", "--evil", "hi"), [])

    def test_windows_batch_refuses_untrusted_args(self):
        from tools.spawn_policy import UnsafeInstruction, refuse_windows_batch
        with mock.patch("tools.spawn_policy.os.name", "nt"):
            with self.assertRaises(UnsafeInstruction):
                refuse_windows_batch([r"C:\tools\codex.cmd", "exec", "x & calc"], "x & calc")


@unittest.skipIf(os.name == "nt", "fsmonitor hook script is POSIX shell")
class WorktreeGitHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.project = self.temp / "proj"
        self.project.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.project, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "--allow-empty", "-m", "init"],
                       cwd=self.project, check=True)
        self.marker = self.temp / "hook-ran"
        hook = self.temp / "evil-hook.sh"
        hook.write_text(f"#!/bin/sh\ntouch '{self.marker}'\nexit 0\n")
        hook.chmod(0o755)
        self.hook = hook

    def _redirect_gitdir(self, wt: Path) -> None:
        """模拟 agent 把 worktree 的 .git 指向自己伪造的 gitdir（含 fsmonitor 钩子）。"""
        evil = self.temp / "evil-gitdir"
        subprocess.run(["git", "init", "-q", "--bare", str(evil)], check=True)
        subprocess.run(["git", "--git-dir", str(evil), "config", "core.bare", "false"], check=True)
        subprocess.run(["git", "--git-dir", str(evil), "config", "core.fsmonitor",
                        str(self.hook)], check=True)
        hooks = evil / "hooks"
        hooks.mkdir(exist_ok=True)
        for name in ("pre-commit", "post-index-change"):
            (hooks / name).write_text(self.hook.read_text())
            (hooks / name).chmod(0o755)
        (wt / ".git").write_text(f"gitdir: {evil}\n")

    def test_redirected_gitdir_hook_never_runs(self):
        wt = worktree.create_worktree(self.project, "t-harden")
        try:
            (wt / "new.txt").write_text("hello\n")
            self._redirect_gitdir(wt)
            try:
                stat = worktree.diff_stat(wt)
                patch = worktree.diff_patch(wt)
            except worktree.WorktreeError:
                stat = patch = None
            self.assertFalse(self.marker.exists(), "fsmonitor hook executed")
            # 钉死真实 gitdir 后 diff 仍基于原 worktree 正常产出
            self.assertIn("new.txt", stat or "")
            self.assertIn("hello", patch or "")
        finally:
            worktree.cleanup_worktree(self.project, wt, "t-harden")

    def test_unregistered_gitfile_is_refused(self):
        wt = self.temp / "loose"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {self.temp / 'nowhere'}\n")
        with self.assertRaises(worktree.WorktreeError):
            worktree.diff_stat(wt)

    def test_hardening_flags_and_env(self):
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"], seen["env"] = argv, kw.get("env")

            class P:
                returncode, stdout, stderr = 0, "", ""
            return P()

        with mock.patch.object(worktree, "run_local", fake_run), \
                mock.patch.dict(os.environ, {"AGENT_FLEET_X": "1", "GIT_DIR": "/evil"}):
            worktree._git(self.project, "status")
        argv, env = seen["argv"], seen["env"]
        for flag in ("core.fsmonitor=false", f"core.hooksPath={os.devnull}",
                     "core.untrackedCache=false"):
            self.assertIn(flag, argv)
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(Path(env["GIT_DIR"]), (self.project / ".git").resolve())
        self.assertNotIn("AGENT_FLEET_X", env)


if __name__ == "__main__":
    unittest.main()
