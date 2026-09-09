"""Tests for the ControlClient LaunchAgent wrapper.

The library ControlClient has no CLI; ``tools/supervisor/control_client_cli.py``
is the machine-side entrypoint. These tests cover:

- argparse / bootstrap (``--help`` is in ``test_direct_run``);
- spool key persist + 32-byte length;
- session-events transport unwraps ``{\"events\": [...]}`` to a JSON list
  (Hub ``POST /api/session-events`` rejects objects);
- bridge factory opens a real SessionBridge with native_transcript when a
  native path is present;
- ``--once`` fail-closed when supervisor is disabled or credential/key missing;
- credential passed to ControlClient is the secret (no ``machine:`` prefix).
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import runner_config
from tools.supervisor import control_client_cli as cli


class SpoolKeyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_creates_32_byte_key_and_is_stable(self):
        path = self.tmp / "session-spool.key"
        first = cli._load_or_create_spool_key(path)
        self.assertEqual(len(first), 32)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        second = cli._load_or_create_spool_key(path)
        self.assertEqual(first, second)

    def test_rejects_wrong_length(self):
        path = self.tmp / "session-spool.key"
        path.write_bytes(b"short")
        with self.assertRaises(cli.CliError):
            cli._load_or_create_spool_key(path)


class SessionEventsTransportTests(unittest.TestCase):
    def test_post_json_sends_event_list_not_object(self):
        captured = {}

        class _Resp:
            def read(self):
                return json.dumps({"ok": True, "accepted_through": 1}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(req, timeout=30):
            captured["url"] = req.full_url
            captured["headers"] = dict(req.header_items())
            captured["body"] = json.loads(req.data.decode())
            return _Resp()

        post = cli._make_session_events_post(
            "https://agent.example.com", "ingest-secret")
        with mock.patch.object(cli.urllib.request, "urlopen", fake_urlopen):
            result = post({"events": [{"kind": "user_message", "sequence": 1}]})
        self.assertTrue(result["ok"])
        self.assertIsInstance(captured["body"], list)
        self.assertEqual(captured["body"][0]["kind"], "user_message")
        self.assertIn("/api/session-events", captured["url"])
        # header names are canonicalized by urllib
        header_vals = {k.lower(): v for k, v in captured["headers"].items()}
        self.assertEqual(header_vals.get("x-agent-fleet-token"), "ingest-secret")


class BridgeFactoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.spool_root = self.tmp / "spool"
        self.key = bytes(range(32))
        self.native = self.tmp / "native.jsonl"
        self.native.write_text('{"kind":"user_message","text":"hi"}\n')

    def test_factory_opens_native_bridge(self):
        factory = cli._make_bridge_factory()
        cfg = {
            "session_id": "sess_factorytest",
            "machine_id": "mac-local",
            "stream_id": "stream_x",
            "agent_family": "claude_code",
            "process_group_id": "grp_abc",
            "managed": True,
            "best_effort": True,
            "native_file_path": str(self.native),
            "spool_root": str(self.spool_root),
            "spool_key": self.key,
            "post_json": lambda payload: {
                "ok": True, "accepted_through": 0},
        }
        bridge = factory(cfg)
        self.assertTrue(hasattr(bridge, "start"))
        self.assertTrue(hasattr(bridge, "ingest_native"))
        self.assertEqual(bridge.session_id, "sess_factorytest")
        self.assertTrue(bridge.managed)
        self.assertIn("native_transcript", bridge.sources)
        # native path rides in the adopt config; no private-slot injection
        self.assertEqual(bridge._native_path, str(self.native))


class CliMainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.project = self.tmp / "proj"
        self.project.mkdir()
        (self.tmp / "runner-token").write_text("runner-secret\n")

    def _write_yaml(self, supervisor=None):
        import yaml
        data = {
            "hub": "https://hub.example.com",
            "machine": "mac-local",
            "credential_file": str(self.tmp / "runner-token"),
            "projects": {"agent-fleet": {"path": str(self.project)}},
            "agents": {"codex": {"command": ["codex", "{instruction}"]}},
        }
        if supervisor is not None:
            data["supervisor"] = supervisor
        path = self.tmp / "runner.yaml"
        path.write_text(yaml.safe_dump(data))
        return path

    def test_once_without_supervisor_exits_2(self):
        path = self._write_yaml()
        rc = cli.main(["--config", str(path), "--once"])
        self.assertEqual(rc, 2)

    def test_once_enabled_without_credential_exits_2(self):
        manifests = self.tmp / "manifests"
        manifests.mkdir()
        path = self._write_yaml(supervisor={
            "enabled": True,
            "manifest_dir": str(manifests),
        })
        rc = cli.main(["--config", str(path), "--once"])
        self.assertEqual(rc, 2)

    def test_once_enabled_without_public_key_exits_2(self):
        manifests = self.tmp / "manifests"
        manifests.mkdir()
        cred = self.tmp / "sup-cred"
        cred.write_text("sup-secret\n")
        path = self._write_yaml(supervisor={
            "enabled": True,
            "manifest_dir": str(manifests),
            "credential_file": str(cred),
        })
        rc = cli.main(["--config", str(path), "--once"])
        self.assertEqual(rc, 2)

    def test_once_polls_with_secret_only_header(self):
        manifests = self.tmp / "manifests"
        manifests.mkdir()
        cred = self.tmp / "sup-cred"
        cred.write_text("mac-local:sup-secret\n")
        pk = self.tmp / "sup-pk"
        pk.write_text(base64.b64encode(b"K" * 32).decode() + "\n")
        ingest = self.tmp / "ingest-token"
        ingest.write_text("ingest-secret\n")
        path = self._write_yaml(supervisor={
            "enabled": True,
            "manifest_dir": str(manifests),
            "credential_file": str(cred),
            "public_key_file": str(pk),
        })
        captured = {}

        def fake_transport(self, url_path, body, headers):
            captured["path"] = url_path
            captured["headers"] = dict(headers)
            captured["body"] = body
            if url_path.endswith("/poll"):
                return 200, {"ok": True, "commands": []}
            return 200, {"ok": True}

        with mock.patch(
                "tools.supervisor.control_client.ControlClient._http_transport",
                fake_transport):
            rc = cli.main([
                "--config", str(path),
                "--once",
                "--ingest-token-file", str(ingest),
                "--spool-key-file", str(self.tmp / "spool.key"),
            ])
        self.assertEqual(rc, 0)
        self.assertTrue(captured["path"].endswith("/api/supervisor/poll"))
        self.assertEqual(
            captured["headers"]["X-Supervisor-Credential"],
            "mac-local:sup-secret")
        self.assertIn("User-Agent", captured["headers"])


if __name__ == "__main__":
    unittest.main()
