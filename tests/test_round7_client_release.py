"""Round 7: 客户端发布格式（fleet_core.client_release）+ hub 侧打包签名服务与路由。"""
import io
import tarfile
from pathlib import Path

import pytest
from support.hub_env import HubStores

from fleet_core import client_release as cr
from fleet_core.control import generate_ed25519_keypair
from hub.application import client_release_service as crs
from hub.application.client_release_service import ClientReleaseService
from hub.application.errors import ApplicationError
from hub.bootstrap import create_app
from report_schema import sanitize_client

MACHINE = "mac-local"
NOW = 1_800_000_000


def make_tree(root: Path, body: str = "x = 1\n") -> Path:
    (root / "tools").mkdir(parents=True)
    (root / "tools" / "a.py").write_text(body)
    (root / "tools" / "run.sh").write_text("#!/bin/sh\n")
    (root / "tools" / "run.sh").chmod(0o755)
    (root / "tools" / "__pycache__").mkdir()
    (root / "tools" / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"junk")
    (root / "fleet_core").mkdir()
    (root / "fleet_core" / "__init__.py").write_text("")
    (root / "report_schema.py").write_text("R = 1\n")
    (root / "hub").mkdir()
    (root / "hub" / "server.py").write_text("hub only\n")
    return root


def build(root: Path, version="v1", sequence=1, channel="stable") -> bytes:
    info = cr.release_info(version, sequence, channel, cr.tree_files(root))
    return cr.build_archive(root, info)


def manifest_for(private, public, artifact: bytes, **overrides):
    manifest = {
        "schemaVersion": cr.SCHEMA_VERSION, "kind": cr.MANIFEST_KIND,
        "channel": "stable", "sequence": 3, "version": "v3",
        "publishedAt": NOW, "issuedAt": NOW, "expiresAt": NOW + 3600,
        "artifact": {"size": len(artifact), "sha256": cr.sha256_bytes(artifact)},
        "keyId": cr.key_id(public),
    }
    manifest.update(overrides)
    return manifest, cr.sign_manifest(private, manifest)


def tar_gz(members) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for ti, data in members:
            tar.addfile(ti, io.BytesIO(data) if data is not None else None)
    return raw.getvalue()


def reg(name, data=b"x"):
    ti = tarfile.TarInfo(name)
    ti.size = len(data)
    return ti, data


# -- archive ----------------------------------------------------------------

def test_closure_excludes_hub_and_caches(tmp_path):
    files = cr.iter_client_files(make_tree(tmp_path))
    assert files == ["fleet_core/__init__.py", "report_schema.py", "tools/a.py", "tools/run.sh"]


def test_archive_is_deterministic_and_roundtrips(tmp_path):
    root = make_tree(tmp_path / "src")
    first, second = build(root), build(root)
    assert first == second
    info = cr.extract_archive(first, tmp_path / "out")
    assert (info["version"], info["sequence"]) == ("v1", 1)
    assert (tmp_path / "out" / "tools" / "a.py").read_text() == "x = 1\n"
    assert (tmp_path / "out" / "tools" / "run.sh").stat().st_mode & 0o111
    assert cr.read_release_info(tmp_path / "out")["files"] == cr.tree_files(root)


def test_build_rejects_tree_changed_after_hashing(tmp_path):
    root = make_tree(tmp_path)
    info = cr.release_info("v1", 1, "stable", cr.tree_files(root))
    (root / "tools" / "a.py").write_text("changed\n")
    with pytest.raises(cr.ReleaseFormatError, match="tree_changed"):
        cr.build_archive(root, info)


@pytest.mark.parametrize("name", ["/etc/passwd", "../evil", "a/../../evil"])
def test_extract_rejects_unsafe_paths(tmp_path, name):
    with pytest.raises(cr.ReleaseFormatError, match="unsafe_member"):
        cr.extract_archive(tar_gz([reg(name)]), tmp_path / "out")


def test_extract_rejects_symlink_and_duplicates(tmp_path):
    link = tarfile.TarInfo("tools/link")
    link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
    with pytest.raises(cr.ReleaseFormatError, match="unsafe_member"):
        cr.extract_archive(tar_gz([(link, None)]), tmp_path / "a")
    with pytest.raises(cr.ReleaseFormatError, match="duplicate_member"):
        cr.extract_archive(tar_gz([reg("x.py"), reg("x.py")]), tmp_path / "b")


def test_extract_requires_matching_release_info(tmp_path):
    with pytest.raises(cr.ReleaseFormatError, match="release_info_missing"):
        cr.extract_archive(tar_gz([reg("tools/a.py")]), tmp_path / "a")
    info = cr.canonical_json_bytes(cr.release_info("v1", 1, "stable", {
        "tools/a.py": cr.sha256_bytes(b"other")}))
    with pytest.raises(cr.ReleaseFormatError, match="release_files_mismatch"):
        cr.extract_archive(tar_gz([reg("tools/a.py"), reg(cr.RELEASE_INFO, info)]),
                           tmp_path / "b")
    with pytest.raises(cr.ReleaseFormatError, match="artifact_corrupt"):
        cr.extract_archive(b"not a tarball", tmp_path / "c")


# -- manifest ---------------------------------------------------------------

def test_manifest_signature_and_tamper():
    private, public = generate_ed25519_keypair()
    _, other_public = generate_ed25519_keypair()
    manifest, sig = manifest_for(private, public, b"artifact")
    assert cr.verify_manifest([public], manifest, sig)
    assert not cr.verify_manifest([other_public], manifest, sig)
    assert not cr.verify_manifest([public], {**manifest, "sequence": 4}, sig)
    assert not cr.verify_manifest([public], manifest, None)
    assert not cr.verify_manifest([public], "nope", sig)
    # 同 key 对其它域（supervisor 命令）的签名不能当清单用
    from fleet_core.control import sign_payload
    raw = sign_payload(private, cr.canonical_json_bytes(manifest))
    assert not cr.verify_manifest([public], manifest, raw)


def test_validate_manifest_expiry_and_channel():
    private, public = generate_ed25519_keypair()
    manifest, _ = manifest_for(private, public, b"artifact")
    assert cr.validate_manifest(manifest, now=NOW + 10, channel="stable") == manifest
    with pytest.raises(cr.ReleaseFormatError, match="manifest_expired"):
        cr.validate_manifest(manifest, now=NOW + 3600, channel="stable")
    with pytest.raises(cr.ReleaseFormatError, match="manifest_expired"):
        cr.validate_manifest(manifest, now=NOW - 301, channel="stable")
    with pytest.raises(cr.ReleaseFormatError, match="channel_mismatch"):
        cr.validate_manifest(manifest, now=NOW, channel="beta")
    for bad in ({"sequence": 0}, {"version": "../x"}, {"artifact": {"size": 0, "sha256": "0" * 64}}):
        with pytest.raises(cr.ReleaseFormatError, match="manifest_invalid"):
            cr.validate_manifest({**manifest, **bad}, now=NOW, channel="stable")


def test_sanitize_client_whitelist():
    assert sanitize_client({"version": "abc123", "sequence": 4, "channel": "stable",
                            "secret": "x"}) == {"version": "abc123", "sequence": 4,
                                                "channel": "stable"}
    assert sanitize_client({"version": "a b"}) is None
    assert sanitize_client("legacy") is None
    assert sanitize_client({"version": "legacy", "sequence": True, "channel": "X!"}) == {
        "version": "legacy", "sequence": 0, "channel": "stable"}


# -- hub service --------------------------------------------------------------

@pytest.fixture
def no_rebuild_cache(monkeypatch):
    monkeypatch.setattr(crs, "_REBUILD_CHECK_S", 0.0)


def service(root, tmp_path, key):
    return ClientReleaseService(source_root=root, state_path=tmp_path / "state" / "s.json",
                                signing_key=key, clock=lambda: NOW)


def test_sequence_bumps_only_on_closure_change(tmp_path, no_rebuild_cache):
    root = make_tree(tmp_path / "src")
    private, _ = generate_ed25519_keypair()
    svc = service(root, tmp_path, private)
    first = svc.current()
    assert first["sequence"] == 1 and first["version"].startswith("dev-")
    (root / "hub" / "server.py").write_text("hub-only change\n")
    assert svc.current()["sequence"] == 1
    # 新进程（hub 重启）沿用持久化序号
    assert service(root, tmp_path, private).current()["sequence"] == 1
    (root / "tools" / "a.py").write_text("x = 2\n")
    second = svc.current()
    assert second["sequence"] == 2 and second["sha256"] != first["sha256"]
    assert svc.artifact(second["sha256"])
    with pytest.raises(ApplicationError) as err:
        svc.artifact(first["sha256"])
    assert err.value.code == "artifact_not_found"


def test_same_commit_dirty_tree_gets_unique_version(tmp_path, no_rebuild_cache):
    root = make_tree(tmp_path / "src")
    (root / "RELEASE_ORIGIN").write_text("commit: 0123456789abcdef\n")
    svc = service(root, tmp_path, None)
    assert svc.current()["version"] == "0123456789ab"
    (root / "tools" / "a.py").write_text("x = 3\n")
    assert svc.current()["version"] == "0123456789ab-s2"


def test_signed_manifest_verifies_and_unsigned_fails_closed(tmp_path):
    root = make_tree(tmp_path / "src")
    private, public = generate_ed25519_keypair()
    signed = service(root, tmp_path, private).signed_manifest()
    manifest = signed["manifest"]
    assert cr.verify_manifest([public], manifest, signed["signature"])
    assert cr.validate_manifest(manifest, now=NOW, channel="stable")
    with pytest.raises(ApplicationError) as err:
        service(root, tmp_path / "u", None).signed_manifest()
    assert err.value.status == 503 and err.value.code == "client_release_unsigned"


# -- routes -------------------------------------------------------------------

def make_app(tmp_path, signing_raw):
    hub = HubStores(tmp_path / "state")
    cfg = hub.config(tmp_path, ingest_token="test-only",
                     runner_credentials={MACHINE: "runner-secret"},
                     supervisor_signing_raw=signing_raw)
    return create_app(cfg)


RUNNER = {"X-Runner-Credential": f"{MACHINE}:runner-secret"}


def test_routes_require_runner_and_serve_signed_release(tmp_path):
    private, public = generate_ed25519_keypair()
    client = make_app(tmp_path, private).test_client()
    assert client.get("/api/client/releases/stable/manifest").status_code in (401, 403)
    assert client.get("/api/client/releases/stable/manifest", headers={
        "X-Runner-Credential": f"{MACHINE}:wrong"}).status_code in (401, 403)
    resp = client.get("/api/client/releases/stable/manifest", headers=RUNNER)
    assert resp.status_code == 200
    body = resp.get_json()
    manifest = body["manifest"]
    assert cr.verify_manifest([public], manifest, body["signature"])
    assert client.get("/api/client/releases/beta/manifest", headers=RUNNER).status_code == 404
    art = client.get(f"/api/client/releases/artifacts/{manifest['artifact']['sha256']}",
                     headers=RUNNER)
    assert art.status_code == 200 and art.mimetype == "application/gzip"
    assert cr.sha256_bytes(art.data) == manifest["artifact"]["sha256"]
    info = cr.extract_archive(art.data, tmp_path / "unpacked")
    assert not any(path.startswith("hub/") for path in info["files"])
    assert "tools/agent-runner.py" in info["files"]
    missing = client.get("/api/client/releases/artifacts/" + "0" * 64, headers=RUNNER)
    assert missing.status_code == 404


def test_manifest_route_unsigned_is_503(tmp_path, monkeypatch):
    monkeypatch.setattr("hub.auth.load_supervisor_signing_key", lambda: None)
    client = make_app(tmp_path, None).test_client()
    resp = client.get("/api/client/releases/stable/manifest", headers=RUNNER)
    assert resp.status_code == 503


def test_ingest_client_reaches_status_and_detail(tmp_path):
    private, _ = generate_ed25519_keypair()
    client = make_app(tmp_path, private).test_client()
    resp = client.post("/api/ingest", headers={"X-Agent-Fleet-Token": "test-only"}, json={
        "machine": MACHINE, "agent": {}, "system": {"platform": "darwin"},
        "client": {"version": "abc123def456", "sequence": 7, "channel": "stable",
                   "extra": "dropped"}})
    assert resp.status_code in (200, 202), resp.get_json()
    status = client.get("/api/status").get_json()
    row = next(m for m in status["machines"] if m["machine"] == MACHINE)
    assert row["client"] == {"version": "abc123def456", "sequence": 7, "channel": "stable"}
    assert status["client_release"]["channel"] == "stable"
    assert status["client_release"]["sequence"] >= 1
    detail = client.get(f"/api/machines/{MACHINE}").get_json()
    assert detail["current"]["client"]["sequence"] == 7
