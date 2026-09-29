import json
import os
from pathlib import Path

import pytest

from hub.integrations.komari import KomariClient
from hub.integrations.komari_capture import (
    MAX_CAPTURE_BYTES,
    KomariCaptureError,
    verify_komari_capture_bytes,
    verify_komari_capture_file,
)


FIXTURE = Path(__file__).parent / "fixtures/platform/komari_capture_v1.json"


def test_capture_report_contains_only_normalized_nodes():
    payload = FIXTURE.read_bytes()

    report = verify_komari_capture_bytes(payload, observed_at=100)

    assert set(report) == {"ok", "schema_version", "observed_at", "node_count", "nodes"}
    assert report["ok"] is True
    assert report["schema_version"] == 1
    assert report["observed_at"] == 100.0
    assert report["node_count"] == 2
    assert report["nodes"][0]["external_id"] == "capture-node-1"
    assert report["nodes"][0]["online"] is True
    assert report["nodes"][1]["external_id"] == "capture-node-2"

    rendered = json.dumps(report, sort_keys=True)
    assert "capture-secret-ignore" not in rendered
    assert "capture-token-ignore" not in rendered
    assert "capture-raw-ignore" not in rendered
    assert "capture-envelope-ignore" not in rendered


def test_capture_file_uses_same_sanitized_report(tmp_path):
    capture = tmp_path / "capture.json"
    capture.write_bytes(FIXTURE.read_bytes())

    report = verify_komari_capture_file(capture, observed_at=101)

    assert report["node_count"] == 2
    assert report["observed_at"] == 101.0
    assert report["nodes"][0]["detail"] == {
        "status": "online",
        "version": "capture-1",
    }


def test_empty_versioned_capture_has_explicit_observation_time():
    report = verify_komari_capture_bytes(
        b'{"schema_version": 1, "nodes": []}', observed_at=123
    )

    assert report == {
        "ok": True,
        "schema_version": 1,
        "observed_at": 123.0,
        "node_count": 0,
        "nodes": [],
    }


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"not-json", "invalid_capture"),
        (b"\xff", "invalid_capture"),
        (json.dumps({"schema_version": 2, "nodes": []}).encode(), "unsupported_schema"),
        (json.dumps({"schema_version": 1, "nodes": "not-a-list"}).encode(), "invalid_capture"),
    ],
)
def test_capture_bytes_fail_closed_without_echoing_input(payload, code):
    with pytest.raises(KomariCaptureError) as exc:
        verify_komari_capture_bytes(payload)

    assert exc.value.code == code
    assert str(exc.value) == code
    assert "capture-token" not in str(exc.value)


def test_capture_bytes_reject_non_bytes_and_invalid_timestamp():
    with pytest.raises(KomariCaptureError) as non_bytes:
        verify_komari_capture_bytes("{}")
    assert non_bytes.value.code == "invalid_capture"

    with pytest.raises(KomariCaptureError) as invalid_time:
        verify_komari_capture_bytes(FIXTURE.read_bytes(), observed_at=float("nan"))
    assert invalid_time.value.code == "invalid_capture"


def test_unversioned_legacy_capture_remains_normalizable():
    report = verify_komari_capture_bytes(
        b'{"nodes": [{"id": "legacy-capture-1", "online": true}]}',
        observed_at=102,
    )

    assert report["schema_version"] == 1
    assert report["nodes"][0]["external_id"] == "legacy-capture-1"


def test_capture_bytes_reject_oversized_payload():
    with pytest.raises(KomariCaptureError) as exc:
        verify_komari_capture_bytes(b"x" * (MAX_CAPTURE_BYTES + 1))

    assert MAX_CAPTURE_BYTES == KomariClient.MAX_BODY_BYTES
    assert exc.value.code == "capture_too_large"


def test_capture_file_rejects_symlink(tmp_path):
    source = tmp_path / "capture.json"
    source.write_bytes(FIXTURE.read_bytes())
    link = tmp_path / "link.json"
    link.symlink_to(source)

    with pytest.raises(KomariCaptureError) as exc:
        verify_komari_capture_file(link)

    assert exc.value.code == "invalid_capture_path"


def test_capture_file_rejects_directory_missing_path_and_non_path():
    with pytest.raises(KomariCaptureError) as directory:
        verify_komari_capture_file(Path(__file__).parent)
    assert directory.value.code == "invalid_capture_path"

    with pytest.raises(KomariCaptureError) as missing:
        verify_komari_capture_file(Path(__file__).parent / "missing-capture.json")
    assert missing.value.code == "invalid_capture_path"

    with pytest.raises(KomariCaptureError) as non_path:
        verify_komari_capture_file(42)
    assert non_path.value.code == "invalid_capture_path"


def test_capture_file_rejects_oversized_payload(tmp_path):
    capture = tmp_path / "oversized.json"
    capture.write_bytes(b"x" * (MAX_CAPTURE_BYTES + 1))

    with pytest.raises(KomariCaptureError) as exc:
        verify_komari_capture_file(capture)

    assert exc.value.code == "capture_too_large"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks are unavailable")
def test_capture_report_never_constructs_or_uses_a_network_client(monkeypatch):
    def fail_network(*_args, **_kwargs):
        raise AssertionError("capture verification must remain offline")

    monkeypatch.setattr("hub.integrations.komari.urlopen", fail_network)
    report = verify_komari_capture_bytes(FIXTURE.read_bytes(), observed_at=100)

    assert report["node_count"] == 2
