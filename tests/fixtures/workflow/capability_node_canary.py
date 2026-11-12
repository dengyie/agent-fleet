"""Explicit fixed workspace capability pilot; stdin is never persisted."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from fleet_core.capability import CapabilityDefinition
from fleet_core.workflow.contract_types import CatalogRef
from tools.platform.node_capabilities import LocalCapability, NodeCapabilityConfig
from tools.platform.node_runtime import NodeRuntime, NodeRuntimeConfig
from tools.transport import Transport

_CASE = re.compile(r"capability-[0-9a-f]{16}")
_ACTIONS = ("tool.workspace.write", "reconcile.workspace.digest")


def pilot_definitions() -> tuple[CapabilityDefinition, CapabilityDefinition]:
    def definition(name: str, retry: str, risk: str) -> CapabilityDefinition:
        return CapabilityDefinition(name, 1, "cli", "in-v1", "out-v1", b'{"type":"object"}',
            b'{"type":"object"}', risk, retry, ("operator",), 1800, 65536)
    return (definition("workspace.files", "reconcile_before_retry", "workspace_write"),
        definition("workspace.digest", "read_only", "workspace_read"))


class _BoundedTransport(Transport):
    def post_json(self, url: str, body: dict[str, Any], headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        import time
        return self.request_bounded_json("POST", url, json.dumps(body, allow_nan=False).encode("utf-8"),
            headers, deadline=time.monotonic() + 15, should_cancel=lambda: False)


def _private(path: Path) -> Path:
    if os.name == "nt":
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from fixtures.workflow.windows_artifact_probe import private_directory
        return private_directory(path)
    path.mkdir(mode=0o700)
    return path


def _runtime(root: Path, config: dict[str, Any]) -> NodeRuntime:
    definitions = pilot_definitions()
    refs = tuple(CatalogRef(item.capability_id, item.revision) for item in definitions)
    registrations = tuple(LocalCapability(item, (action,)) for item, action in zip(definitions, _ACTIONS))
    local = NodeCapabilityConfig(1, registrations, refs, slots=1)
    return NodeRuntime(NodeRuntimeConfig(workspace_id=config.get("workspace_id", "workspace"), node_id="node-a", credential=config["credential"],
        workspace_root=root / "workspace", journal_path=root / "node.db", hub_url=config["hub_url"],
        public_key=bytes.fromhex(config["public_key"]), worker_id="pilot-node",
        capabilities=frozenset({"workspace.write", "reconcile.workspace.digest"}), capability_config=local),
        transport=_BoundedTransport(mode="direct", attempts=1), clock=lambda: config["now"])


def _prepare(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    _private(root)
    _private(root / "workspace")
    runtime = _runtime(root, config)
    try:
        receipt = runtime.report_capabilities()
        offer = runtime.capability_service.offer(now=config["now"])
        assert receipt.node_id == "node-a" and receipt.offer_revision == offer.offer_revision
        assert offer.memory_bytes == 0 and offer.disk_bytes >= 200
        assert {item.id for item in offer.capabilities} == {item.capability_id for item in pilot_definitions()}
        return {"offer": offer.payload(), "offer_digest": offer.config_digest}
    finally:
        runtime.close_all()


def _write(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    interrupt = config.get("interrupt", True)
    assert type(interrupt) is bool
    runtime = _runtime(root, config)
    calls: list[str] = []
    original_write = runtime.backend.write
    def counted_write(path: str, content: bytes) -> Any:
        calls.append(path)
        receipt = original_write(path, content)
        assert receipt.state == "succeeded"
        if interrupt:
            raise OSError("fixed pilot lost completion after successful write")
        return receipt
    runtime.backend.write = counted_write
    try:
        result = runtime.poll_once(lease_s=30)
        assert result == {"ok": True, "commands": 1, "receipts": 1}, result
        original = runtime.journal.get(config["command_id"])
        assert original is not None and original["state"] == ("unknown" if interrupt else "succeeded")
        assert calls == ["report.txt"]
        raw = (root / "workspace/report.txt").read_bytes()
        assert raw == b"fixed output"
        report = {"poll": result, "original": original, "write_calls": len(calls),
            "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        (root / "written.json").write_text(json.dumps(report) + "\n", encoding="utf-8")
        return report
    finally:
        runtime.close_all()


def _replay(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    written = json.loads((root / "written.json").read_text(encoding="utf-8"))
    command = config["original"]
    runtime = _runtime(root, config)
    calls = {"io": 0}

    def forbidden_io(*arguments: Any) -> Any:
        calls["io"] += 1
        raise AssertionError("terminal journal replay must not execute I/O")

    runtime.backend.write = runtime.backend.digest = forbidden_io
    try:
        before = runtime.journal.get(command["command_id"])
        assert before == written["original"] and before["state"] in {"unknown", "succeeded"}
        for _ in range(2):
            assert runtime.client.handle(command)["status"] == before["state"]
        poll = runtime.poll_once(lease_s=30)
        assert poll == {"ok": True, "commands": 0, "receipts": 0}, poll
        assert runtime.journal.get(command["command_id"]) == before and calls["io"] == 0
        return {"original": before, "restart_io": calls["io"], "poll": poll}
    finally:
        runtime.close_all()


def _postcheck(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    expected = {"matched": "succeeded", "mismatch": "succeeded", "read_failed": "failed"}
    outcome = config["outcome"]
    assert outcome in expected
    written = json.loads((root / "written.json").read_text(encoding="utf-8"))
    output = root / "workspace/report.txt"
    assert hashlib.sha256(output.read_bytes()).hexdigest() == written["sha256"]
    if outcome != "matched":
        output.rename(root / "workspace/retained-original.txt")
    if outcome == "mismatch":
        output.write_bytes(b"different fixed fixture bytes")
    runtime = _runtime(root, config)
    calls = {"write": 0, "digest": 0}
    original_digest = runtime.backend.digest
    def forbidden_write(path: str, content: bytes) -> Any:
        calls["write"] += 1
        raise AssertionError("postcheck must not write")
    def counted_digest(path: str) -> Any:
        calls["digest"] += 1
        return original_digest(path)
    runtime.backend.write, runtime.backend.digest = forbidden_write, counted_digest
    try:
        before = runtime.journal.get(config["original"]["command_id"])
        assert before == written["original"]
        polled = runtime.poll_once(lease_s=30)
        assert polled == {"ok": True, "commands": 1, "receipts": 1}, polled
        check = runtime.journal.get(config["check"]["command_id"])
        assert check is not None and check["state"] == expected[outcome]
        assert runtime.client.handle(config["original"])["status"] == "unknown"
        assert runtime.client.handle(config["check"])["status"] == expected[outcome]
        assert runtime.journal.get(config["original"]["command_id"]) == before
        assert calls == {"write": 0, "digest": 1}
        assert runtime.poll_once() == {"ok": True, "commands": 0, "receipts": 0}
    finally:
        runtime.close_all()
    restarted = _runtime(root, config)
    restart_calls = {"io": 0}
    def unexpected_io(*arguments: Any) -> Any:
        restart_calls["io"] += 1
        raise AssertionError("terminal journals must not repeat I/O")
    restarted.backend.write = restarted.backend.digest = unexpected_io
    try:
        assert restarted.client.handle(config["original"])["status"] == "unknown"
        assert restarted.client.handle(config["check"])["status"] == expected[outcome]
        assert restarted.journal.get(config["original"]["command_id"]) == before
        assert restarted.journal.get(config["check"]["command_id"]) == check
        assert restart_calls["io"] == 0
        return {"poll": polled, "original": before, "check": check, "postcheck_writes": calls["write"],
            "digest_calls": calls["digest"], "restart_io": restart_calls["io"], "outcome": outcome}
    finally:
        restarted.close_all()


def main() -> None:
    raw = sys.stdin.buffer.read(65537)
    assert len(raw) <= 65536
    config = json.loads(raw)
    assert type(config) is dict and config.get("operation") in {"prepare", "write", "postcheck", "replay"}
    assert type(config.get("case")) is str and _CASE.fullmatch(config["case"])
    base = Path(config["root"])
    assert base.is_absolute() and ".." not in base.parts
    operation = config["operation"]
    function = {"prepare": _prepare, "write": _write, "postcheck": _postcheck, "replay": _replay}[operation]
    report = function(base / config["case"], config) | {"operation": operation, "hostname": platform.node(),
        "platform": platform.system().lower(), "python": platform.python_version(), "pid": os.getpid()}
    encoded = json.dumps(report, sort_keys=True).encode("utf-8")
    assert len(encoded) <= 65536
    sys.stdout.buffer.write(encoded + b"\n")


if __name__ == "__main__":
    main()
