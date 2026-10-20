"""Regression guard for the centralised child-process launch policy.

Every console child the fleet spawns must go through
:mod:`tools.local_process`. A raw ``subprocess`` spawn is how the Windows
``pythonw`` flash-window and the silent ``UnicodeDecodeError`` regressions
crept back in, so this scans the runtime packages with an AST rather than
relying on review — including spawns spelled through an alias, imported
directly, or referenced as a value (``runner or subprocess.run``).
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
        package_root = ROOT / package
        if not package_root.is_dir():
            raise AssertionError(f"scanned package missing: {package}")
        for path in sorted(package_root.rglob("*.py")):
            if EXCLUDED_PARTS & set(path.relative_to(ROOT).parts):
                continue
            if path.resolve() == POLICY_MODULE.resolve():
                continue  # the policy module is the one place raw spawns live
            yield path


def _subprocess_bindings(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Names denoting the ``subprocess`` module, and directly imported spawns."""
    modules = {"subprocess"}
    callables: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for alias in node.names:
                if alias.name in SPAWN_METHODS:
                    callables.add(alias.asname or alias.name)
    return modules, callables


def _raw_subprocess_spawns(path: Path) -> list[int]:
    """Line numbers of any raw ``subprocess`` spawn, however it is spelled."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules, callables = _subprocess_bindings(tree)
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    offenders: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if not (isinstance(node.value, ast.Name) and node.value.id in modules):
                continue
            if node.attr not in SPAWN_METHODS:
                continue
            parent = parents.get(node)
            if isinstance(parent, ast.Subscript) and parent.value is node:
                continue  # ``subprocess.Popen[bytes]`` is only a type annotation
            offenders.add(node.lineno)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id in callables):
            offenders.add(node.lineno)
    return sorted(offenders)


def test_every_runtime_spawn_goes_through_the_policy():
    offenders = {}
    for path in _runtime_modules():
        lines = _raw_subprocess_spawns(path)
        if lines:
            offenders[path.relative_to(ROOT).as_posix()] = lines
    assert not offenders, (
        f"raw subprocess spawns bypass the launch policy: {offenders}. "
        "Use tools.local_process.run / tools.local_process.popen instead."
    )


def test_guard_scans_the_runtime_packages():
    scanned = {path.relative_to(ROOT).as_posix() for path in _runtime_modules()}
    assert {"connectors/probe.py", "hub/web.py", "tools/worktree.py"} <= scanned


def test_guard_flags_every_spelling_of_a_raw_spawn(tmp_path):
    module = tmp_path / "offender.py"
    module.write_text(
        "import subprocess\n"
        "import subprocess as sp\n"
        "from subprocess import call\n"
        "subprocess.run(['a'])\n"
        "sp.check_output(['b'])\n"
        "call(['c'])\n"
        "runner = None or subprocess.Popen\n"
        "annotation = subprocess.Popen[bytes]\n",
        encoding="utf-8",
    )
    assert _raw_subprocess_spawns(module) == [4, 5, 6, 7]


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


def test_popen_applies_the_same_policy():
    with mock.patch.object(local_process.subprocess, "Popen",
                           return_value="sentinel") as fake:
        assert local_process.popen(["x"], capture_output=True,
                                   text=True) == "sentinel"
    argv, kwargs = fake.call_args
    assert argv == (["x"],)
    assert kwargs["creationflags"] == local_process.NO_WINDOW
    assert kwargs["errors"] == "replace"


def test_popen_binary_mode_never_forces_a_text_decoder():
    with mock.patch.object(local_process.subprocess, "Popen",
                           return_value="sentinel") as fake:
        local_process.popen(["x"])
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


def test_resolve_bash_rejects_a_shim_override(tmp_path):
    if os.name != "nt":
        return  # POSIX has no shim to reject; covered by the Windows CI job
    shim_dir = tmp_path / "WindowsApps"
    shim_dir.mkdir()
    shim = shim_dir / "bash.exe"
    shim.write_text("")
    local_process.resolve_bash.cache_clear()
    try:
        with mock.patch.dict(os.environ, {"AGENT_FLEET_BASH": str(shim)}):
            assert local_process.resolve_bash() != str(shim)
    finally:
        local_process.resolve_bash.cache_clear()


def test_resolve_bash_is_memoised():
    local_process.resolve_bash.cache_clear()
    try:
        first = local_process.resolve_bash()
        hits = local_process._resolve_bash_cached.cache_info().hits
        assert local_process.resolve_bash() == first
        assert local_process._resolve_bash_cached.cache_info().hits == hits + 1
    finally:
        local_process.resolve_bash.cache_clear()
