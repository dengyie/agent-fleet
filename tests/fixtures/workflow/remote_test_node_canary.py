"""Opt-in independent Linux Node; credentials exist only in bounded stdin."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import time
from typing import Any, Callable, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from fleet_core.capability import CapabilityDefinition
from fleet_core.remote_workflow_test import REMOTE_TEST_ACTION
from fleet_core.workflow.contract_types import CatalogRef
from tools.platform.artifact_transfer import InputDirectory
from tools.platform.node_capabilities import LocalCapability, NodeCapabilityConfig
from tools.platform.node_runtime import NodeRuntime, NodeRuntimeConfig
from tools.transport import Transport


def pilot_definition() -> CapabilityDefinition:
    return CapabilityDefinition("coding.test", 1, "fixed-tests", "in-v1", "out-v1",
        b'{"type":"object"}', b'{"type":"object"}', "workspace_read", "manual_only",
        ("operator",), 3600, 65536)


class PilotTransport(Transport):
    def __init__(self, *, drop_receipt: bool) -> None:
        super().__init__(mode="direct", attempts=1)
        self.drop_receipt = drop_receipt
        self.downloads = 0
        self.dropped = 0

    def post_json(self, url: str, body: dict[str, Any], headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        if url.endswith("/receipts") and self.drop_receipt:
            self.dropped += 1
            raise OSError("native pilot drops test receipt before HTTP submission")
        return self.request_bounded_json("POST", url, json.dumps(body, allow_nan=False).encode("utf-8"),
            headers, deadline=time.monotonic() + 15, should_cancel=lambda: False)

    @contextmanager
    def get_stream(self, *args: Any, **kwargs: Any) -> Iterator[Any]:
        self.downloads += 1
        with super().get_stream(*args, **kwargs) as response:
            yield response


def _runtime(root: Path, config: dict[str, Any], transport: PilotTransport) -> NodeRuntime:
    definition = pilot_definition()
    registration = NodeCapabilityConfig(1, (LocalCapability(definition, (REMOTE_TEST_ACTION,)),),
        (CatalogRef(definition.capability_id, 1),))
    launcher = (sys.executable, "-I", str(Path(__file__).with_name("remote_test_launcher.py").resolve(strict=True)))
    return NodeRuntime(NodeRuntimeConfig(node_id="node-a", credential=config["credential"],
        workspace_root=root / "workspace", journal_path=root / "node.db", hub_url=config["hub_url"],
        public_key=bytes.fromhex(config["public_key"]), worker_id="native-test-node",
        capabilities=frozenset({"artifact.receive", REMOTE_TEST_ACTION}),
        artifact_inputs=(("source", InputDirectory(root / "inputs", frozenset({".tar"}))),),
        sandbox_launcher=launcher, capability_config=registration, workflow_test_enabled=True),
        transport=transport, clock=lambda: config["now"])


def operate(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    operation = config["operation"]
    if operation == "prepare":
        root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        root.mkdir(mode=0o700)
        for name in ("workspace", "inputs"):
            (root / name).mkdir(mode=0o700)
        (root / "host-secret").write_text("native-host-only-secret", encoding="utf-8")
    transport = PilotTransport(drop_receipt=operation == "lost")
    runtime = _runtime(root, config, transport)
    calls = {"executions": 0, "preflights": 0}
    original_execute = runtime.workflow_test_executor.execute
    original_prepare = runtime.executor.capability_prepare

    def execute(command: dict[str, Any], *, should_cancel: Callable[[], bool] | None = None) -> dict[str, Any]:
        calls["executions"] += 1
        if operation in {"replay", "recover"}:
            raise AssertionError("terminal journal must not rerun fixed tests")
        result = original_execute(command, should_cancel=should_cancel)
        if operation == "unknown":
            raise OSError("native pilot interrupts after the fixed test finishes")
        return result

    def prepare(command: dict[str, Any]) -> dict[str, Any]:
        calls["preflights"] += 1
        return original_prepare(command)

    runtime.workflow_test_executor.execute = execute
    runtime.executor.capability_prepare = prepare
    try:
        if operation == "prepare":
            receipt = runtime.report_capabilities()
            offer = runtime.capability_service.offer(now=config["now"])
            assert receipt.offer_revision == offer.offer_revision and offer.os == "linux"
            assert offer.capabilities == (CatalogRef("coding.test", 1),)
            return {"offer": offer.payload(), "offer_digest": offer.config_digest}
        if operation in {"recover", "replay"}:
            command = config["command"]
            saved = runtime.journal.get(command["command_id"])
            assert saved is not None and saved["state"] in {"succeeded", "unknown"}
            for _ in range(2):
                receipt = runtime.client.handle(command)
                assert receipt["status"] == saved["state"] and receipt["result"] == saved["result"]
            assert saved["receipt_pending"] is (operation == "recover")
            poll = runtime.poll_once(lease_s=60)
            assert poll == {"ok": True, "commands": 0, "receipts": 1 if operation == "recover" else 0}, poll
            acknowledged = runtime.journal.get(command["command_id"])
            assert acknowledged == {**saved, "receipt_pending": False}
            saved = acknowledged
        else:
            poll = runtime.poll_once(lease_s=60)
            assert poll == {"ok": True, "commands": 1, "receipts": 0 if operation == "lost" else 1}, poll
            saved = runtime.journal.get(config["command_id"]) if operation != "receive" else None
        assert calls["executions"] == (1 if operation in {"execute", "lost", "unknown"} else 0)
        assert calls["preflights"] == calls["executions"]
        assert transport.downloads == (1 if operation == "receive" else 0)
        received = tuple((root / "inputs").glob("*.tar"))
        assert len(received) == 1
        digest = hashlib.sha256()
        with received[0].open("rb") as handle:
            for block in iter(lambda: handle.read(65536), b""):
                digest.update(block)
        return {"poll": poll, "journal": saved, **calls, "downloads": transport.downloads,
            "dropped_receipts": transport.dropped, "source_sha256": digest.hexdigest(),
            "host_secret_preserved": (root / "host-secret").read_text(encoding="utf-8") == "native-host-only-secret"}
    finally:
        runtime.close_all()


def main() -> None:
    assert sys.platform == "linux"
    raw = sys.stdin.buffer.read(65537)
    assert len(raw) <= 65536
    config = json.loads(raw)
    assert type(config) is dict and config.get("operation") in {"facts", "prepare", "receive", "execute", "lost", "unknown", "recover", "replay"}
    root = Path(config["root"])
    assert root.is_absolute() and ".." not in root.parts
    result = ({"namespaces": {name: os.stat("/proc/self/ns/" + name).st_ino
        for name in ("user", "mnt", "pid", "net")}} if config["operation"] == "facts" else operate(root, config))
    report = result | {"operation": config["operation"], "pid": os.getpid(),
        "hostname": platform.node(), "platform": platform.system().lower(), "python": platform.python_version()}
    encoded = json.dumps(report, allow_nan=False, sort_keys=True).encode("utf-8")
    assert len(encoded) <= 65536 and config["credential"].encode() not in encoded
    sys.stdout.buffer.write(encoded + b"\n")


if __name__ == "__main__":
    main()
