import sqlite3
import threading

import pytest

from hub.bootstrap import create_app, start_background_jobs
from hub.config import FleetConfig
from hub.infrastructure.browser_repository import BrowserRepository, BrowserRepositoryError


OWNER = "owner@example.test"


def _repositories(tmp_path):
    app = create_app(FleetConfig.from_root(
        tmp_path, dev_operator=OWNER, platform_enabled=True,
        platform_browser_enabled=True,
    ))
    fleet = app.extensions["fleet"]
    return app, fleet["platform_repository"], fleet["repositories"]["browser"]


def _node(platform, node_id, *, enabled=True, last_seen_at=None):
    platform.upsert_node(OWNER, {"node_id": node_id, "enabled": enabled})
    conn = sqlite3.connect(platform.db_path)
    try:
        conn.execute(
            "UPDATE nodes SET last_seen_at=? WHERE owner_id=? AND node_id=?",
            (last_seen_at, OWNER, node_id),
        )
        conn.commit()
    finally:
        conn.close()


def _session(browser, session_id, node_id, *, state="open", updated_at=1.0):
    row = browser.create_session(
        OWNER, workspace_id="workspace-a", run_id="run-a", node_id=node_id,
        profile_id="profile-a", session_id=session_id,
    )
    conn = browser._connect()
    try:
        conn.execute(
            "UPDATE browser_sessions SET state=?,updated_at=? WHERE session_id=?",
            (state, updated_at, row["session_id"]),
        )
    finally:
        conn.close()
    return row["session_id"]


def test_stale_reconciliation_marks_only_observed_expired_enabled_node_sessions(tmp_path):
    _, platform, browser = _repositories(tmp_path)
    _node(platform, "node-stale", last_seen_at=699.0)
    _node(platform, "node-boundary", last_seen_at=700.0)
    _node(platform, "node-never-seen")
    _node(platform, "node-disabled", enabled=False, last_seen_at=1.0)
    _session(browser, "stale-open-session", "node-stale")
    _session(browser, "stale-closing-session", "node-stale", state="closing")
    _session(browser, "boundary-session", "node-boundary")
    _session(browser, "never-seen-session", "node-never-seen")
    _session(browser, "disabled-session", "node-disabled")
    _session(browser, "missing-node-session", "node-missing")
    _session(browser, "sibling-owner-session", "node-stale")
    conn = browser._connect()
    try:
        conn.execute(
            "UPDATE browser_sessions SET owner_id=? WHERE session_id=?",
            ("other@example.test", "sibling-owner-session"),
        )
    finally:
        conn.close()

    assert browser.reconcile_stale_sessions(now=1000.0) == 2
    assert browser.get_session(OWNER, "stale-open-session")["state"] == "unknown"
    assert browser.get_session(OWNER, "stale-closing-session")["state"] == "unknown"
    for session_id in (
        "boundary-session", "never-seen-session", "disabled-session", "missing-node-session",
    ):
        assert browser.get_session(OWNER, session_id)["state"] == "open"
    assert browser.get_session("other@example.test", "sibling-owner-session")["state"] == "open"
    assert browser.reconcile_stale_sessions(now=1000.0) == 0


def test_stale_reconciliation_drains_backlog_in_bounded_batches(tmp_path):
    _, platform, browser = _repositories(tmp_path)
    _node(platform, "node-stale", last_seen_at=0.0)
    session_ids = [f"stale-session-{index:03d}" for index in range(130)]
    for session_id in session_ids:
        _session(browser, session_id, "node-stale")

    assert browser.reconcile_stale_sessions(now=301.0, limit=128) == 128
    assert browser.reconcile_stale_sessions(now=301.0, limit=128) == 2
    assert browser.reconcile_stale_sessions(now=301.0, limit=128) == 0
    assert all(browser.get_session(OWNER, sid)["state"] == "unknown" for sid in session_ids)


@pytest.mark.parametrize("limit", [0, 129, True, 1.5, "2"])
def test_stale_reconciliation_rejects_unbounded_or_non_integer_batch(limit, tmp_path):
    _, _, browser = _repositories(tmp_path)
    with pytest.raises(BrowserRepositoryError) as caught:
        browser.reconcile_stale_sessions(now=1000.0, limit=limit)
    assert caught.value.code == "invalid_reconciliation_limit"


@pytest.mark.parametrize("now", [float("nan"), float("inf"), "bad"])
def test_stale_reconciliation_rejects_invalid_clock_values(now, tmp_path):
    _, _, browser = _repositories(tmp_path)
    with pytest.raises(BrowserRepositoryError) as caught:
        browser.reconcile_stale_sessions(now=now)
    assert caught.value.code == "invalid_reconciliation_time"


def test_stale_reconciliation_skips_legacy_nodes_without_liveness_columns(tmp_path):
    _, _, browser = _repositories(tmp_path)
    conn = browser._connect()
    try:
        conn.execute("DROP TABLE nodes")
        conn.execute("CREATE TABLE nodes (owner_id TEXT, node_id TEXT)")
    finally:
        conn.close()
    assert browser.reconcile_stale_sessions(now=1000.0) == 0


def test_background_stale_reconciliation_is_gate_scoped_and_stoppable(tmp_path, monkeypatch):
    def build(name, **gates):
        return create_app(FleetConfig.from_root(
            tmp_path / name, dev_operator=OWNER, platform_enabled=True,
            platform_browser_enabled=True, **gates,
        ))

    calls = []
    def fake_job(callback, *, interval_s):
        calls.append((callback, interval_s))
        return threading.Event()

    import hub.application.reconciliation_service as lifecycle
    monkeypatch.setattr(lifecycle, "start_browser_session_reconciliation", fake_job)
    monkeypatch.setattr(lifecycle, "start_browser_capture_staging_cleanup", fake_job)
    monkeypatch.setattr(lifecycle, "start_browser_capture_retention", fake_job)
    monkeypatch.setattr(lifecycle, "start_reconciliation", lambda *_a, **_k: threading.Event())
    monkeypatch.setattr(lifecycle, "start_lease_reconciler", lambda *_a, **_k: threading.Event())

    disabled = build("disabled")
    disabled_jobs = start_background_jobs(disabled)
    assert "browser_session_reconciliation" not in disabled_jobs

    enabled = build("enabled", platform_remote_execution_enabled=True,
                    platform_require_command_signature=True)
    enabled_jobs = start_background_jobs(
        enabled, browser_session_reconciliation_interval_s=17,
        browser_capture_staging_cleanup_interval_s=19,
    )
    assert "browser_session_reconciliation" in enabled_jobs
    stale_callback, stale_interval = next(
        item for item in calls if item[0].__name__ == "reconcile_stale_sessions"
    )
    assert stale_interval == 17
    assert stale_callback.__self__ is enabled.extensions["fleet"]["repositories"]["browser"]
    cleanup_callback, cleanup_interval = next(
        item for item in calls if item[0].__name__ == "cleanup_staging"
    )
    assert cleanup_interval == 19
    assert cleanup_callback.__self__ is enabled.extensions["fleet"]["services"]["platform_artifacts"]
    assert "browser_capture_staging_cleanup" in enabled_jobs
    assert "browser_capture_retention" in enabled_jobs
    retention_callback, retention_interval = next(
        item for item in calls if item[0].__name__ == "cleanup_expired"
    )
    assert retention_interval == 300
    assert retention_callback.__self__ is enabled.extensions["fleet"]["repositories"]["browser_capture_gc"]
    assert "browser_capture_retention" not in disabled_jobs
    for jobs in (disabled_jobs, enabled_jobs):
        for stop in jobs.values():
            stop.set()
