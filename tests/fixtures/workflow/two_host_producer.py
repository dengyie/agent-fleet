"""Opt-in actual Windows source preparation and original signed Node export."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from fixtures.workflow.windows_artifact_probe import private_directory
from fixtures.workflow.windows_export_canary import LostResponseTransport
from tools.platform import artifact_windows as windows
from tools.platform.artifact_export import OutputFile
from tools.platform.node_runtime import NodeRuntime, NodeRuntimeConfig
from tools.platform.workflow_source import SourceExporter

_CASE = re.compile(r"handoff-[0-9a-f]{16}")
_LIMIT = 1024 * 1024
_TESTS = (
    "from pathlib import Path\n"
    "import os\n"
    "def test_fixed_committed_bytes():\n"
    "    assert Path('src.txt').read_bytes() == b'fixed source\\n'\n"
    "def test_private_input_boundary():\n"
    "    assert not Path('.env').exists() and not Path('untracked.txt').exists()\n"
    "    assert not any('CREDENTIAL' in key or 'TOKEN' in key for key in os.environ)\n"
)


def _git(repository: Path, *arguments: str) -> str:
    environment = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_DATE": "2001-01-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2001-01-01T00:00:00+00:00"}
    result = subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgsign=false",
        *arguments], cwd=repository, env=environment, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    return result.stdout.decode("utf-8").strip()


def _root(config: dict[str, Any]) -> Path:
    assert os.name == "nt" and windows.windows_supported()
    assert type(config.get("case")) is str and _CASE.fullmatch(config["case"])
    root = Path(config["root"]) / config["case"]
    windows.validate_root(str(root))
    return root


def _prepare(root: Path) -> dict[str, Any]:
    private_directory(root)
    repository, outputs, control = [private_directory(root / name) for name in ("repository", "outputs", "control")]
    private_directory(root / "journals")
    _git(repository, "init", "-q")
    _git(repository, "config", "core.autocrlf", "false")
    _git(repository, "config", "user.name", "Fixed handoff fixture")
    _git(repository, "config", "user.email", "handoff@example.invalid")
    (repository / "src.txt").write_bytes(b"fixed source\n")
    (repository / ".env").write_bytes(b"TOKEN=synthetic-do-not-export\n")
    _git(repository, "add", "src.txt", ".env")
    _git(repository, "commit", "-qm", "fixed source")
    base = _git(repository, "rev-parse", "HEAD")
    (repository / "test_payload.py").write_bytes(_TESTS.encode("utf-8"))
    _git(repository, "add", "test_payload.py")
    _git(repository, "commit", "-qm", "fixed validation recipe")
    head, tree = _git(repository, "rev-parse", "HEAD"), _git(repository, "rev-parse", "HEAD^{tree}")
    (repository / "src.txt").write_bytes(b"uncommitted work must not ship\n")
    (repository / "untracked.txt").write_bytes(b"untracked work must not ship\n")
    archive = control / "source.tar"
    exported = asyncio.run(SourceExporter(repository).export_archive(repository_id="repo-a",
        base_commit=base, head_commit=head, tree_id=tree, destination=archive))
    assert exported.included_paths == ("src.txt", "test_payload.py") and exported.omitted_paths == (".env",)
    assert 0 < archive.stat().st_size <= _LIMIT
    digest = hashlib.sha256()
    size = 0
    with windows.root_handle(outputs) as directory:
        descriptor = windows.open_child(directory, "source.tar", writable=True, create=True, exclusive=True)
        with os.fdopen(descriptor, "wb") as destination, archive.open("rb") as source:
            for chunk in iter(lambda: source.read(65536), b""):
                size += len(chunk)
                assert size <= _LIMIT
                digest.update(chunk)
                destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())
    report = {"hostname": platform.node(), "platform": platform.platform(), "python": platform.python_version(),
        "operation": "prepare", "repository_id": "repo-a", "base_commit": base, "head_commit": head,
        "tree_id": tree, "sha256": digest.hexdigest(), "size": size,
        "included_paths": list(exported.included_paths), "omitted_paths": list(exported.omitted_paths)}
    (control / "prepared.json").write_bytes((json.dumps(report, sort_keys=True) + "\n").encode("utf-8"))
    return report


def _export(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    prepared = json.loads((root / "control" / "prepared.json").read_bytes())
    assert prepared["hostname"] == platform.node()
    transport = LostResponseTransport("part")
    params = NodeRuntimeConfig(node_id=config["node_id"], credential=config["credential"],
        worker_id="node-worker", hub_url=config["hub_url"], public_key=bytes.fromhex(config["public_key"]),
        workspace_root=root / "workspace", journal_path=root / "journals" / "commands.db",
        capabilities=frozenset({"artifact.export"}),
        artifact_outputs=((config["workspace"], "report", OutputFile(root / "outputs", "source.tar")),))
    first = NodeRuntime(params, transport=transport, clock=lambda: config["now"])
    try:
        assert first.manifest()["capabilities"] == {"artifact.export": True}
        before = first.poll_once(lease_s=300)
        assert before == {"ok": True, "commands": 1, "receipts": 0}, before
        original = first.artifact_exporter.journal.get("export-a")
        assert original["state"] == "reserved"
    finally:
        first.close_all()
    restarted = NodeRuntime(params, transport=transport, clock=lambda: config["now"])
    try:
        after = restarted.poll_once(lease_s=300)
        assert after == {"ok": True, "commands": 0, "receipts": 1}, after
        final = restarted.artifact_exporter.journal.get("export-a")
        assert final["state"] == "ready" and final["acknowledged"] == 1
        assert (final["transfer_id"], final["artifact_id"]) == (original["transfer_id"], original["artifact_id"])
        assert (final["sha256"], final["size"]) == (prepared["sha256"], prepared["size"])
        assert transport.puts == 1 and transport.receipt_posts == 1
        assert restarted.artifact_exporter.pending_receipts() == ()
        return {"hostname": platform.node(), "operation": "export", "before": before, "after": after,
            "command_id": final["command_id"], "transfer_id": final["transfer_id"],
            "receipt": json.loads(final["receipt"]), "puts": transport.puts,
            "receipt_posts": transport.receipt_posts, "source_identity": json.loads(final["source_identity"])}
    finally:
        restarted.close_all()


def main() -> None:
    raw = sys.stdin.buffer.read(65537)
    assert len(raw) <= 65536
    config = json.loads(raw)
    assert type(config) is dict and config.get("operation") in {"prepare", "export"}
    root = _root(config)
    report = _prepare(root) if config["operation"] == "prepare" else _export(root, config)
    encoded = json.dumps(report, sort_keys=True).encode("utf-8")
    assert len(encoded) <= 65536
    sys.stdout.buffer.write(encoded + b"\n")


if __name__ == "__main__":
    main()
