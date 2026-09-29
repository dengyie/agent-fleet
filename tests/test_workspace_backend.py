from hub.domain.workspace import WorkspaceError, normalize_relative_path
from tools.platform.backends.directory import DirectoryBackend
from tools.platform.resource_lease import ResourceLeaseManager
from tools.platform.tool_broker import ToolBroker


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
