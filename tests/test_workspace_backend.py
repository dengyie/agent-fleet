import sys

import pytest

from fleet_core.workspace import normalize_relative_path
from fleet_core.backends.directory import DirectoryBackend
from fleet_core.backends.sandbox import SandboxBackend
from hub.platform.resource_lease import ResourceLeaseError, ResourceLeaseManager
from hub.platform.tool_broker import ToolBroker


def test_directory_backend_rejects_traversal_and_symlink_escape(tmp_path):
    root = tmp_path / "workspace"
    backend = DirectoryBackend(root)
    assert normalize_relative_path("reports/out.md") == "reports/out.md"
    for path in ("../outside", "/etc/passwd", "C:/temp", "reports/../x"):
        result = backend.read(path)
        assert result.error_code in {"path_escape", "not_found"}
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    assert backend.write("link/escape.txt", b"x").error_code == "path_escape"


def test_workspace_write_lease_and_tool_allowlist(tmp_path):
    backend = DirectoryBackend(tmp_path / "workspace")
    leases = ResourceLeaseManager()
    lease = leases.acquire("workspace-1", "run-1")
    broker = ToolBroker(backend, leases, resource_id="workspace-1")
    ok = broker.execute(command_id="c1", tool="workspace.write", arguments={"path": "a.md", "content": "hello"}, owner_id="run-1", epoch=lease["epoch"])
    assert ok.state == "succeeded"
    assert broker.execute(command_id="c2", tool="shell.raw", arguments={}, owner_id="run-1", epoch=lease["epoch"]).error_code == "unknown_tool"
    assert broker.execute(command_id="c2b", tool="workspace.exec", arguments={"argv": ["sh", "-c", "touch escape"]}, owner_id="run-1", epoch=lease["epoch"]).error_code == "command_not_allowed"
    assert broker.execute(command_id="c3", tool="workspace.read", arguments={"path": "a.md"}, owner_id="other", epoch=lease["epoch"]).error_code == "lease_mismatch"


@pytest.mark.parametrize("sandboxed", [False, True])
def test_workspace_executes_current_runtime_without_a_generic_alias(tmp_path, monkeypatch, sandboxed):
    from fleet_core.backends import directory

    # A fixed recipe pins the actual interpreter, whose name may be
    # python3.14 or python.exe rather than either generic Python alias.
    monkeypatch.setattr(directory, "ALLOWED_EXECUTABLES", frozenset({"git", "pwd"}))
    root = tmp_path / "workspace"
    if sandboxed:
        launcher = tmp_path / "forward.py"
        launcher.write_text("import os, sys\nos.execv(sys.argv[1], sys.argv[1:])\n", encoding="utf-8")
        backend = SandboxBackend(root, launcher=(sys.executable, str(launcher)))
    else:
        backend = DirectoryBackend(root)
    script = root / "runtime_probe.py"
    script.write_text("from pathlib import Path\nPath('executed').write_text('once')\nprint('runtime-ok')\n", encoding="utf-8")
    receipt = backend.execute("runtime-probe", [sys.executable, str(script)])
    assert receipt.state == "succeeded", receipt
    assert receipt.result["returncode"] == 0 and "runtime-ok" in receipt.result["output"]
    assert (root / "executed").read_text(encoding="utf-8") == "once"


@pytest.mark.parametrize("sandboxed", [False, True])
def test_workspace_runtime_admission_rejects_unregistered_version_names(tmp_path, sandboxed):
    root = tmp_path / "workspace"
    backend = (SandboxBackend(root, launcher=(sys.executable,)) if sandboxed else DirectoryBackend(root))
    executable = root / "python3.987"
    executable.write_text("#!/bin/sh\ntouch unexpected-execution\n", encoding="utf-8")
    executable.chmod(0o700)
    receipt = backend.execute("unregistered-runtime", [str(executable)])
    assert receipt.state == "failed" and receipt.error_code == "command_not_allowed"
    assert not (root / "unexpected-execution").exists()


@pytest.mark.parametrize("backend_type", [DirectoryBackend, SandboxBackend])
def test_workspace_overwrite_quota_counts_replacement_size(tmp_path, backend_type):
    backend = backend_type(tmp_path / "workspace", quota_bytes=10)
    assert backend.write("keep.txt", b"keep").state == "succeeded"
    assert backend.write("replace.txt", b"123456").state == "succeeded"
    # Repeated equal-sized edits and shrinking a full workspace must work.
    assert backend.write("replace.txt", b"abcdef").state == "succeeded"
    assert backend.write("replace.txt", b"ab").state == "succeeded"
    # Growing back to the quota boundary is allowed; exceeding it preserves
    # the previously saved file and the other workspace contents.
    assert backend.write("replace.txt", b"abcdef").state == "succeeded"
    assert backend.write("replace.txt", b"abcdefg").error_code == "disk_quota"
    assert backend.write("new.txt", b"x").error_code == "disk_quota"
    assert (backend.root / "replace.txt").read_bytes() == b"abcdef"
    assert (backend.root / "keep.txt").read_bytes() == b"keep"
    assert not (backend.root / "new.txt").exists()


def test_resource_lease_epoch_cannot_repeat_after_release():
    leases = ResourceLeaseManager(clock=lambda: 1)
    original = leases.acquire("workspace", "run")
    leases.release("workspace", "run", original["epoch"])
    current = leases.acquire("workspace", "run")
    assert current["epoch"] != original["epoch"]
    assert not leases.validate("workspace", "run", original["epoch"])


def test_parallel_resource_leases_preserve_capacity_and_independent_fencing():
    now = [1.0]
    leases = ResourceLeaseManager(clock=lambda: now[0], capacity=2)
    first = leases.acquire("workspace", "first", ttl_s=3)
    second = leases.acquire("workspace", "second", ttl_s=10)
    with pytest.raises(ResourceLeaseError, match="workspace_busy"):
        leases.acquire("workspace", "third")
    assert leases.validate("workspace", "first", first["epoch"])
    assert leases.validate("workspace", "second", second["epoch"])
    leases.release("workspace", "first", first["epoch"])
    third = leases.acquire("workspace", "third", ttl_s=1)
    assert leases.validate("workspace", "second", second["epoch"])
    now[0] = 3
    fourth = leases.acquire("workspace", "fourth")
    assert not leases.validate("workspace", "third", third["epoch"])
    assert leases.validate("workspace", "fourth", fourth["epoch"])
    with pytest.raises(ResourceLeaseError, match="lease_mismatch"):
        leases.renew("workspace", "third", third["epoch"])
