"""Loopback Chromium acceptance for bounded search and sequence deep links."""
import os
from pathlib import Path
import subprocess
from threading import Thread

import pytest
from werkzeug.serving import make_server

from hub.bootstrap import create_app
from hub.config import FleetConfig

ROOT = Path(__file__).resolve().parents[1]


def test_session_search_in_real_browser(tmp_path):
    if not os.environ.get("FLEET_PLAYWRIGHT_MODULE"):
        pytest.skip("Set FLEET_PLAYWRIGHT_MODULE to run Chromium acceptance")
    app = create_app(FleetConfig.from_root(
        tmp_path, frontend_dir=ROOT / "frontend", dev_operator="search@example.test",
        platform_enabled=True, session_repositories_enabled=True,
        session_search_enabled=True, session_encryption_raw=b"s" * 32,
    ))
    sessions = app.extensions["fleet"]["services"]["sessions"]
    search = app.extensions["fleet"]["services"]["session_search"]
    task, _ = search.task_repository.create_task(
        machine="search-node", agent_type="codex", project="search-project",
        instruction="search fixture", requested_by="search@example.test", client_token="search-browser")
    rows = [{
        "schema_version": 1, "event_id": f"search-event-{sequence}",
        "stream_id": "search-stream", "machine_id": "search-node",
        "session_id": "search-session", "process_group_id": "search-group",
        "attempt_id": task["attempt_id"], "sequence": sequence,
        "kind": "assistant_message", "capture_quality": "structured", "source": "fixture",
        "emitted_at": "2026-10-11T00:00:00Z",
        "payload": {"text": ("记录 登录完成 <img src=x onerror=window.injected=1>\nBearer browsersecretvalue"
                             if sequence == 143 else f"记录 {sequence}"), "is_complete": True},
    } for sequence in range(1, 152)]
    rows.append({**rows[-1], "event_id": "search-event-large", "sequence": 9007199254740993,
                 "payload": {"text": "超大序号", "is_complete": True}})
    for start in range(0, len(rows), 100):
        result = sessions.ingest_events(rows[start:start + 100])
        assert result["rejected"] == []
    server = make_server("127.0.0.1", 0, app, threaded=True)
    serving = Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        result = subprocess.run(
            ["node", str(ROOT / "tests/fixtures/frontend/session_search_browser.cjs"),
             f"http://127.0.0.1:{server.server_port}"], capture_output=True, text=True, timeout=65)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.shutdown()
        server.server_close()
        serving.join(5)
