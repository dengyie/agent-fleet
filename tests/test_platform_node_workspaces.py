"""Resource routing must select the explicitly configured Node workspace."""
import hashlib
from dataclasses import replace
import shutil

import pytest

from fleet_core.backends.directory import DirectoryBackend
from fleet_core.control import generate_ed25519_keypair
from fleet_core.platform_command import PlatformCommand
from tools.platform.node_executor import NodeToolExecutor
from tools.platform.node_runtime import NodeRuntime, NodeRuntimeConfig

PRIVATE_KEY, PUBLIC_KEY = generate_ed25519_keypair()


def signed(resource_id, *, action="tool.workspace.read", arguments=None, command_id=None):
    return PlatformCommand.create(command_id=command_id or "read-" + resource_id, target_node="node-1",
        resource_id=resource_id, action=action, arguments=arguments or {"path": "same.txt"},
        expires_at=9999999999).signed(PRIVATE_KEY).as_dict()


def config(tmp_path, **kwargs):
    return NodeRuntimeConfig(node_id="node-1", credential="node-1:secret", public_key=PUBLIC_KEY,
        workspace_root=tmp_path / "a", journal_path=tmp_path / "journal.db",
        hub_url="https://hub.example", capabilities=frozenset({"workspace.read", "workspace.write", "reconcile.workspace.digest"}), **kwargs)


def test_single_root_must_not_silently_serve_another_resource(tmp_path):
    root = tmp_path / "a"
    root.mkdir()
    (root / "same.txt").write_text("from-A", encoding="utf-8")
    runtime = NodeRuntime(config(tmp_path, workspace_id="A"))
    result = runtime.client.handle(signed("B"))
    assert result["status"] == "failed"
    assert result["result"]["error_code"] == "workspace_unavailable"


def test_workspace_capability_requires_an_explicit_binding(tmp_path):
    with pytest.raises(ValueError, match="explicit workspace binding required"):
        config(tmp_path)


def test_runtime_routes_same_filename_to_two_explicit_roots(tmp_path):
    for name in ("a", "b"):
        root = tmp_path / name
        root.mkdir()
        (root / "same.txt").write_text("from-" + name, encoding="utf-8")
    runtime = NodeRuntime(config(tmp_path, workspace_roots={"A": tmp_path / "a", "B": tmp_path / "b"}))
    assert runtime.client.handle(signed("A"))["result"]["result"]["content"] == "from-a"
    assert runtime.client.handle(signed("B"))["result"]["result"]["content"] == "from-b"
    written = runtime.client.handle(signed("B", action="tool.workspace.write", command_id="write-b",
        arguments={"path": "same.txt", "content": "changed-b"}))
    assert written["status"] == "succeeded"
    assert (tmp_path / "a" / "same.txt").read_text() == "from-a"
    assert (tmp_path / "b" / "same.txt").read_text() == "changed-b"
    digest = runtime.client.handle(signed("B", action="reconcile.workspace.digest", command_id="digest-b"))
    assert digest["result"]["result"]["sha256"] == hashlib.sha256(b"changed-b").hexdigest()


def test_single_root_remains_available_with_explicit_resource_binding(tmp_path):
    runtime = NodeRuntime(config(tmp_path, workspace_id="A"))
    result = runtime.client.handle(signed("A", action="tool.workspace.write",
        arguments={"path": "same.txt", "content": "explicit-A"}))
    assert result["status"] == "succeeded"
    assert runtime.client.handle(signed("B"))["result"]["error_code"] == "workspace_unavailable"


def test_exec_cwd_and_list_follow_the_same_resource_mapping(tmp_path):
    if shutil.which("pwd") is None:
        pytest.skip("pwd executable unavailable")
    roots = {name: DirectoryBackend(tmp_path / name) for name in ("A", "B")}
    (roots["A"].root / "only-a.txt").write_text("A", encoding="utf-8")
    (roots["B"].root / "only-b.txt").write_text("B", encoding="utf-8")
    executor = NodeToolExecutor(None, workspace_backends=roots)
    for resource_id, backend in roots.items():
        execution = executor(signed(resource_id, action="tool.workspace.exec", arguments={"argv": ["pwd"]}))
        assert execution["state"] == "succeeded"
        assert execution["result"]["output"].strip() == str(backend.root)
        listing = executor(signed(resource_id, action="tool.workspace.list", arguments={"path": ""}))
        assert listing["result"]["entries"] == [{"name": f"only-{resource_id.lower()}.txt", "kind": "file"}]


def test_direct_executor_requires_a_binding_for_workspace_commands(tmp_path):
    backend = DirectoryBackend(tmp_path / "root")
    (backend.root / "same.txt").write_text("wrong-resource", encoding="utf-8")
    executor = NodeToolExecutor(backend)
    assert executor(signed("B"))["error_code"] == "workspace_unavailable"


@pytest.mark.parametrize("action,arguments", [
    ("tool.workspace.list", {"path": ""}),
    ("tool.workspace.read", {"path": "same.txt"}),
    ("tool.workspace.write", {"path": "same.txt", "content": "wrong"}),
    ("tool.workspace.exec", {"argv": ["pwd"]}),
    ("reconcile.workspace.digest", {"path": "same.txt"}),
])
def test_unknown_resource_never_uses_bound_backend(tmp_path, action, arguments):
    class Backend:
        def __getattr__(self, name):
            raise AssertionError("unknown resource touched backend: " + name)

    executor = NodeToolExecutor(Backend(), workspace_id="A")
    result = executor(signed("B", action=action, arguments=arguments))
    assert result["state"] == "failed" and result["error_code"] == "workspace_unavailable"


def test_bindings_are_copied_and_single_root_config_can_be_replaced(tmp_path):
    supplied = {"B": tmp_path / "b"}
    initial = config(tmp_path, workspace_id="A", workspace_roots=supplied)
    supplied["B"] = tmp_path / "wrong"
    assert initial.workspace_roots["B"] == (tmp_path / "b").resolve()
    again = replace(initial, worker_id="worker-2")
    assert dict(again.workspace_roots) == dict(initial.workspace_roots)
    with pytest.raises(TypeError):
        initial.workspace_roots["B"] = tmp_path / "wrong"


def test_conflicting_single_root_binding_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="duplicate workspace binding"):
        config(tmp_path, workspace_id="A", workspace_roots={"A": tmp_path / "other"})


@pytest.mark.parametrize("bindings", [{"": "/tmp/a"}, {"bad\nID": "/tmp/a"}, {"A": ""}, {"A": None}])
def test_invalid_bindings_fail_before_creating_runtime(tmp_path, bindings):
    with pytest.raises(ValueError):
        config(tmp_path, workspace_roots=bindings)
    assert not (tmp_path / "journal.db").exists()


def test_non_workspace_runtime_does_not_create_an_unused_workspace(tmp_path):
    runtime = NodeRuntime(NodeRuntimeConfig(node_id="node-1", credential="node-1:s",
        workspace_root=tmp_path / "unused", journal_path=tmp_path / "journal.db",
        hub_url="https://hub.example"))
    assert runtime.workspace_backends == {}
    assert not (tmp_path / "unused").exists()
    assert not runtime.executor.workspace_backends


def test_backend_exception_remains_unknown_with_the_original_resource_binding(tmp_path):
    class Interrupted:
        def read(self, path):
            raise OSError("interrupted")

    runtime = NodeRuntime(config(tmp_path, workspace_id="A"))
    runtime.executor.workspace_backends["A"] = Interrupted()
    assert runtime.client.handle(signed("A"))["status"] == "unknown"
