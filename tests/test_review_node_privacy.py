"""Review fix #1: transcripts are archived, but secrets never leave the node.

source_record (complete archive) and normalized previews keep agent text and
paths; secret/credential patterns are replaced on the node before the spool
(and therefore the upload payload), and the Hub re-applies the same idempotent
rules before storage (transcripts, runner log lines and log summary).
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from hub.application.runner_service import RunnerService
from hub.infrastructure.transcript_repository import TranscriptRepository
from tools.session.bridge import SessionBridge
from fleet_core.session.redact import redact_secrets_text

SK = "sk-proj-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"
GHP = "ghp_" + "abcdEFGHijklMNOPqrstUVWXyz0123456789"
AWS = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
BEARER = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJl"
SECRETS = (SK, GHP, AWS, BEARER, "hunter2-plain-bearer")

SECRET_LINE = (
    f"export OPENAI_API_KEY={SK}\n"
    f"AWS_SECRET_ACCESS_KEY={AWS}\n"
    f"git push https://x:{GHP}@github.com/o/r\n"
    f"curl -H 'Authorization: Bearer {BEARER}' /Users/me/project/file.py\n"
    "Authorization: Bearer hunter2-plain-bearer\n"
)

MACH = "host-privacy"
SESS = "sess_privacy"
KEY = bytes(range(32))


def _manifest():
    return {
        "agent": "codex", "version": "1", "installed": True, "spawn": False,
        "resume": False, "native_transcript": False, "structured_stream": True,
        "hooks": False, "pty": False, "supported_event_kinds": [],
        "quality_by_kind": {}, "diagnostics": [],
    }


class RedactSecretsTextTests(unittest.TestCase):
    def test_plain_text_secrets_removed_paths_kept(self):
        out, report = redact_secrets_text(SECRET_LINE)
        for secret in SECRETS:
            self.assertNotIn(secret, out)
        self.assertIn("/Users/me/project/file.py", out)
        self.assertGreater(report.replaced, 0)

    def test_json_line_stays_valid_json(self):
        line = json.dumps({"type": "event_msg", "payload": {
            "type": "exec_command_end", "stdout": SECRET_LINE,
            "cwd": "/home/u/repo", "n": 3, "token": "opaque-value"}})
        out, _ = redact_secrets_text(line)
        parsed = json.loads(out)
        self.assertEqual(parsed["payload"]["cwd"], "/home/u/repo")
        self.assertEqual(parsed["payload"]["n"], 3)
        self.assertNotIn("opaque-value", out)
        for secret in SECRETS:
            self.assertNotIn(secret, out)

    def test_idempotent(self):
        once, _ = redact_secrets_text(SECRET_LINE + "token=abc; password=p w\n")
        twice, report = redact_secrets_text(once)
        self.assertEqual(once, twice)
        self.assertEqual(report.replaced, 0)

    def test_clean_json_line_unchanged_bytes(self):
        line = '{"a": 1,  "b": "hello /tmp/x"}'
        self.assertEqual(redact_secrets_text(line)[0], line)


class BridgeUploadPayloadTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="fleet-privacy-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.posted = []

    def _post(self, batch):
        self.posted.append(json.dumps(batch))
        events = batch["events"]
        return {"accepted_through": events[-1]["sequence"] if events else 0}

    def test_source_record_and_messages_upload_without_secrets(self):
        bridge = SessionBridge(_manifest(), {
            "session_id": SESS, "machine_id": MACH,
            "spool_root": self.dir / "spool", "spool_key": KEY,
            "preserve_source": True, "post_json": self._post,
        })
        self.addCleanup(bridge.close)
        bridge.start()
        # 原始行（非 JSON）与超过 4000 码点的长行（密钥跨分片边界）
        bridge.record_source("x" * 3990 + SECRET_LINE)
        bridge.record_source(json.dumps({"stdout": SECRET_LINE}))
        bridge.ingest_one({"kind": "user_message",
                           "payload": {"text": SECRET_LINE}})
        bridge.flush()
        payload = "".join(self.posted)
        self.assertIn("source_record", payload)
        self.assertIn("/Users/me/project/file.py", payload)
        for secret in SECRETS:
            self.assertNotIn(secret, payload)
        # 加密 spool 回放也不含密钥
        spooled = json.dumps(bridge._spool.read_after(0, 1000, 1 << 22))
        for secret in SECRETS:
            self.assertNotIn(secret, spooled)


def _event(seq, kind, text):
    return {
        "schema_version": 1, "event_id": f"evt_priv_{seq}",
        "stream_id": "stream_priv", "machine_id": MACH, "session_id": SESS,
        "sequence": seq, "kind": kind, "capture_quality": "structured",
        "source": "bridge", "emitted_at": "2026-08-26T00:00:00Z",
        "payload": {"text": text, "is_complete": True},
    }


class HubStorageTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="fleet-privacy-hub-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_transcript_repository_redacts_before_storage(self):
        repo = TranscriptRepository(self.dir / "t.db", key=KEY)
        repo.init()
        self.assertEqual(repo.ingest(_event(1, "source_record", SECRET_LINE)).status, "accepted")
        self.assertEqual(repo.ingest(_event(2, "user_message", SECRET_LINE)).status, "accepted")
        stored = json.dumps(repo.read_redacted(SESS))
        raw = json.dumps([repo.read_raw(SESS, "evt_priv_1", actor="op"),
                          repo.read_raw(SESS, "evt_priv_2", actor="op")])
        db_bytes = (self.dir / "t.db").read_bytes()
        for secret in SECRETS:
            self.assertNotIn(secret, stored)
            self.assertNotIn(secret, raw)
            self.assertNotIn(secret.encode(), db_bytes)
        self.assertIn("/Users/me/project/file.py", stored)

    def test_runner_log_lines_and_summary_redacted(self):
        emitted = []
        stored = {}

        class Repo:
            def heartbeat(self, **kw):
                return {"task_id": "t1"}

            def complete_task(self, **kw):
                stored.update(kw)
                return {"task_id": "t1", "state": "done"}

        class Pub:
            def emit(self, event, **kw):
                emitted.append(kw)

        svc = RunnerService(Repo(), Pub())
        svc.heartbeat("m", "a", "n", log_lines=SECRET_LINE.splitlines())
        svc.result("m", "a", "n", 0, log_summary=SECRET_LINE)
        blob = json.dumps(emitted) + json.dumps(stored.get("log_summary"))
        for secret in SECRETS:
            self.assertNotIn(secret, blob)
        self.assertIn("[REDACTED:", blob)


if __name__ == "__main__":
    unittest.main()
