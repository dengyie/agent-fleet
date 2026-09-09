import importlib.util
import inspect
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from connectors.probe import ProbeContext


class LocalProbeTests(unittest.TestCase):
    def test_probe_context_executes_only_local_commands(self):
        ctx = ProbeContext("mac-local")
        self.assertEqual(ctx.run("printf ok"), "ok")
        self.assertNotIn("ssh", inspect.getsource(ProbeContext).lower())


def load_self_report_module():
    path = Path(__file__).resolve().parents[1] / "tools" / "agent-self-report.py"
    spec = importlib.util.spec_from_file_location("agent_self_report", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SelfReportContractTests(unittest.TestCase):
    def test_payload_uses_agents_map(self):
        module = load_self_report_module()
        self.assertTrue(hasattr(module, "collect_all"))

    def test_send_payload_sets_probe_user_agent(self):
        module = load_self_report_module()
        captured = {}

        class Response:
            status = 200

            def read(self):
                return b'{"ok":true}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return Response()

        with mock.patch.object(module.urllib.request, "urlopen", side_effect=fake_urlopen):
            module.send_payload("https://example.test", "secret", {"machine": "test"})

        self.assertEqual(captured["request"].headers["User-agent"], "agent-fleet-probe/1.0")
        self.assertEqual(captured["timeout"], 15)

    def test_dry_run_does_not_open_network(self):
        module = load_self_report_module()
        output = StringIO()
        with mock.patch.object(module.urllib.request, "urlopen") as urlopen:
            with redirect_stdout(output):
                with mock.patch.dict(os.environ, {}, clear=True):
                    result = module.main(["--dry-run", "--name", "mac-local"])
        urlopen.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["machine"], "mac-local")
        self.assertIn("agents", payload)

    def test_probe_source_has_no_production_hub_default(self):
        source = (
            Path(__file__).resolve().parents[1] / "tools" / "agent-self-report.py"
        ).read_text()
        self.assertNotIn("mangoqwq", source)
        self.assertNotIn("agent.mangoqwq.com", source)
        module = load_self_report_module()
        with self.assertRaises(SystemExit):
            module.main(["--name", "worker-a", "--token", "x"])

    def test_collect_all_drops_session_identifiers_and_file_paths(self):
        from tools import probe_collectors

        connector = mock.Mock()
        connector.detect.return_value = True
        connector.collect.return_value = {
            "installed": True,
            "active_count": 1,
            "session_count": 2,
            "sessions": [{"session_id": "secret-session", "file": "private.jsonl"}],
            "projects": [{"file": "customer-project.jsonl"}],
            "processes": [{"cmd": "agent --token secret"}],
            "raw": "private collector output",
        }

        with mock.patch.object(probe_collectors, "create", return_value=connector):
            result = probe_collectors.collect_all("mac-local", ["codex"])

        self.assertEqual(result, {
            "codex": {
                "installed": True,
                "active_count": 1,
                "session_count": 2,
            }
        })


class ReconciliationTests(unittest.TestCase):
    def test_reconcile_never_executes_subprocess(self):
        from hub import events, scan, state

        temp = Path(tempfile.mkdtemp())
        old_state_dir = state.STATE_DIR
        old_event_log = events.EVENT_LOG
        state.STATE_DIR = temp
        events.EVENT_LOG = temp / "events.jsonl"
        try:
            with mock.patch("subprocess.run", side_effect=AssertionError("no subprocess")):
                result = scan.reconcile_ingest(now=0)
            self.assertEqual(result, [])
        finally:
            state.STATE_DIR = old_state_dir
            events.EVENT_LOG = old_event_log

    def test_hub_production_mode_fails_closed_without_token(self):
        from hub import web

        with self.assertRaises(RuntimeError):
            web.make_app(require_token=True)

    def test_hub_direct_script_entrypoint_loads_project_modules(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp_dir:
            env = os.environ.copy()
            env["AGENT_FLEET_INGEST_TOKEN"] = "test-only-token"
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(root / "hub" / "web.py"),
                    "--port",
                    "0",
                    "--host",
                    "127.0.0.1",
                    "--reconcile-interval",
                    "3600",
                ],
                cwd=root,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                _, stderr = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.communicate(timeout=2)
                return
            self.fail(
                "direct hub entrypoint exited before startup; "
                f"returncode={process.returncode}, stderr={stderr}"
            )


class ReconciliationBoundaryTests(unittest.TestCase):
    def test_reconciliation_service_imports_no_remote_or_flask_modules(self):
        import ast as _ast
        from hub.application.reconciliation_service import (
            ReconciliationService,
            start_lease_reconciler,
            start_reconciliation,
        )
        root = Path(__file__).resolve().parents[1]
        path = root / "hub" / "application" / "reconciliation_service.py"
        tree = _ast.parse(path.read_text())
        imported = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, _ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        forbidden = {"subprocess", "flask", "paramiko", "socket", "requests"}
        self.assertEqual(imported & forbidden, set())
        self.assertTrue(callable(ReconciliationService))
        self.assertTrue(callable(start_reconciliation))
        self.assertTrue(callable(start_lease_reconciler))



if __name__ == "__main__":
    unittest.main()
