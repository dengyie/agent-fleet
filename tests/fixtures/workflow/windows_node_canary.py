"""Explicit Windows Node canary for a disposable Hub reached over SSH loopback."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import socket
import sys
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from fixtures.workflow.windows_artifact_probe import private_directory
from tools.platform.artifact_transfer import InputDirectory
from tools.platform.node_runtime import NodeRuntime, NodeRuntimeConfig
from tools.transport import Transport


class LostReceiptTransport(Transport):
    def __init__(self) -> None:
        super().__init__(mode="direct", attempts=1)
        self.downloads = 0
        self.receipts = 0

    def post_json(self, url: str, *args: Any, **kwargs: Any) -> Any:
        result = super().post_json(url, *args, **kwargs)
        if url.endswith("/receipts"):
            self.receipts += 1
            if self.receipts == 1:
                raise OSError("canary lost response after authoritative Hub receipt commit")
        return result

    @contextmanager
    def get_stream(self, *args: Any, **kwargs: Any) -> Iterator[Any]:
        self.downloads += 1
        with super().get_stream(*args, **kwargs) as response:
            yield response


def main() -> None:
    raw = sys.stdin.buffer.read(65537)
    assert len(raw) <= 65536
    config = json.loads(raw)
    root = private_directory(Path(config["root"]) / config["case"])
    inputs, journals = private_directory(root / "inputs"), private_directory(root / "journals")
    transport = LostReceiptTransport()
    params = NodeRuntimeConfig(node_id=config["node_id"], credential=config["credential"],
        workspace_root=root / "workspace", journal_path=journals / "commands.db", hub_url=config["hub_url"],
        public_key=bytes.fromhex(config["public_key"]), worker_id="windows-receiver",
        capabilities=frozenset({"artifact.receive"}), artifact_inputs=(("source", InputDirectory(inputs, frozenset({".tar"}))),))
    node = NodeRuntime(params, transport=transport, clock=lambda: config["now"])
    try:
        assert node.manifest()["capabilities"] == {"artifact.receive": True}
        first = node.poll_once(lease_s=60)
        assert first["receipts"] == 0, first
        pending = node.artifact_receiver.pending_receipts()
        assert len(pending) == 1
        original = pending[0]
    finally:
        node.close_all()
    restarted = NodeRuntime(params, transport=transport, clock=lambda: config["now"])
    try:
        second = restarted.poll_once(lease_s=60)
        assert second["receipts"] == 1, second
        assert restarted.artifact_receiver.pending_receipts() == ()
        assert transport.downloads == 1 and transport.receipts == 2
        file = inputs / (original[1]["grant_id"] + ".tar")
        digest = hashlib.sha256()
        with file.open("rb") as source:
            for chunk in iter(lambda: source.read(65536), b""):
                digest.update(chunk)
        assert digest.hexdigest() == config["sha256"]
        assert file.stat().st_size == config["size"]
        print(json.dumps({"hostname": socket.gethostname(), "first": first, "second": second,
            "downloads": transport.downloads, "receipt_posts": transport.receipts,
            "command_id": original[0], "receipt": original[1], "sha256": digest.hexdigest(),
            "receive_state": restarted.artifact_receiver.journal.get(original[0])["state"]}))
    finally:
        restarted.close_all()


if __name__ == "__main__":
    main()
