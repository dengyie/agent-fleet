import hashlib
import sys

import pytest

from tools.platform.artifacts import ArtifactError, ArtifactStore
from tools.platform.backends.sandbox import SandboxBackend, SandboxPolicy
from tools.platform.backends.directory import DirectoryBackend
from tools.platform.resource_lease import ResourceLeaseManager
from tools.platform.tool_broker import ToolBroker


def test_artifact_store_manifest_is_bounded_and_owner_scoped(tmp_path):
    workspace = tmp_path / "workspace"
    source = workspace / "report.md"
    workspace.mkdir()
    source.write_text("hello", encoding="utf-8")
    store = ArtifactStore(tmp_path / "artifacts", clock=lambda: 10.0)
    manifest = store.put_file("owner-a", "workspace-a", source)
    assert manifest["size"] == 5
    assert manifest["sha256"] == hashlib.sha256(b"hello").hexdigest()
    assert store.read("owner-a", "workspace-a", manifest["artifact_id"]) == b"hello"
    assert store.get("owner-b", "workspace-a", manifest["artifact_id"]) is None
    assert store.list("owner-a", "workspace-a") == [manifest]
    with pytest.raises(ArtifactError) as denied:
        store.read("owner-a", "workspace-b", manifest["artifact_id"])
    assert denied.value.code == "not_found"


def test_artifact_store_rejects_bad_name_and_detects_corruption(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("payload", encoding="utf-8")
    store = ArtifactStore(tmp_path / "artifacts")
    with pytest.raises(ArtifactError) as bad:
        store.put_file("owner", "workspace", source, name="../secret")
    assert bad.value.code == "invalid_artifact_name"
    manifest = store.put_file("owner", "workspace", source)
    content = next((tmp_path / "artifacts").glob("*/content"))
    content.write_bytes(b"tampered")
    with pytest.raises(ArtifactError) as corrupt:
        store.read("owner", "workspace", manifest["artifact_id"])
    assert corrupt.value.code == "artifact_corrupt"


def test_artifact_list_filters_before_limit_and_validates_scope(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("payload", encoding="utf-8")
    store = ArtifactStore(tmp_path / "artifacts")
    store.put_file("owner-b", "workspace", source)
    expected = store.put_file("owner-a", "workspace", source)
    assert store.list("owner-a", "workspace", limit=1) == [expected]
    with pytest.raises(ArtifactError) as invalid:
        store.list("owner/a", "workspace")
    assert invalid.value.code == "invalid_scope"


def test_tool_broker_publishes_artifact_from_workspace(tmp_path):
    backend = DirectoryBackend(tmp_path / "workspace")
    backend.write("report.md", b"report")
    leases = ResourceLeaseManager()
    lease = leases.acquire("workspace-a", "run-a")
    broker = ToolBroker(
        backend, leases, resource_id="workspace-a",
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
    )
    receipt = broker.execute(
        command_id="artifact-1", tool="workspace.artifact",
        arguments={"path": "report.md"}, owner_id="run-a", epoch=lease["epoch"],
    )
    assert receipt.state == "succeeded"
    assert receipt.result["artifact"]["sha256"] == hashlib.sha256(b"report").hexdigest()


def test_artifact_store_rejects_control_characters_in_display_name(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("payload", encoding="utf-8")
    store = ArtifactStore(tmp_path / "artifacts")
    with pytest.raises(ArtifactError) as invalid:
        store.put_file("owner", "workspace", source, name="bad\nname")
    assert invalid.value.code == "invalid_artifact_name"


def test_artifact_preview_is_utf8_bounded_and_text_only(tmp_path):
    source = tmp_path / "report.md"
    source.write_bytes(("<script>literal</script>\n" + "界" * 40_000).encode("utf-8"))
    store = ArtifactStore(tmp_path / "artifacts")
    manifest = store.put_file("owner", "workspace", source, content_type="text/markdown")

    preview = store.read_preview("owner", "workspace", manifest["artifact_id"])

    assert preview["bytes"] == ArtifactStore.MAX_PREVIEW_BYTES
    assert preview["truncated"] is True
    assert preview["content_type"] == "text/markdown"
    assert preview["text"].startswith("<script>literal</script>")

    binary = tmp_path / "archive.bin"
    binary.write_bytes(b"binary")
    binary_manifest = store.put_file(
        "owner", "workspace", binary, content_type="application/octet-stream"
    )
    with pytest.raises(ArtifactError) as unsupported:
        store.read_preview("owner", "workspace", binary_manifest["artifact_id"])
    assert unsupported.value.code == "preview_unsupported"

    with pytest.raises(ArtifactError) as isolated:
        store.read_preview("other", "workspace", manifest["artifact_id"])
    assert isolated.value.code == "not_found"


def test_sandbox_fails_closed_without_explicit_launcher(tmp_path):
    backend = SandboxBackend(tmp_path / "sandbox")
    assert backend.execute("sandbox-1", ["python", "-c", "print('x')"]).error_code == "sandbox_unavailable"


def test_sandbox_with_explicit_launcher_still_uses_allowlist_and_timeout(tmp_path):
    launcher = [sys.executable, "-c", "import os,sys; os.execv(sys.argv[1], sys.argv[1:])"]
    backend = SandboxBackend(
        tmp_path / "sandbox", policy=SandboxPolicy(network_enabled=True, timeout_seconds=0.2), launcher=launcher,
    )
    assert backend.execute("sandbox-1", ["python", "-c", "print('x')"]).error_code == "command_not_allowed"
    timed = backend.execute("sandbox-2", ["python", "-m", "timeit", "-n", "1", "-r", "1", "pass"], timeout_s=0.1)
    assert timed.state in {"succeeded", "failed"}


def test_sandbox_rejects_invalid_launcher_and_timeout(tmp_path):
    with pytest.raises(ValueError):
        SandboxBackend(tmp_path / "invalid", launcher=["", "bad\x00arg"])
    backend = SandboxBackend(
        tmp_path / "sandbox", policy=SandboxPolicy(network_enabled=True),
        launcher=[sys.executable, "-c", "import os,sys; os.execv(sys.argv[1], sys.argv[1:])"],
    )
    assert backend.execute("sandbox-invalid-timeout", ["python", "-m", "timeit"], timeout_s=float("nan")).error_code == "invalid_timeout"
