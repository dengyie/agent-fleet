"""``ProbeContext`` must route every command through the resolved shell.

The probe runs on the machine that creates the context, so on Windows it must
use a real POSIX shell (never the ``System32`` WSL shim) and must degrade to
empty output rather than raise when that shell is missing or misbehaves.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest import mock

from connectors import probe


def test_run_executes_through_the_resolved_shell():
    result = SimpleNamespace(stdout="ok\n", returncode=0)
    with mock.patch.object(probe, "resolve_bash", return_value="/fake/bash"), \
            mock.patch.object(probe, "run_local", return_value=result) as run:
        assert probe.ProbeContext("m").run("printf ok") == "ok"
    argv, kwargs = run.call_args
    assert argv == (["/fake/bash", "-c", "printf ok"],)
    assert kwargs["timeout"] == 10


def test_run_returns_empty_when_no_real_shell_exists():
    with mock.patch.object(probe, "resolve_bash", return_value=None), \
            mock.patch.object(probe, "run_local") as run:
        assert probe.ProbeContext("m").run("printf ok") == ""
    run.assert_not_called()


def test_run_survives_a_none_stdout():
    result = SimpleNamespace(stdout=None, returncode=0)
    with mock.patch.object(probe, "resolve_bash", return_value="/fake/bash"), \
            mock.patch.object(probe, "run_local", return_value=result):
        assert probe.ProbeContext("m").run("x") == ""


def test_run_swallows_transport_failures():
    with mock.patch.object(probe, "resolve_bash", return_value="/fake/bash"), \
            mock.patch.object(probe, "run_local", side_effect=OSError("boom")):
        assert probe.ProbeContext("m").run("x") == ""


def test_run_python_uses_the_running_interpreter():
    seen = {}

    def fake(argv, **kwargs):
        seen["argv"] = argv
        with open(argv[-1], encoding="utf-8") as handle:
            seen["script"] = handle.read()
        return SimpleNamespace(stdout="{}", returncode=0)

    with mock.patch.object(probe, "run_local", side_effect=fake):
        probe.ProbeContext("m").run_python("print(1)")
    assert seen["argv"][:3] == [sys.executable, "-c", probe._RUN_SCRIPT]
    assert seen["script"] == "print(1)"
    assert not os.path.exists(seen["argv"][-1])  # temp script removed


def test_run_python_really_runs_the_script_with_cwd_on_path():
    out = probe.ProbeContext("m").run_python(
        "import json,sys\nprint(json.dumps({'p0': sys.path[0], 'argv': sys.argv}))")
    data = json.loads(out)
    assert data == {"p0": "", "argv": ["-"]}


def test_run_is_bounded_by_run_bounded(monkeypatch):
    calls = []

    def fake(argv, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(argv, 0, "x".encode() + b"\xff", b"")

    monkeypatch.setattr(probe, "run_bounded", fake)
    monkeypatch.setattr(probe, "resolve_bash", lambda: "/fake/bash")
    assert probe.ProbeContext("m", timeout=3).run("x") == "x\ufffd"
    assert calls[0]["timeout_s"] == 3
    assert calls[0]["stdout_limit"] == probe.MAX_PROBE_OUTPUT
    assert calls[0]["stderr_limit"] == 0


def test_run_python_falls_back_to_empty_json():
    with mock.patch.object(probe, "run_local", side_effect=OSError("boom")):
        assert probe.ProbeContext("m").run_python("x") == "{}"
