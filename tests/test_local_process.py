"""Regression guard for the centralised child-process launch policy.

Every console child the fleet spawns must go through
:mod:`tools.local_process` (or declare ``creationflags`` explicitly). A raw
``subprocess.run`` / ``Popen`` without the keyword is how the Windows
``pythonw`` flash-window and the silent ``UnicodeDecodeError`` regressions
crept back in, so this scans the runtime packages with an AST rather than
relying on review.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
from unittest import mock

from tools import local_process

ROOT = Path(__file__).resolve().parents[1]
SCANNED_PACKAGES = ("connectors", "hub", "tools")
EXCLUDED_PARTS = {"testing", "__pycache__"}
POLICY_MODULE = ROOT / "tools" / "local_process.py"
SPAWN_METHODS = {"run", "Popen", "call", "check_call", "check_output"}


def _runtime_modules():
    """Yield every shipped module whose spawns must honour the launch policy."""
    for package in SCANNED_PACKAGES:
        for path in sorted((ROOT / package).rglob("*.py")):
            if EXCLUDED_PARTS & set(path.relative_to(ROOT).parts):
                continue
            if path.resolve() == POLICY_MODULE.resolve():
                continue  # the policy module is the one place raw spawns live
            yield path


def _raw_subprocess_spawns(path: Path) -> list[int]:
    """Line numbers of ``subprocess.<spawn>(...)`` calls lacking ``creationflags``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in SPAWN_METHODS:
            continue
        if not isinstance(func.value, ast.Name) or func.value.id != "subprocess":
            continue
        if any(keyword.arg == "creationflags" for keyword in node.keywords):
            continue
        offenders.append(node.lineno)
    return offenders


def test_every_runtime_spawn_declares_creationflags():
    offenders = {}
    for path in _runtime_modules():
        lines = _raw_subprocess_spawns(path)
        if lines:
            offenders[path.relative_to(ROOT).as_posix()] = lines
    assert not offenders, (
        f"raw subprocess spawns bypass the launch policy: {offenders}. "
        "Use tools.local_process.run / tools.local_process.popen instead."
    )


def test_hidden_flag_is_defined_for_this_platform():
    if os.name == "nt":
        assert local_process.NO_WINDOW == local_process.subprocess.CREATE_NO_WINDOW
    else:
        assert local_process.NO_WINDOW == 0


def test_run_applies_hidden_window_and_lenient_decode():
    with mock.patch.object(local_process.subprocess, "run",
                           return_value="sentinel") as fake:
        assert local_process.run(["x"], capture_output=True, text=True) == "sentinel"
    argv, kwargs = fake.call_args
    assert argv == (["x"],)
    assert kwargs["creationflags"] == local_process.NO_WINDOW
    assert kwargs["errors"] == "replace"


def test_run_preserves_caller_creationflags_and_errors():
    with mock.patch.object(local_process.subprocess, "run",
                           return_value="sentinel") as fake:
        local_process.run(["x"], text=True, creationflags=0x00000200,
                          errors="strict")
    kwargs = fake.call_args.kwargs
    assert kwargs["creationflags"] == (0x00000200 | local_process.NO_WINDOW)
    assert kwargs["errors"] == "strict"


def test_binary_mode_never_forces_a_text_decoder():
    # ``errors`` is invalid without text mode; the policy must not add it.
    with mock.patch.object(local_process.subprocess, "run",
                           return_value=b"raw") as fake:
        local_process.run(["x"])
    assert "errors" not in fake.call_args.kwargs


def test_resolve_bash_never_returns_the_wsl_launcher():
    if os.name != "nt":
        return  # POSIX has no WSL/Store shim to avoid
    shell = local_process.resolve_bash()
    if shell is not None:
        normalized = shell.replace("/", "\\").lower()
        assert "system32" not in normalized
        assert "windowsapps" not in normalized


def test_resolve_bash_honours_override(tmp_path):
    if os.name != "nt":
        # POSIX resolves ``bash`` through the OS at spawn time; the override
        # only chooses among Windows shells.
        assert local_process.resolve_bash() == "bash"
        return
    real = tmp_path / "mybash.exe"
    real.write_text("")
    local_process.resolve_bash.cache_clear()
    try:
        with mock.patch.dict(os.environ, {"AGENT_FLEET_BASH": str(real)}):
            assert local_process.resolve_bash() == str(real)
    finally:
        local_process.resolve_bash.cache_clear()


def test_resolve_bash_is_memoised(monkeypatch):
    local_process.resolve_bash.cache_clear()
    calls = {"which": 0}
    original_which = local_process.shutil.which

    def counting_which(*args, **kwargs):
        calls["which"] += 1
        return original_which(*args, **kwargs)

    monkeypatch.setattr(local_process.shutil, "which", counting_which)
    try:
        first = local_process.resolve_bash()
        after_first = calls["which"]
        assert local_process.resolve_bash() == first
        assert calls["which"] == after_first  # second call re-scanned nothing
    finally:
        local_process.resolve_bash.cache_clear()
