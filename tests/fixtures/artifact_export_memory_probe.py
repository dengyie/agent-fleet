"""Measure the real export chain in a process owned by one memory probe."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tracemalloc
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]


def main(root: Path) -> None:
    from test_artifact_export import export_node
    from test_artifact_transfer import transfer, transfer_http
    from test_workflow_dispatch import setup
    from test_workflow_repository import OWNER

    root.mkdir(mode=0o700)
    workflow = setup.__wrapped__(root)
    fixture = transfer.__wrapped__(root, workflow)
    node = export_node.__wrapped__(fixture, transfer_http.__wrapped__(fixture, root), root)

    class ServiceTransport:
        def request_bounded_json(self, method, url, body, headers, **kwargs):
            # Exercise the original SQLite/filesystem pipeline without Flask
            # test-client request history retaining each binary input body.
            scope = {"owner_id": OWNER, "node_id": "node-a", "worker_id": "node-worker"}
            path = urlsplit(url).path.split("/artifact-transfers", 1)[1]
            if method == "PUT":
                transfer_id, _, offset = path.strip("/").split("/")
                return 200, {"ok": True, "part": fixture.service.put_part(transfer_id,
                    **scope, offset=int(offset), payload=body, content_length=len(body),
                    declared_hash=headers["X-Content-SHA256"])}
            if method == "GET":
                status = fixture.service.status(path[1:], **scope, command_id="export-a")
                status["expires_at"] = datetime.fromtimestamp(status["expires_at"], timezone.utc).isoformat()
                return 200, {"ok": True, "transfer": status}
            data = json.loads(body)
            if path.endswith("/commit"):
                return 200, {"ok": True, "artifact": fixture.service.commit(path.split("/")[1], **scope, **data)}
            manifest = fixture.service.reserve(**scope, **data)
            return 201, {"ok": True, "transfer": {"transfer_id": manifest.transfer_id,
                "artifact_id": manifest.artifact_id, "chunk_size": manifest.chunk_size,
                "expires_at": datetime.fromtimestamp(manifest.expires_at, timezone.utc).isoformat()}}

    with node.source.open("wb") as output:
        for _ in range(12):
            output.write(b"x" * (1024 * 1024))
    tracemalloc.stop()
    tracemalloc.start()
    try:
        receipt = node.exporter(transport=ServiceTransport()).export(node.command)
        current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    result = {"pid": os.getpid(), "size": receipt["artifact"]["size"],
              "current_bytes": current, "peak_bytes": peak, "limit_bytes": 8 * 1024 * 1024}
    (root / "memory.json").write_text(json.dumps(result) + "\n", encoding="utf-8")
    assert result["size"] == 12 * 1024 * 1024
    assert peak < result["limit_bytes"], result


if __name__ == "__main__":
    main(Path(sys.argv[1]))
