"""Opt-in Windows exporter against an original disposable Hub over SSH loopback."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from fixtures.workflow.windows_artifact_probe import private_directory
from tools.platform import artifact_windows as windows
from tools.platform.artifact_export import OutputFile
from tools.platform.node_runtime import NodeRuntime, NodeRuntimeConfig
from tools.transport import Transport


class LostResponseTransport(Transport):
    def __init__(self, stage: str) -> None:
        super().__init__(mode="direct", attempts=1)
        self.stage = stage
        self.dropped = False
        self.puts = 0
        self.receipt_posts = 0

    def request_bounded_json(self, method: str, url: str, *args: Any, **kwargs: Any) -> Any:
        result = super().request_bounded_json(method, url, *args, **kwargs)
        if method == "PUT":
            self.puts += 1
        if url.endswith("/receipts"):
            self.receipt_posts += 1
        if not self.dropped and self.stage == "part" and method == "PUT":
            self.dropped = True
            raise OSError("canary lost response after original Hub stored part")
        if not self.dropped and self.stage == "receipt" and url.endswith("/receipts"):
            self.dropped = True
            raise OSError("canary lost response after original Hub committed receipt")
        return result


def main() -> None:
    raw = sys.stdin.buffer.read(65537)
    assert len(raw) <= 65536
    config = json.loads(raw)
    assert config["stage"] in {"part", "receipt"}
    root = private_directory(Path(config["root"]) / config["case"])
    outputs, journals = private_directory(root / "outputs"), private_directory(root / "journals")
    data = config["payload"].encode("utf-8")
    with windows.root_handle(outputs) as directory:
        with os.fdopen(windows.open_child(directory, "report.bin", writable=True, create=True, exclusive=True), "wb") as source:
            source.write(data)
    transport = LostResponseTransport(config["stage"])
    params = NodeRuntimeConfig(node_id=config["node_id"], credential=config["credential"],
        worker_id="node-worker", hub_url=config["hub_url"], public_key=bytes.fromhex(config["public_key"]),
        workspace_root=root / "workspace", journal_path=journals / "commands.db",
        capabilities=frozenset({"artifact.export"}),
        artifact_outputs=((config["workspace"], "report", OutputFile(outputs, "report.bin")),))
    first_node = NodeRuntime(params, transport=transport, clock=lambda: config["now"])
    try:
        assert first_node.manifest()["capabilities"] == {"artifact.export": True}
        first = first_node.poll_once(lease_s=300)
        assert first == {"ok": True, "commands": 1, "receipts": 0}, first
        original = first_node.artifact_exporter.journal.get("export-a")
        assert original["state"] == ("reserved" if config["stage"] == "part" else "ready")
    finally:
        first_node.close_all()
    restarted = NodeRuntime(params, transport=transport, clock=lambda: config["now"])
    try:
        second = restarted.poll_once(lease_s=300)
        assert second == {"ok": True, "commands": 0, "receipts": 1}, second
        assert restarted.artifact_exporter.pending_receipts() == ()
        final = restarted.artifact_exporter.journal.get("export-a")
        assert final["state"] == "ready" and final["acknowledged"] == 1
        assert (final["transfer_id"], final["artifact_id"]) == (original["transfer_id"], original["artifact_id"])
        receipt = json.loads(final["receipt"])
        assert receipt["artifact"]["size"] == len(data)
        assert receipt["artifact"]["sha256"] == hashlib.sha256(data).hexdigest()
        assert transport.puts == 1 and transport.receipt_posts == (1 if config["stage"] == "part" else 2)
        print(json.dumps({"hostname": socket.gethostname(), "stage": config["stage"], "first": first,
            "second": second, "puts": transport.puts, "receipt_posts": transport.receipt_posts,
            "command_id": final["command_id"], "transfer_id": final["transfer_id"], "receipt": receipt,
            "sha256": receipt["artifact"]["sha256"], "source_identity": json.loads(final["source_identity"]),
            "state": final["state"]}))
    finally:
        restarted.close_all()


if __name__ == "__main__":
    main()
