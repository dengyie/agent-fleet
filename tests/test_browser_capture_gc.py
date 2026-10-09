import hashlib
import json
import sqlite3

import pytest

from hub.infrastructure.browser_capture_gc import (
    BrowserCaptureGcError, BrowserCaptureGcRepository, REFERENCE_LIMIT,
)
from tools.platform.artifacts import ArtifactStore

PNG = b"\x89PNG\r\n\x1a\n" + b"bounded-test-png"
NOW = 40 * 24 * 60 * 60


def _database(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE browser_artifact_tickets (
            ticket_id TEXT PRIMARY KEY, state TEXT, content_type TEXT, artifact_id TEXT,
            owner_id TEXT, workspace_id TEXT, consumed_at REAL
        );
        CREATE TABLE execution_window_events (payload TEXT);
        CREATE TABLE run_events (payload TEXT);
        CREATE TABLE platform_commands (result TEXT);
        CREATE TABLE direct_refs (artifact_id TEXT);
        """
    )
    conn.commit()
    conn.close()


def _capture(root, db, *, created_at=0):
    store = ArtifactStore(root, clock=lambda: created_at)
    manifest = store.put_bytes("owner", "workspace", PNG)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO browser_artifact_tickets VALUES(?,?,?,?,?,?,?)",
        ("ticket-" + manifest["artifact_id"], "consumed", "image/png",
         manifest["artifact_id"], "owner", "workspace", created_at),
    )
    conn.commit()
    conn.close()
    return manifest["artifact_id"]


def _gc(tmp_path):
    db = tmp_path / "platform.db"
    root = tmp_path / "artifacts"
    _database(db)
    return db, root, BrowserCaptureGcRepository(db, root, clock=lambda: NOW)


def test_gc_removes_only_unreferenced_expired_capture(tmp_path):
    db, root, gc = _gc(tmp_path)
    removable = _capture(root, db)
    referenced = _capture(root, db)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO execution_window_events VALUES(?)",
                 (json.dumps({"artifact_id": referenced}),))
    conn.commit()
    conn.close()
    gc.init()

    assert gc.cleanup_expired() == 1
    assert not (root / removable).exists()
    assert (root / referenced / "content").is_file()
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM browser_capture_gc").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM browser_artifact_tickets").fetchone()[0] == 2
    conn.close()


def test_gc_candidate_batch_skips_referenced_prefix_without_starving_later_capture(tmp_path):
    db, root, gc = _gc(tmp_path)
    removable = _capture(root, db, created_at=1)
    referenced = [f"referenced-{index:03d}" for index in range(128)]
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO browser_artifact_tickets VALUES(?,?,?,?,?,?,?)",
        [(f"ticket-{artifact_id}", "consumed", "image/png", artifact_id,
          "owner", "workspace", 0) for artifact_id in referenced],
    )
    conn.executemany(
        "INSERT INTO execution_window_events VALUES(?)",
        [(json.dumps({"artifact_id": artifact_id}),) for artifact_id in referenced],
    )
    conn.commit()
    conn.close()
    gc.init()

    assert gc.cleanup_expired() == 1
    assert not (root / removable).exists()


def test_gc_fails_closed_for_malformed_reference_without_marking_candidates(tmp_path):
    db, root, gc = _gc(tmp_path)
    artifact_id = _capture(root, db)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO run_events VALUES(?)", ("not-json",))
    conn.commit()
    conn.close()
    gc.init()

    with pytest.raises(BrowserCaptureGcError, match="capture_gc_reference_malformed"):
        gc.cleanup_expired()
    assert (root / artifact_id / "content").is_file()
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM browser_capture_gc").fetchone()[0] == 0
    conn.close()


def test_gc_fails_closed_when_reference_scan_exceeds_global_bound(tmp_path):
    db, root, gc = _gc(tmp_path)
    artifact_id = _capture(root, db)
    conn = sqlite3.connect(db)
    conn.executemany("INSERT INTO direct_refs VALUES(?)",
                     [(f"ref-{index}",) for index in range(REFERENCE_LIMIT + 1)])
    conn.commit()
    conn.close()
    gc.init()

    with pytest.raises(BrowserCaptureGcError, match="capture_gc_reference_limit"):
        gc.cleanup_expired()
    assert (root / artifact_id / "content").is_file()


def test_gc_scans_run_command_and_direct_reference_surfaces(tmp_path):
    db, root, gc = _gc(tmp_path)
    run_ref = _capture(root, db)
    command_ref = _capture(root, db)
    direct_ref = _capture(root, db)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO run_events VALUES(?)",
                 (json.dumps({"result": {"artifact_id": run_ref}}),))
    conn.execute("INSERT INTO platform_commands VALUES(?)",
                 (json.dumps({"artifact_id": command_ref}),))
    conn.execute("INSERT INTO direct_refs VALUES(?)", (direct_ref,))
    conn.commit()
    conn.close()
    gc.init()

    assert gc.cleanup_expired() == 0
    assert all((root / artifact_id / "content").is_file()
               for artifact_id in (run_ref, command_ref, direct_ref))


def test_gc_rejects_scope_mismatch_and_extra_artifact_files(tmp_path):
    db, root, gc = _gc(tmp_path)
    artifact_id = _capture(root, db)
    manifest_path = root / artifact_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["owner_id"] = "other"
    manifest_path.write_text(json.dumps(manifest))
    gc.init()

    with pytest.raises(BrowserCaptureGcError, match="capture_gc_artifact_invalid"):
        gc.cleanup_expired()
    assert (root / artifact_id / "content").is_file()

    manifest["owner_id"] = "owner"
    manifest_path.write_text(json.dumps(manifest))
    (root / artifact_id / "unexpected").write_bytes(b"blocked")
    with pytest.raises(BrowserCaptureGcError, match="capture_gc_artifact_invalid"):
        gc.cleanup_expired()


def test_gc_treats_absent_optional_window_surface_as_empty(tmp_path):
    db, root, gc = _gc(tmp_path)
    artifact_id = _capture(root, db)
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE execution_window_events")
    conn.commit()
    conn.close()
    gc.init()

    assert gc.cleanup_expired() == 1
    assert not (root / artifact_id).exists()


def test_gc_fails_closed_when_core_reference_surface_is_missing(tmp_path):
    db, root, gc = _gc(tmp_path)
    artifact_id = _capture(root, db)
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE run_events")
    conn.commit()
    conn.close()
    gc.init()

    with pytest.raises(BrowserCaptureGcError, match="capture_gc_reference_surface_unknown"):
        gc.cleanup_expired()
    assert (root / artifact_id / "content").is_file()


def test_gc_clears_tombstone_after_an_already_completed_removal(tmp_path):
    db, root, gc = _gc(tmp_path)
    artifact_id = _capture(root, db)
    gc.init()
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO browser_capture_gc(artifact_id,marked_at) VALUES(?,?)",
        (artifact_id, NOW),
    )
    conn.commit()
    conn.close()
    (root / artifact_id / "content").unlink()
    (root / artifact_id / "manifest.json").unlink()
    (root / artifact_id).rmdir()

    assert gc.cleanup_expired() == 0
    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT COUNT(*) FROM browser_capture_gc WHERE artifact_id=?",
        (artifact_id,),
    ).fetchone()[0] == 0
    conn.close()


def test_gc_recovers_quarantine_after_interrupted_file_removal(tmp_path, monkeypatch):
    db, root, gc = _gc(tmp_path)
    artifact_id = _capture(root, db)
    gc.init()
    original = gc._quarantine_and_remove

    def interrupt_after_rename(value):
        (root / value).rename(root / (".gc-" + value))
        raise OSError("simulated stop after quarantine rename")

    monkeypatch.setattr(gc, "_quarantine_and_remove", interrupt_after_rename)
    with pytest.raises(BrowserCaptureGcError, match="capture_gc_failed"):
        gc.cleanup_expired()
    assert (root / (".gc-" + artifact_id)).is_dir()
    monkeypatch.setattr(gc, "_quarantine_and_remove", original)

    assert gc.cleanup_expired() == 1
    assert not (root / artifact_id).exists()
    assert not (root / (".gc-" + artifact_id)).exists()


def test_gc_verifies_manifest_content_hash_and_retention_boundary(tmp_path):
    db, root, gc = _gc(tmp_path)
    boundary_id = _capture(root, db, created_at=NOW - 30 * 24 * 60 * 60 + 1)
    artifact_id = _capture(root, db, created_at=NOW - 31 * 24 * 60 * 60)
    gc.init()

    manifest_path = root / artifact_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["sha256"] = hashlib.sha256(b"other").hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(BrowserCaptureGcError, match="capture_gc_artifact_invalid"):
        gc.cleanup_expired()
    assert (root / boundary_id / "content").is_file()


def test_gc_rejects_content_without_png_signature_even_when_hash_matches(tmp_path):
    db, root, gc = _gc(tmp_path)
    artifact_id = _capture(root, db)
    content_path = root / artifact_id / "content"
    invalid_content = b"not-a-png"
    content_path.write_bytes(invalid_content)
    manifest_path = root / artifact_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["size"] = len(invalid_content)
    manifest["sha256"] = hashlib.sha256(invalid_content).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    gc.init()

    with pytest.raises(BrowserCaptureGcError, match="capture_gc_artifact_invalid"):
        gc.cleanup_expired()
    assert content_path.read_bytes() == invalid_content
