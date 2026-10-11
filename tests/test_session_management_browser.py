"""Loopback Chromium acceptance for real question commands and sequence jumps."""
import os
from pathlib import Path
import subprocess
from threading import Thread

import pytest
from werkzeug.serving import make_server

from hub.bootstrap import create_app
from hub.config import FleetConfig

ROOT = Path(__file__).resolve().parents[1]


def test_session_management_browser(tmp_path):
    if not os.environ.get("FLEET_PLAYWRIGHT_MODULE"):
        pytest.skip("Set FLEET_PLAYWRIGHT_MODULE for Chromium acceptance")
    app = create_app(FleetConfig.from_root(tmp_path, frontend_dir=ROOT / "frontend",
        dev_operator="browser@example.test", session_repositories_enabled=True,
        session_encryption_raw=b"s" * 32, supervisor_enabled=True,
        supervisor_signing_raw=b"a" * 32, supervisor_credentials={"m1": "browser-receipt"},
        append_user_turn_enabled=True))
    sessions = app.extensions["fleet"]["services"]["sessions"]
    for width in (390, 1440):
        sid = "questions-" + str(width)
        def event(seq, kind, payload):
            return {"schema_version": 1, "event_id": f"evt-{width}-{seq}", "session_id": sid,
                "machine_id": "m1", "process_group_id": "g1", "stream_id": "st1", "sequence": seq,
                "kind": kind, "capture_quality": "structured", "source": "hooks",
                "emitted_at": "2026-10-11T00:00:00Z", "payload": payload}
        rows = [event(1, "session_start", {"agent_family": "claude", "adapter": "hooks", "interactive_questions": True}),
                event(2, "agent_state_changed", {"state": "waiting_for_user", "source": "hook", "confidence": "high", "reason": "user_input_required"})]
        for seq, kind in enumerate(("single", "multi", "text", "confirm"), 3):
            rows.append(event(seq, "interaction_request", {"request_id": kind, "question_type": kind,
                "prompt": "请选择 " + kind, "options": [] if kind == "text" else [
                    {"id": "a", "label": "选项甲"}, {"id": "b", "label": "选项乙"}],
                "allow_custom": kind == "text"}))
        sessions.ingest_events(rows)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(["node", str(ROOT / "tests/fixtures/frontend/session_management_browser.cjs"),
            f"http://127.0.0.1:{server.server_port}"], capture_output=True, text=True, timeout=90)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
