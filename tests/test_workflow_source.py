"""Source exporter and safe bundle extraction evidence."""
from __future__ import annotations

import hashlib
import io
import json
import sys
import threading
from dataclasses import replace
import asyncio
import subprocess
import tarfile
from pathlib import Path

import pytest

from hub.platform.artifacts import ArtifactStore
from hub.platform.workflow_source import SourceExportError, SourceExporter, extract_source_bundle


OWNER = "owner@example.test"
WORKSPACE = "workspace-a"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def repo_with_commit(tmp_path: Path) -> tuple[Path, str, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Workflow Test")
    git(repo, "config", "user.email", "workflow@example.test")
    (repo / "src.txt").write_text("fixed source\n", encoding="utf-8")
    (repo / ".env").write_text("TOKEN=do-not-export\n", encoding="utf-8")
    git(repo, "add", "src.txt", ".env")
    git(repo, "commit", "-qm", "source")
    head = git(repo, "rev-parse", "HEAD")
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    return repo, head, head, tree


def test_export_uses_committed_tree_and_excludes_credentials(tmp_path: Path) -> None:
    repo, base, head, tree = repo_with_commit(tmp_path)
    store = ArtifactStore(tmp_path / "artifacts")
    (repo / 'src.txt').write_text('uncommitted user work', encoding='utf-8')
    (repo / 'untracked.txt').write_text('untracked user work', encoding='utf-8')
    result = asyncio.run(SourceExporter(repo, store).export(OWNER, WORKSPACE, repository_id="repo-a", base_commit=base, head_commit=head, tree_id=tree))
    assert result.included_paths == ("src.txt",)
    assert result.omitted_paths == (".env",)
    assert result.revision.source_bundle.sha256 == result.manifest["sha256"]
    destination = tmp_path / "checkout"
    assert extract_source_bundle(store, OWNER, WORKSPACE, result.manifest["artifact_id"], destination,
                                 expected_revision=result.revision) == ("src.txt",)
    assert (destination / "src.txt").read_text(encoding="utf-8") == "fixed source\n"
    assert not (destination / ".env").exists()


def test_export_excludes_named_credential_stores_from_committed_tree(tmp_path: Path) -> None:
    repo, base, _, _ = repo_with_commit(tmp_path)
    forbidden = (".git-credentials", ".aws/credentials", ".ssh/config", "credentials.json", "secrets.yaml", "id_ecdsa", "id_dsa")
    for name in forbidden:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("must never enter the delivery bundle", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "credential fixtures")
    head, tree = git(repo, "rev-parse", "HEAD"), git(repo, "rev-parse", "HEAD^{tree}")
    store = ArtifactStore(tmp_path / "artifacts")
    result = asyncio.run(SourceExporter(repo, store).export(OWNER, WORKSPACE, repository_id="repo-a", base_commit=base, head_commit=head, tree_id=tree))
    assert result.included_paths == ("src.txt",)
    assert set(result.omitted_paths) == {".env", *forbidden}
    extract_source_bundle(store, OWNER, WORKSPACE, result.manifest["artifact_id"], tmp_path / "out", expected_revision=result.revision)
    assert tuple(item.name for item in (tmp_path / "out").iterdir()) == ("src.txt",)


@pytest.mark.parametrize("field", ["base_commit", "head_commit", "tree_id"])
def test_export_rejects_non_object_ids(tmp_path: Path, field: str) -> None:
    repo, base, head, tree = repo_with_commit(tmp_path)
    values = {"base_commit": base, "head_commit": head, "tree_id": tree}
    values[field] = "main"
    with pytest.raises(SourceExportError, match="invalid_"):
        asyncio.run(SourceExporter(repo, ArtifactStore(tmp_path / "artifacts")).export(OWNER, WORKSPACE, repository_id="repo-a", **values))


def test_extraction_rejects_path_escape_and_symlink(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    archive = tmp_path / "malicious.tar"
    with tarfile.open(archive, "w") as output:
        info = tarfile.TarInfo("../escape.txt")
        info.size = 1
        output.addfile(info, io.BytesIO(b"x"))
    manifest = store.put_file(OWNER, WORKSPACE, archive, name="source.tar", content_type="application/x-tar")
    with pytest.raises(SourceExportError, match="archive_path_escape"):
        extract_source_bundle(store, OWNER, WORKSPACE, manifest["artifact_id"], tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()


def test_extraction_checks_hash_before_writing(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    archive = tmp_path / "source.tar"
    with tarfile.open(archive, "w") as output:
        info = tarfile.TarInfo("src.txt")
        info.size = 1
        output.addfile(info, io.BytesIO(b"x"))
    manifest = store.put_file(OWNER, WORKSPACE, archive, name="source.tar", content_type="application/x-tar")
    with pytest.raises(SourceExportError, match="artifact_hash_mismatch"):
        extract_source_bundle(store, OWNER, WORKSPACE, manifest["artifact_id"], tmp_path / "out", expected_sha256="0" * 64)
    assert not (tmp_path / "out").exists()


def changed_archive(tmp_path: Path, transform):
    repo, base, head, tree = repo_with_commit(tmp_path)
    store = ArtifactStore(tmp_path / "artifacts")
    result = asyncio.run(SourceExporter(repo, store).export(OWNER, WORKSPACE, repository_id="repo-a", base_commit=base, head_commit=head, tree_id=tree))
    raw = store.read(OWNER, WORKSPACE, result.manifest["artifact_id"])
    path = tmp_path / "changed.tar"
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as source, tarfile.open(path, "w") as output:
        members = [(member, source.extractfile(member).read()) for member in source]
        for member, contents in transform(members):
            output.addfile(member, io.BytesIO(contents) if member.isfile() else None)
    manifest = store.put_file(OWNER, WORKSPACE, path, name="changed.tar", content_type="application/x-tar")
    from hub.domain.workflow_contract_types import ArtifactReference
    reference = replace(result.revision, source_bundle=ArtifactReference(manifest["artifact_id"], manifest["sha256"], manifest["size"]))
    return store, reference


@pytest.mark.parametrize("mutation", ["blob", "tree", "duplicate", "symlink", "hardlink", "fifo", "device", "missing"])
@pytest.mark.parametrize("consumer", ["bundle", "stream", "verify"])
def test_extractor_rejects_tampered_tree_and_members_before_publication(
        tmp_path: Path, mutation: str, consumer: str) -> None:
    from fleet_core.workflow.source import extract_source_stream, verify_source_stream

    def transform(members):
        if mutation == "blob":
            info, raw = members[-1]
            members[-1] = (info, b"X" * len(raw))
        elif mutation == "tree":
            info, raw = members[0]
            value = json.loads(raw)
            value["entries"][-1]["oid"] = "0" * 40
            raw = json.dumps(value).encode()
            info.size = len(raw)
            members[0] = (info, raw)
        elif mutation == "duplicate":
            members.append(members[-1])
        elif mutation in {"symlink", "hardlink", "fifo", "device"}:
            info, _ = members[-1]
            info.type, info.size = {
                "symlink": (tarfile.SYMTYPE, 0),
                "hardlink": (tarfile.LNKTYPE, 0),
                "fifo": (tarfile.FIFOTYPE, 0),
                "device": (tarfile.CHRTYPE, 0),
            }[mutation]
            if mutation == "symlink":
                info.linkname = "/etc/passwd"
            elif mutation == "hardlink":
                info.linkname = "src.txt"
            elif mutation == "device":
                info.devmajor, info.devminor = 1, 3
            members[-1] = (info, b"")
        else:
            members.pop()
        return members
    store, reference = changed_archive(tmp_path, transform)
    with pytest.raises(SourceExportError):
        if consumer == "bundle":
            extract_source_bundle(store, OWNER, WORKSPACE, reference.source_bundle.artifact_id, tmp_path / "output", expected_revision=reference)
        else:
            with store.open_verified(OWNER, WORKSPACE, reference.source_bundle.artifact_id) as handle:
                if consumer == "stream":
                    extract_source_stream(handle, tmp_path / "output", expected=reference, should_cancel=lambda: False)
                else:
                    verify_source_stream(handle, expected=reference)
    assert not (tmp_path / "output").exists()


def test_expected_bundle_size_is_part_of_the_reference(tmp_path: Path) -> None:
    store, reference = changed_archive(tmp_path, lambda members: members)
    reference = replace(reference, source_bundle=replace(reference.source_bundle, size=1))
    with pytest.raises(SourceExportError, match="artifact_hash_mismatch"):
        extract_source_bundle(store, OWNER, WORKSPACE, reference.source_bundle.artifact_id, tmp_path / "output", expected_revision=reference)


@pytest.mark.parametrize("case", ["file_count", "expanded_bytes"])
def test_stream_source_limits_reject_before_writing_members(tmp_path: Path, case: str) -> None:
    from fleet_core.workflow.source import MAX_BYTES, MAX_FILES, _tree_hash, extract_source_stream, verify_source_stream
    from hub.domain.workflow_contract_types import ArtifactReference, CodeRevisionReference

    if case == "file_count":
        entries = [{"path": f"file-{index}.txt", "mode": "100644", "oid": "0" * 40,
            "size": 0, "included": True} for index in range(MAX_FILES + 1)]
        tree_id = "0" * 40
        expected_error = "source_file_limit"
    else:
        entries = [{"path": "large.txt", "mode": "100644", "oid": "0" * 40,
            "size": MAX_BYTES + 1, "included": True}]
        tree_id = _tree_hash(entries, 40)
        expected_error = "source_expansion_too_large"
    manifest = json.dumps({"schema_version": 1, "repository_id": "repo-a", "base_commit": "1" * 40,
        "head_commit": "2" * 40, "tree_id": tree_id, "entries": entries},
        separators=(",", ":"), sort_keys=True).encode("utf-8")
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        info = tarfile.TarInfo(".fleet-source.json")
        info.size = len(manifest)
        archive.addfile(info, io.BytesIO(manifest))
    package = raw.getvalue()
    revision = CodeRevisionReference("repo-a", "1" * 40, "2" * 40, tree_id,
        ArtifactReference("source-package", hashlib.sha256(package).hexdigest(), len(package)))
    with pytest.raises(SourceExportError, match=expected_error):
        verify_source_stream(io.BytesIO(package), expected=revision)
    destination = tmp_path / f"{case}-destination"
    with pytest.raises(SourceExportError, match=expected_error):
        extract_source_stream(io.BytesIO(package), destination, expected=revision, should_cancel=lambda: False)
    assert not destination.exists()


def test_extraction_cannot_replace_a_directory_created_after_the_last_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, reference = changed_archive(tmp_path, lambda members: members)
    destination = tmp_path / "output"
    original_exists = Path.exists
    checks, original_inode = 0, None

    def race(path: Path) -> bool:
        nonlocal checks, original_inode
        if path == destination:
            checks += 1
            if checks == 2:
                path.mkdir()
                original_inode = path.stat().st_ino
                return False
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", race)
    with pytest.raises(SourceExportError, match="destination_exists"):
        extract_source_bundle(store, OWNER, WORKSPACE, reference.source_bundle.artifact_id, destination, expected_revision=reference)
    assert destination.stat().st_ino == original_inode
    assert tuple(destination.iterdir()) == ()


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_multifile_and_executable_tree_is_reconstructed(tmp_path: Path, object_format: str) -> None:
    repo = tmp_path / "repository"
    repo.mkdir()
    git(repo, "init", "-q", "--object-format=" + object_format)
    git(repo, "config", "user.name", "Workflow Test")
    git(repo, "config", "user.email", "workflow@example.test")
    git(repo, "config", "core.autocrlf", "false")
    (repo / "dir").mkdir()
    (repo / "dir" / "a.txt").write_text("a", encoding="utf-8")
    script = repo / "run.sh"
    script_bytes = b"#!/bin/sh\nexit 0\n"
    script.write_bytes(script_bytes)
    script.chmod(0o755)
    git(repo, "add", ".")
    # Git's executable bit is independent of the host filesystem on Windows.
    git(repo, "update-index", "--chmod=+x", "run.sh")
    git(repo, "commit", "-qm", "source")
    head, tree = git(repo, "rev-parse", "HEAD"), git(repo, "rev-parse", "HEAD^{tree}")
    store = ArtifactStore(tmp_path / "artifacts")
    result = asyncio.run(SourceExporter(repo, store).export(OWNER, WORKSPACE, repository_id="repo-a", base_commit=head, head_commit=head, tree_id=tree))
    assert git(repo, "ls-tree", "HEAD", "run.sh").startswith("100755 blob ")
    with tarfile.open(fileobj=io.BytesIO(store.read(OWNER, WORKSPACE, result.manifest["artifact_id"]))) as archive:
        assert archive.getmember("run.sh").mode == 0o755
        metadata = json.load(archive.extractfile(".fleet-source.json"))
        assert next(entry for entry in metadata["entries"] if entry["path"] == "run.sh")["mode"] == "100755"
    extract_source_bundle(store, OWNER, WORKSPACE, result.manifest["artifact_id"], tmp_path / "out", expected_revision=result.revision)
    assert (tmp_path / "out" / "dir" / "a.txt").read_text(encoding="utf-8") == "a"
    assert (tmp_path / "out" / "run.sh").read_bytes() == script_bytes
    if sys.platform != "win32":
        assert (tmp_path / "out" / "run.sh").stat().st_mode & 0o111 == 0o111


def test_cancelled_git_operation_reaps_its_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = asyncio.create_subprocess_exec
    children = []
    async def scenario() -> None:
        spawned = asyncio.Event()
        async def launch(*args, **kwargs):
            child = await original(sys.executable, "-c", "import time; time.sleep(30)", **kwargs)
            children.append(child)
            spawned.set()
            return child
        monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
        exporter = SourceExporter(tmp_path, ArtifactStore(tmp_path / "artifacts"))
        task = asyncio.create_task(exporter._git("status"))
        await spawned.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert children[0].returncode is not None
    asyncio.run(scenario())


def test_cancelled_export_awaits_artifact_publication_before_removing_source(tmp_path: Path) -> None:
    repo, base, head, tree = repo_with_commit(tmp_path)
    started, release = threading.Event(), threading.Event()
    published = []
    class SlowStore(ArtifactStore):
        def put_file(self, *args, **kwargs):
            started.set()
            assert release.wait(5)
            manifest = super().put_file(*args, **kwargs)
            published.append(manifest)
            return manifest
    async def scenario() -> None:
        task = asyncio.create_task(SourceExporter(repo, SlowStore(tmp_path / "artifacts")).export(
            OWNER, WORKSPACE, repository_id="repo-a", base_commit=base, head_commit=head, tree_id=tree))
        try:
            for _ in range(500):
                if started.is_set():
                    break
                await asyncio.sleep(.01)
            assert started.is_set()
            task.cancel()
            await asyncio.sleep(.03)
            assert not task.done(), "publication still owns the temporary source file"
            task.cancel()
            await asyncio.sleep(.03)
            assert not task.done(), "repeated cancellation must still join publication"
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert caught.value.__cause__ is None
        assert len(published) == 1
        assert published[0]["size"] > 0
    asyncio.run(scenario())
