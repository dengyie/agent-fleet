"""Opt-in actual Linux Node receive followed by separate fixed source validation."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from fixtures.workflow.windows_node_canary import LostReceiptTransport
from fleet_core.workflow.contract_types import ArtifactReference, CodeRevisionReference
from tools.platform.artifact_export import OutputFile, _output_handle
from tools.platform.artifact_journal import ArtifactReceiveJournal
from tools.platform.artifact_transfer import InputDirectory
from tools.platform.node_runtime import NodeRuntime, NodeRuntimeConfig
from tools.platform.workflow_source import extract_source_bundle, verify_source_stream
from tools.platform.workflow_validators import FixedTestRunner, test_report_bytes
from workflow_sandbox_runtime import linux_test_backend

_CASE = re.compile(r"handoff-[0-9a-f]{16}")
_LIMIT = 1024 * 1024


def _root(config: dict[str, Any]) -> Path:
    assert sys.platform == "linux" and os.name == "posix"
    assert type(config.get("case")) is str and _CASE.fullmatch(config["case"])
    root = Path(config["root"]) / config["case"]
    assert root.is_absolute() and ".." not in root.parts
    return root


def _private(path: Path) -> Path:
    path.mkdir(mode=0o700)
    return path


def _hash(output: OutputFile) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with _output_handle(output) as (source, identity):
        assert int(identity["size"]) <= _LIMIT
        for chunk in iter(lambda: source.read(65536), b""):
            size += len(chunk)
            assert size <= _LIMIT
            digest.update(chunk)
    return digest.hexdigest(), size


def _receive(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    _private(root)
    inputs, journals = _private(root / "inputs"), _private(root / "journals")
    transport = LostReceiptTransport()
    params = NodeRuntimeConfig(node_id=config["node_id"], credential=config["credential"],
        workspace_root=root / "workspace", journal_path=journals / "commands.db", hub_url=config["hub_url"],
        public_key=bytes.fromhex(config["public_key"]), worker_id="receiver",
        capabilities=frozenset({"artifact.receive"}),
        artifact_inputs=(("source", InputDirectory(inputs, frozenset({".tar"}))),))
    first = NodeRuntime(params, transport=transport, clock=lambda: config["now"])
    try:
        assert first.manifest()["capabilities"] == {"artifact.receive": True}
        before = first.poll_once(lease_s=300)
        assert before == {"ok": True, "commands": 1, "receipts": 0}, before
        pending = first.artifact_receiver.pending_receipts()
        assert len(pending) == 1 and pending[0][0] == config["command_id"]
        original = pending[0][1]
    finally:
        first.close_all()
    restarted = NodeRuntime(params, transport=transport, clock=lambda: config["now"])
    try:
        after = restarted.poll_once(lease_s=300)
        assert after == {"ok": True, "commands": 0, "receipts": 1}, after
        assert restarted.artifact_receiver.pending_receipts() == ()
        record = restarted.artifact_receiver.journal.get(config["command_id"])
        assert record["state"] == "ready" and record["acknowledged"] == 1
        assert json.loads(record["receipt"]) == original
        assert transport.downloads == 1 and transport.receipts == 2
        output = OutputFile(inputs, original["grant_id"] + ".tar")
        digest, size = _hash(output)
        assert (digest, size) == (config["sha256"], config["size"])
        assert not (root / "validation").exists()
        report = {"hostname": platform.node(), "platform": platform.platform(), "python": platform.python_version(),
            "operation": "receive", "before": before, "after": after, "downloads": transport.downloads,
            "receipt_posts": transport.receipts, "command_id": config["command_id"], "receipt": original,
            "sha256": digest, "size": size, "state": record["state"], "validation_started": False}
        (root / "received.json").write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")
        return report
    finally:
        restarted.close_all()


@dataclass(frozen=True)
class _ReceivedSource:
    output: OutputFile
    owner: str
    workspace: str
    reference: ArtifactReference

    def read(self, owner: str, workspace: str, artifact_id: str) -> bytes:
        assert (owner, workspace, artifact_id) == (self.owner, self.workspace, self.reference.artifact_id)
        assert 0 < self.reference.size <= _LIMIT
        with _output_handle(self.output) as (source, identity):
            assert int(identity["size"]) == self.reference.size
            raw = source.read(self.reference.size + 1)
        assert len(raw) == self.reference.size and hashlib.sha256(raw).hexdigest() == self.reference.sha256
        return raw


def _validate(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    assert "credential" not in config and config["validation_authorized"] is True
    received = json.loads((root / "received.json").read_text(encoding="utf-8"))
    assert received["hostname"] == platform.node() and received["validation_started"] is False
    reference = ArtifactReference(**config["reference"])
    revision = CodeRevisionReference(**config["source_revision"], source_bundle=reference)
    receipt = received["receipt"]
    assert (receipt["artifact_id"], receipt["sha256"], receipt["size"]) == (reference.artifact_id, reference.sha256, reference.size)
    journal = ArtifactReceiveJournal(root / "journals" / "commands.receives.db")
    record = journal.get(received["command_id"])
    assert record["state"] == "ready" and record["acknowledged"] == 1 and json.loads(record["receipt"]) == receipt
    output = OutputFile(root / "inputs", receipt["grant_id"] + ".tar")
    assert _hash(output) == (reference.sha256, reference.size)
    with _output_handle(output) as (source, _identity):
        checked = verify_source_stream(source, expected=revision)
    assert checked.included_paths == ("src.txt", "test_payload.py") and checked.omitted_paths == (".env",)
    store = _ReceivedSource(output, config["owner"], config["workspace"], reference)
    validation = _private(root / "validation")
    checkout = validation / "source"
    paths = extract_source_bundle(store, config["owner"], config["workspace"], reference.artifact_id,
        checkout, expected_revision=revision)
    assert paths == ("src.txt", "test_payload.py")
    assert (checkout / "src.txt").read_bytes() == b"fixed source\n"
    assert not (checkout / ".env").exists() and not (checkout / "untracked.txt").exists()
    result = asyncio.run(FixedTestRunner(backend_factory=linux_test_backend).run(revision, reference.sha256,
        checkout, recipe_id="coding-delivery-tests", executor_id="linux-handoff-validator"))
    report_bytes = test_report_bytes(result, revision)
    assert result.passed and result.exit_code == 0 and not result.timed_out and result.junit is not None
    assert _hash(output) == (reference.sha256, reference.size)
    (validation / "test-report.json").write_bytes(report_bytes)
    (validation / "junit.xml").write_bytes(result.junit)
    return {"hostname": platform.node(), "operation": "validate", "command_id": received["command_id"],
        "grant_id": receipt["grant_id"], "sha256": reference.sha256, "size": reference.size,
        "included_paths": list(paths), "omitted_paths": list(checked.omitted_paths),
        "report": json.loads(report_bytes), "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "junit": result.junit.decode("utf-8"), "junit_sha256": hashlib.sha256(result.junit).hexdigest()}


def main() -> None:
    raw = sys.stdin.buffer.read(65537)
    assert len(raw) <= 65536
    config = json.loads(raw)
    assert type(config) is dict and config.get("operation") in {"receive", "validate"}
    root = _root(config)
    report = _receive(root, config) if config["operation"] == "receive" else _validate(root, config)
    encoded = json.dumps(report, sort_keys=True).encode("utf-8")
    assert len(encoded) <= 65536
    sys.stdout.buffer.write(encoded + b"\n")


if __name__ == "__main__":
    main()
