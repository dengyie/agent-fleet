from pathlib import Path

from hub.bootstrap import create_app
from hub.config import FleetConfig


def _app(tmp_path: Path, *, enabled: bool):
    return create_app(FleetConfig.from_root(
        tmp_path, ingest_token="ingest-only", dev_operator="owner@example.test",
        platform_enabled=enabled,
    ))


def test_platform_gate_off_does_not_create_store_or_route(tmp_path):
    app = _app(tmp_path, enabled=False)
    assert not (tmp_path / "var" / "platform" / "platform.db").exists()
    response = app.test_client().get("/api/platform/v1/defaults")
    assert response.status_code == 404


def test_platform_routes_use_operator_domain_and_redact_model_secret(tmp_path):
    app = _app(tmp_path, enabled=True)
    client = app.test_client()
    assert client.get("/api/platform/v1/defaults").status_code == 200
    assert client.get("/api/platform/v1/defaults", headers={"X-Agent-Fleet-Token": "ingest-only"}).status_code == 401
    repo = app.extensions["fleet"]["services"]["platform_defaults"].repository
    repo.upsert_model("owner@example.test", {
        "profile_id": "default-model", "provider": "compatible",
        "model": "model-1", "secret_ref": "secret://do-not-return",
    })
    response = client.get("/api/platform/v1/models")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert "secret_ref" not in text
    assert "do-not-return" not in text


def test_defaults_require_if_match_and_conflict_is_bounded(tmp_path):
    app = _app(tmp_path, enabled=True)
    client = app.test_client()
    missing = client.put("/api/platform/v1/defaults", json={"defaults": {}})
    assert missing.status_code == 428
    assert missing.get_json()["error"] == "precondition_required"
    first = client.put("/api/platform/v1/defaults", json={"defaults": {}}, headers={"If-Match": "0"})
    assert first.status_code == 200
    conflict = client.put("/api/platform/v1/defaults", json={"defaults": {}}, headers={"If-Match": "0"})
    assert conflict.status_code == 409
    assert conflict.get_json()["error"] == "revision_conflict"
