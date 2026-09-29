from pathlib import Path

from hub.bootstrap import create_app
from hub.config import FleetConfig


def _app(tmp_path: Path):
    return create_app(FleetConfig.from_root(
        tmp_path, dev_operator="owner-a@example.test", platform_enabled=True,
    ))


def test_artifact_http_api_is_operator_scoped_and_integrity_checked(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    store = app.extensions["fleet"]["services"]["platform_artifacts"]
    source = tmp_path / "report.md"
    source.write_text("hello", encoding="utf-8")
    manifest = store.put_file("owner-a@example.test", "workspace-a", source)

    listed = client.get("/api/platform/v1/workspaces/workspace-a/artifacts")
    assert listed.status_code == 200
    assert listed.get_json()["artifacts"][0]["artifact_id"] == manifest["artifact_id"]
    content = client.get(
        f"/api/platform/v1/artifacts/{manifest['artifact_id']}/content?workspace_id=workspace-a"
    )
    assert content.status_code == 200
    assert content.data == b"hello"
    assert content.mimetype == "text/markdown"
    assert content.headers["Content-Disposition"] == 'attachment; filename="report.md"'
    assert content.headers["X-Artifact-Sha256"] == manifest["sha256"]

    other = client.get(
        f"/api/platform/v1/artifacts/{manifest['artifact_id']}/content?workspace_id=workspace-b"
    )
    assert other.status_code == 404
    unauthenticated = app.test_client().get(
        "/api/platform/v1/workspaces/workspace-a/artifacts",
        headers={"Cf-Access-Authenticated-User-Email": "owner-b@example.test"},
    )
    assert unauthenticated.status_code == 200
    assert unauthenticated.get_json()["artifacts"] == []

    content_path = (
        tmp_path / "var" / "platform" / "artifacts"
        / manifest["artifact_id"] / "content"
    )
    content_path.write_bytes(b"tampered")
    corrupt = client.get(
        f"/api/platform/v1/artifacts/{manifest['artifact_id']}/content?workspace_id=workspace-a"
    )
    assert corrupt.status_code == 409
    assert corrupt.get_json()["error"] == "artifact_corrupt"


def test_artifact_http_api_rejects_missing_or_invalid_scope(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    assert client.get("/api/platform/v1/workspaces/../artifacts").status_code in {400, 404}
    response = client.get("/api/platform/v1/artifacts/id/content")
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_workspace"


def test_artifact_preview_is_bounded_owner_scoped_and_text_only(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    store = app.extensions["fleet"]["services"]["platform_artifacts"]
    source = tmp_path / "report.md"
    source.write_text("<b>literal</b>\n" + ("x" * 70_000), encoding="utf-8")
    manifest = store.put_file("owner-a@example.test", "workspace-a", source)

    response = client.get(
        f"/api/platform/v1/artifacts/{manifest['artifact_id']}/preview?workspace_id=workspace-a"
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["preview"]["truncated"] is True
    assert payload["preview"]["bytes"] == store.MAX_PREVIEW_BYTES
    assert payload["preview"]["text"].startswith("<b>literal</b>")
    assert payload["artifact"]["artifact_id"] == manifest["artifact_id"]

    denied = client.get(
        f"/api/platform/v1/artifacts/{manifest['artifact_id']}/preview?workspace_id=workspace-b"
    )
    assert denied.status_code == 404

    binary = tmp_path / "archive.bin"
    binary.write_bytes(b"binary")
    binary_manifest = store.put_file(
        "owner-a@example.test", "workspace-a", binary,
        content_type="application/octet-stream",
    )
    unsupported = client.get(
        f"/api/platform/v1/artifacts/{binary_manifest['artifact_id']}/preview?workspace_id=workspace-a"
    )
    assert unsupported.status_code == 415
    assert unsupported.get_json()["error"] == "preview_unsupported"

    content_path = (
        tmp_path / "var" / "platform" / "artifacts"
        / manifest["artifact_id"] / "content"
    )
    content_path.write_bytes(b"tampered")
    corrupt = client.get(
        f"/api/platform/v1/artifacts/{manifest['artifact_id']}/preview?workspace_id=workspace-a"
    )
    assert corrupt.status_code == 409
    assert corrupt.get_json()["error"] == "artifact_corrupt"
