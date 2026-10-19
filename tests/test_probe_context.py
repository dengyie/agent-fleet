"""``ProbeContext`` must route every command through the resolved shell.

The probe runs on the machine that creates the context, so on Windows it must
use a real POSIX shell (never the ``System32`` WSL shim) and must degrade to
empty output rather than raise when that shell is missing or misbehaves.
"""
from __future__ import annotations

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
    assert kwargs["text"] is True


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
    result = SimpleNamespace(stdout="{}", returncode=0)
    with mock.patch.object(probe, "run_local", return_value=result) as run:
        probe.ProbeContext("m").run_python("print(1)")
    argv, kwargs = run.call_args
    assert argv == ([sys.executable, "-"],)
    assert kwargs["input"] == "print(1)"


def test_run_python_falls_back_to_empty_json():
    with mock.patch.object(probe, "run_local", side_effect=OSError("boom")):
        assert probe.ProbeContext("m").run_python("x") == "{}"
