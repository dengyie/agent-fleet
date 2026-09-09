"""Tests for the Task 7 Session Bridge orchestration.

Covers:
- ``SessionBridge.open(manifest, session_config) -> SessionBridge`` with
  source adapter selection driven ONLY by capability manifests;
- the source read -> normalize -> redact -> append -> upload ordering;
- redaction precedes the spool append (no raw secret/path text in durable
  records);
- managed vs unmanaged capture semantics (non-managed = ``managed=false``,
  ``capture_quality=best_effort``, ``control_capability=unavailable``);
- the Task 6 POST session-events response contract over the injected
  uploader/post callable (accepted_through, next_cursor, bounded rejects);
- fake-source ingestion round-trips through a Hub HTTP test client;
- source failure isolation into a bounded ``capture_gap`` event.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from session_schema import validate_event
from tools.session.bridge import (
    SessionBridge,
    build_capture_gap_event,
    build_capture_quality_event,
)
from tools.session.capture import hooks, pty as pty_module
from tools.session.spool import LocalSpool
from tools.session.uploader import SessionUploader

SESS = "sess_bridge"
MACH = "host-bridge"
STREAM = "stream_bridge_1"
TEST_KEY = bytes(range(32))


def _manifest(**kw):
    base = {
        "agent": "claude",
        "version": "1.0.0",
        "installed": True,
        "spawn": False,
        "resume": False,
        "native_transcript": False,
        "structured_stream": False,
        "hooks": False,
        "pty": False,
        "supported_event_kinds": [],
        "quality_by_kind": {},
        "diagnostics": [],
    }
    base.update(kw)
    return base


class BridgeBase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="fleet-bridge-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.spool = LocalSpool(
            self.dir / "spool", MACH, SESS, key=TEST_KEY,
            max_session_bytes=1 << 20)
        self.addCleanup(self._close_spool)

    def _close_spool(self):
        try:
            self.spool.close()
        except Exception:
            pass

    def _config(self, **kw):
        return {
            "session_id": SESS,
            "machine_id": MACH,
            "stream_id": STREAM,
            "spool": self.spool,
            **kw,
        }

    def _bridge(self, manifest=None, **kw):
        manifest = manifest or _manifest(structured_stream=True)
        return SessionBridge.open(
            manifest, self._config(**kw))

    def _struct_manifest(self):
        return _manifest(
            structured_stream=True,
            quality_by_kind={
                "session_start": ("structured",),
                "user_message": ("structured",),
                "assistant_message": ("structured",),
                "tool_call": ("structured",),
                "tool_result": ("structured",),
            })


class SourceSelectionTests(BridgeBase):
    def test_adapter_selected_from_manifest_only(self):
        bridge = self._bridge(self._struct_manifest())
        self.assertIn("structured_stream", bridge.sources)
        self.assertIn(bridge.source, bridge.sources)

    def test_unstructured_manifest_selects_no_structured_source(self):
        bridge = self._bridge(_manifest(hooks=True))
        self.assertEqual(bridge.sources, ["hooks"])
        self.assertEqual(bridge.source, "hooks")

    def test_non_managed_is_best_effort_no_control(self):
        bridge = self._bridge(_manifest(managed=False, process_group_id="grp"))
        self.assertFalse(bridge.managed)
        self.assertEqual(bridge.capture_quality, "best_effort")
        self.assertEqual(bridge.control_capability, "unavailable")

    def test_pty_source_only_when_capability(self):
        bridge = self._bridge(_manifest(pty=True))
        self.assertEqual(bridge.sources, ["pty"])
        self.assertEqual(bridge.capture_quality, "best_effort")


class UnmanagedClampTests(BridgeBase):
    """Review R1: unmanaged sessions must NEVER durably emit structured/exact."""

    def _raw_full_event(self, quality="exact", event_id="evt_abc_1234"):
        return {
            "schema_version": 1,
            "event_id": event_id,
            "stream_id": STREAM,
            "machine_id": MACH,
            "session_id": SESS,
            "sequence": 7,
            "kind": "assistant_message",
            "capture_quality": quality,
            "emitted_at": "2026-08-26T00:00:00Z",
            "payload": {"text": "full structured text", "is_complete": True},
        }

    def test_unmanaged_full_exact_event_is_clamped_to_best_effort(self):
        bridge = self._bridge(self._struct_manifest(), managed=False)
        bridge.ingest_one(self._raw_full_event("exact"), source="structured_stream")
        replay = self.spool.read_after(0, 100, 1 << 18)
        # the full exact event is durably clamped to best_effort
        self.assertTrue(replay)
        self.assertNotIn("exact", [e.get("capture_quality")
                                   for e in replay])
        for e in replay:
            self.assertEqual(e["capture_quality"], "best_effort")

    def test_unmanaged_hook_event_is_clamped(self):
        bridge = self._bridge(_manifest(hooks=True), managed=False)
        bridge.ingest_one({
            "type": "PostToolUse",
            "tool_name": "Bash",
            "tool_use_id": "u_123",
            "tool_response": {"output": "ok"},
        }, source="hooks")
        replay = self.spool.read_after(0, 100, 1 << 18)
        self.assertTrue(replay)
        for e in replay:
            self.assertEqual(e["capture_quality"], "best_effort")

    def test_unmanaged_clamp_still_preserves_content(self):
        bridge = self._bridge(self._struct_manifest(), managed=False)
        bridge.ingest_structured([json.dumps({
            "kind": "user_message", "text": "keep this text",
        })])
        replay = self.spool.read_after(0, 100, 1 << 18)
        self.assertTrue(replay)
        payloads = json.dumps(replay)
        self.assertIn("keep this text", payloads)
        for e in replay:
            self.assertEqual(e["capture_quality"], "best_effort")

    def test_managed_session_keeps_structured(self):
        bridge = self._bridge(self._struct_manifest(),
                              managed=True, process_group_id="grp_1")
        bridge.ingest_structured([json.dumps({
            "kind": "user_message", "text": "managed structured",
        })])
        replay = self.spool.read_after(0, 100, 1 << 18)
        self.assertTrue(replay)
        self.assertEqual(replay[0]["capture_quality"], "structured")

    def test_unmanaged_source_control_event_is_rejected_not_persisted(self):
        """Review R2: an unmanaged source-injected capture_gap / quality-change
        can carry a top-level exact/structured quality; it must never reach the
        durable spool.  The bridge's OWN control events bypass this gate."""
        bridge = self._bridge(self._struct_manifest(), managed=False)
        bridge.ingest_one({
            "kind": "capture_quality_changed",
            "capture_quality": "exact",
            "emitted_at": "2026-08-26T00:00:00Z",
            "payload": {"old_quality": "exact", "new_quality": "structured",
                        "reason": "spoofed"},
        }, source="structured_stream")
        bridge.ingest_one({
            "kind": "capture_gap",
            "capture_quality": "structured",
            "emitted_at": "2026-08-26T00:00:00Z",
            "payload": {"start_sequence": 1, "end_sequence": 1,
                        "quality": "best_effort", "reason": "spoofed"},
        }, source="structured_stream")
        replay = self.spool.read_after(0, 100, 1 << 18)
        kinds = [e["kind"] for e in replay]
        self.assertNotIn("capture_quality_changed", kinds)
        self.assertNotIn("capture_gap", kinds)
        # none of the rejected control events was persisted
        self.assertEqual(replay, [])

    def test_managed_bridge_generated_disconnect_gap_still_persists(self):
        """Review R2: bridge-GENERATED control events (disconnect quality drop
        + gap) persist with bounded graded semantics via the direct spool path,
        even though the same source-injected kinds are rejected on unmanaged."""
        bridge = self._bridge(_manifest(native_transcript=True),
                              managed=True, process_group_id="grp_gen")
        path = self.dir / "native_gen.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"kind": "user_message", "text": "ok"}\n')
            fh.write('{"kind": "user_message", "text": "trunc')  # disconnect
        bridge.ingest_native(str(path))
        replay = self.spool.read_after(0, 100, 1 << 18)
        kinds = [e["kind"] for e in replay]
        self.assertIn("capture_quality_changed", kinds)
        self.assertIn("capture_gap", kinds)


class AdoptBestEffortClampTests(BridgeBase):
    """Task 8: an adopt-shaped config forces ``best_effort`` while managed.

    The ``best_effort: True`` config switch keeps ``capture_quality ==
    "best_effort"`` on the *managed* path (the native transcript is still
    retained, redaction + upload unchanged), while ``control_capability``
    stays ``available`` (the dashboard governance contract).
    """

    def test_managed_best_effort_config_reports_best_effort(self):
        bridge = self._bridge(
            _manifest(native_transcript=True),
            managed=True, process_group_id="grp_opaqueadopt",
            best_effort=True)
        self.assertTrue(bridge.managed)
        self.assertEqual(bridge.capture_quality, "best_effort")
        self.assertEqual(bridge.control_capability, "available")

    def test_managed_best_effort_ingested_events_stay_best_effort(self):
        bridge = self._bridge(
            self._struct_manifest(),
            managed=True, process_group_id="grp_opaqueadopt",
            best_effort=True)
        bridge.ingest_structured([json.dumps({
            "kind": "assistant_message", "text": "best-effort managed",
        })])
        replay = self.spool.read_after(0, 100, 1 << 18)
        self.assertTrue(replay)
        for event in replay:
            self.assertEqual(event["capture_quality"], "best_effort")


class PassthroughBeforeAppendTests(BridgeBase):
    """Live ingest keeps agent text; schema allowlist still bounds the envelope."""

    def test_spool_passthrough_keeps_agent_text(self):
        bridge = self._bridge(self._struct_manifest())
        bridge.ingest_structured([json.dumps({
            "kind": "user_message",
            "text": "Bearer hunter2-secret-value",
        })])
        replay = self.spool.read_after(0, 100, 1 << 18)
        flat = json.dumps(replay)
        self.assertIn("hunter2-secret-value", flat)
        self.assertNotIn("[REDACTED", flat)

    def test_passthrough_event_keeps_secret_shaped_text(self):
        bridge = self._bridge(self._struct_manifest())
        secret = "sk-ant-abcdefghijklmnopqrstuvwxyz123456"
        bridge.ingest_structured([json.dumps({
            "kind": "user_message",
            "text": "my api key is " + secret,
        })])
        replay = self.spool.read_after(0, 100, 1 << 18)
        self.assertTrue(replay)
        self.assertIn(secret, json.dumps(replay))


class SourceDisconnectTests(BridgeBase):
    """Review R1: source disconnect/truncation yields stable gap/quality."""

    def test_native_disconnect_emits_bounded_gap_and_quality(self):
        # managed structured native source: disconnect degrades to best_effort
        bridge = self._bridge(_manifest(native_transcript=True),
                              managed=True, process_group_id="grp_disc")
        path = self.dir / "native.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"kind": "user_message", "text": "one"}\n')
            fh.write('{"kind": "user_message", "text": "two')  # truncated tail
        bridge.ingest_native(str(path))
        replay = self.spool.read_after(0, 100, 1 << 18)
        kinds = [e["kind"] for e in replay]
        self.assertIn("user_message", kinds)
        self.assertIn("capture_quality_changed", kinds)
        self.assertIn("capture_gap", kinds)
        for e in replay:
            self.assertNotIn("Traceback", json.dumps(e))
            self.assertNotIn("/private", json.dumps(e))

    def test_native_io_failure_emits_bounded_gap(self):
        bridge = self._bridge(_manifest(native_transcript=True),
                              managed=True, process_group_id="grp_disc")
        path = self.dir / "missing.jsonl"  # does not exist
        bridge.ingest_native(str(path))
        replay = self.spool.read_after(0, 100, 1 << 18)
        self.assertTrue(any(e["kind"] in ("capture_gap", "capture_quality_changed")
                            for e in replay))

    def test_raw_secret_shaped_event_id_replaced(self):
        bridge = self._bridge(self._struct_manifest())
        bridge.ingest_structured([json.dumps({
            "kind": "user_message",
            "text": "hello",
            "event_id": "secret-hunter2-token",
        })])
        replay = self.spool.read_after(0, 100, 1 << 18)
        self.assertTrue(replay)
        flat = json.dumps(replay)
        self.assertNotIn("secret-hunter2-token", flat)
        # the durable event_id is a bridge-generated opaque id (event payload
        # text is preserved)
        self.assertIn("hello", flat)
        # the durable event id is the generated opaque form
        self.assertTrue(replay[0]["event_id"].startswith("evt_"))

    def test_jwt_shaped_event_id_replaced(self):
        """Review R2: a JWT-shaped id (eyJ... dotted segments) must never reach
        the durable record; it is replaced by a generated opaque id."""
        bridge = self._bridge(self._struct_manifest())
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIiwiZXhwIjoxNzUwMH0.sigval"
        bridge.ingest_structured([json.dumps({
            "kind": "user_message", "text": "jwt test", "event_id": jwt,
        })])
        replay = self.spool.read_after(0, 100, 1 << 18)
        self.assertTrue(replay)
        flat = json.dumps(replay)
        self.assertNotIn("eyJhbGci", flat)
        self.assertNotIn(".eyJzdWI", flat)
        self.assertNotIn("sigval", flat)
        self.assertTrue(replay[0]["event_id"].startswith("evt_"))

    def test_session_cookie_shaped_event_id_replaced(self):
        """Review R2: a session-token/cookie-shaped id (base64 with = or %%XX)
        must never reach the durable record."""
        bridge = self._bridge(self._struct_manifest())
        for bad_id in ("session=abc123%2Btoken%3D", "sid=Zm9vYmFyK3h5eg==",
                       "12_cookie_abc==", "a=b"):
            bridge.ingest_structured([json.dumps({
                "kind": "user_message", "text": "tok", "event_id": bad_id,
            })])
        replay = self.spool.read_after(0, 100, 1 << 18)
        self.assertTrue(replay)
        flat = json.dumps(replay)
        for marker in ("session=abc", "sid=", "Zm9vYmFy", "cookie",
                       "b=", "a=b"):
            self.assertNotIn(marker, flat)
        for e in replay:
            self.assertTrue(e["event_id"].startswith("evt_"))


class SourceFailureIsolationTests(BridgeBase):
    def test_structured_source_error_emits_bounded_gap(self):
        bridge = self._bridge(self._struct_manifest())
        # a failing normalize (envelope invalid) produces no event; the
        # caller surfaces a bounded gap via the helper
        bridge.ingest("this is not json")
        replay = self.spool.read_after(0, 100, 1 << 16)
        # the noise is rejected and NOT appended; a gap is not automatically
        # emitted for one malformed record (malformed records affect only
        # themselves).  The gap helper is validated separately.
        self.assertEqual([e["kind"] for e in replay], [])

    def test_capture_gap_helper_is_valid_and_bounded(self):
        base = {
            "schema_version": 1,
            "event_id": "e", "stream_id": STREAM, "machine_id": MACH,
            "session_id": SESS, "sequence": 5, "kind": "session_start",
            "capture_quality": "structured", "emitted_at": "2026-08-26T00:00:00Z",
            "payload": {},
        }
        gap = build_capture_gap_event(
            base, start_sequence=3, end_sequence=4, reason="source_unavailable")
        clean = validate_event(gap)
        self.assertEqual(clean["kind"], "capture_gap")
        self.assertEqual(clean["payload"]["quality"], "best_effort")

    def test_quality_downgrade_helper(self):
        base = {
            "schema_version": 1,
            "event_id": "e", "stream_id": STREAM, "machine_id": MACH,
            "session_id": SESS, "sequence": 5, "kind": "capture_quality_changed",
            "capture_quality": "structured", "emitted_at": "2026-08-26T00:00:00Z",
            "payload": {},
        }
        evt = build_capture_quality_event(
            base, old_quality="exact", new_quality="structured",
            reason="native ended")
        clean = validate_event(evt)
        self.assertEqual(clean["kind"], "capture_quality_changed")

    def test_spool_error_is_isolated_never_escapes_ingest(self):
        """A SpoolError (e.g. unwritable checkpoint) degrades to a bounded
        per-event reject and never escapes ingest into the source loop."""
        from tools.session.spool import SpoolError

        bridge = self._bridge(self._struct_manifest())
        original = type(self.spool).append

        def boom(self, event):
            raise SpoolError("checkpoint_corrupt")

        type(self.spool).append = boom
        try:
            # Must not raise — the SpoolError is contained.
            bridge.ingest_structured(
                [json.dumps({"kind": "assistant_message", "text": "hello"})])
        finally:
            type(self.spool).append = original
        # The bounded degrade is recorded as a stable code, not propagated.
        self.assertEqual(bridge._last_reject, "checkpoint_corrupt")


class UnmanagedSemanticsTests(BridgeBase):
    def test_non_managed_capture_quality_best_effort(self):
        bridge = self._bridge(self._struct_manifest(), managed=False)
        self.assertEqual(bridge.capture_quality, "best_effort")
        self.assertEqual(bridge.control_capability, "unavailable")


class FakeSourceAndUploadTests(BridgeBase):
    def test_fake_source_round_trip_and_upload(self):
        calls = []
        bridge = self._bridge(
            self._struct_manifest(),
            post_json=lambda payload: calls.append(payload) or {
                "ok": True, "stream_id": STREAM,
                "accepted_through": 1, "next_cursor": "c1",
                "rejected": [],
            })
        bridge.ingest_structured([json.dumps({"kind": "assistant_message",
                                              "text": "hello"})])
        acked = bridge.flush()
        self.assertEqual(acked, 1)
        self.assertTrue(calls)
        self.assertEqual(calls[0]["events"][0]["kind"], "assistant_message")

    def test_ingest_response_contract_via_cursor(self):
        responses = []
        bridge = self._bridge(
            self._struct_manifest(),
            post_json=lambda payload: responses.append(payload) or {
                "ok": True, "status": "accepted",
                "accepted_through": 1, "next_cursor": "cursor-x",
                "rejected": [],
            })
        bridge.ingest_structured([json.dumps({"kind": "user_message",
                                              "text": "one"})])
        bridge.flush()
        # next_cursor surfaced by the injected transport
        self.assertEqual(bridge.uploader.next_cursor, "cursor-x")
        self.assertEqual(bridge.spool.status()["ack_sequence"], 1)


class HubHttpRoundtripTests(unittest.TestCase):
    """A fake source feeds the bridge; the uploader posts real Hub HTTP."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="fleet-hub-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        from hub.bootstrap import create_app
        from hub.config import FleetConfig

        root = self.dir / "root"
        root.mkdir()
        config = FleetConfig.from_root(
            root,
            ingest_token="ingest-secret",
            dev_operator="op@example.com",
            runner_credentials=None,
            project_whitelist={"host-bridge": ["agent-fleet"]},
            session_repositories_enabled=True,
            session_db=root / "var" / "sessions" / "meta.db",
            session_transcript_root=root / "var" / "sessions" / "transcripts",
            session_encryption_raw=bytes(range(32)),
        )
        self.app = create_app(config)
        self.client = self.app.test_client()

        self.spool = LocalSpool(
            self.dir / "spool", MACH, SESS, key=bytes(range(32)),
            max_session_bytes=1 << 20)

    def test_hub_http_roundtrip(self):
        bridge = SessionBridge.open(
            _manifest(structured_stream=True),
            {"session_id": SESS, "machine_id": MACH, "stream_id": STREAM,
             "spool": self.spool})

        def post_json(payload):
            resp = self.client.post(
                "/api/session-events",
                json=payload["events"],
                headers={"X-Agent-Fleet-Token": "ingest-secret"})
            return resp.get_json()

        bridge.ingest_structured([json.dumps({"kind": "user_message",
                                              "text": "http round trip"})])
        uploader = SessionUploader(post_json, spool=self.spool)
        acked = uploader.flush_once()
        self.assertGreaterEqual(acked, 1)
        self.assertEqual(self.spool.status()["ack_sequence"],
                         self.spool.status()["last_sequence"])


if __name__ == "__main__":
    unittest.main()

class EnvelopeBindingTests(BridgeBase):
    """Adopt bridges must bind the supervisor process group on the envelope.

    The Hub derives ``managed`` from the event's ``process_group_id``
    (``hub/application/session_service.py``); a managed bridge that omits it
    degrades the whole session to unmanaged server-side.
    """

    def test_managed_bridge_events_carry_process_group_id(self):
        bridge = self._bridge(self._struct_manifest(), managed=True,
                              process_group_id="grp_env_test")
        bridge.ingest_structured([json.dumps({"kind": "user_message",
                                              "text": "hi"})])
        rows = bridge.spool.read_after(0, 10, 1 << 20)
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row.get("process_group_id"), "grp_env_test")

    def test_standalone_bridge_omits_process_group_id(self):
        bridge = self._bridge(self._struct_manifest(), managed=False)
        bridge.ingest_structured([json.dumps({"kind": "user_message",
                                              "text": "hi"})])
        rows = bridge.spool.read_after(0, 10, 1 << 20)
        self.assertTrue(rows)
        for row in rows:
            self.assertIsNone(row.get("process_group_id"))


class NativeTranscriptShapeTests(BridgeBase):
    """claude_code native JSONL rows (type user/assistant) must map to
    content events; before the fix every row was dropped so adopted
    sessions only ever surfaced ``session_start``."""

    def _native_bridge(self, **kw):
        manifest = _manifest(native_transcript=True)
        return SessionBridge.open(
            manifest, self._config(managed=True,
                                   process_group_id="grp_native",
                                   **kw))

    def _tail_file(self, tmp, rows):
        path = tmp / "native.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        return path

    def test_user_assistant_rows_map_to_message_events(self):
        import tempfile as _tf
        tmp = Path(_tf.mkdtemp(prefix="fleet-native-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bridge = self._native_bridge()
        path = self._tail_file(tmp, [
            {"type": "user", "message": {"role": "user",
                                         "content": "hello fleet"}},
            {"type": "assistant", "message": {"role": "assistant",
             "content": [{"type": "thinking", "thinking": "internal"},
                         {"type": "text", "text": "hi there"}]}},
        ])
        self.assertTrue(bridge.ingest_native(str(path)) > 0)
        rows = bridge.spool.read_after(0, 20, 1 << 20)
        kinds = [r.get("kind") for r in rows]
        self.assertIn("user_message", kinds)
        self.assertIn("assistant_message", kinds)
        assistant = next(r for r in rows if r["kind"] == "assistant_message")
        # thinking blocks never leak into the message body
        self.assertEqual(assistant["payload"]["text"], "hi there")

    def test_tool_use_and_result_blocks_map_to_tool_events(self):
        import tempfile as _tf
        tmp = Path(_tf.mkdtemp(prefix="fleet-native-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bridge = self._native_bridge()
        path = self._tail_file(tmp, [
            {"type": "assistant", "message": {"role": "assistant",
             "content": [{"type": "tool_use", "id": "toolu_1",
                          "name": "Bash",
                          "input": {"command": "ls"}}]}},
            {"type": "user", "message": {"role": "user",
             "content": [{"type": "tool_result", "tool_use_id": "toolu_1",
                          "content": "file.txt"}]}},
        ])
        bridge.ingest_native(str(path))
        rows = bridge.spool.read_after(0, 20, 1 << 20)
        call = next(r for r in rows if r["kind"] == "tool_call")
        result = next(r for r in rows if r["kind"] == "tool_result")
        self.assertEqual(call["payload"]["tool_name"], "Bash")
        self.assertEqual(call["payload"]["call_id"], "toolu_1")
        self.assertIn("ls", call["payload"]["arguments"])
        self.assertEqual(result["payload"]["call_id"], "toolu_1")
        self.assertIn("file.txt", result["payload"]["result"])

    def test_tool_result_image_content_keeps_result_empty(self):
        """Claude native tool_result blocks with non-text content (image
        payloads) keep result empty — real transcripts carry 460-680KB
        base64 blobs per Read-of-screenshot, and _bounded_json would dump
        them into the durable record."""
        import tempfile as _tf
        tmp = Path(_tf.mkdtemp(prefix="fleet-native-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bridge = self._native_bridge()
        path = self._tail_file(tmp, [
            {"type": "user", "message": {"role": "user",
             "content": [{"type": "tool_result", "tool_use_id": "toolu_img",
                          "content": [{"type": "image", "data": "aGVsbG8=",
                                       "mimeType": "image/png"}]}]}},
            {"type": "user", "message": {"role": "user",
             "content": [{"type": "tool_result", "tool_use_id": "toolu_mix",
                          "content": [{"type": "image", "data": "aGVsbG8=",
                                       "mimeType": "image/png"},
                                      {"type": "text", "text": "see shot"}]}]}},
        ])
        bridge.ingest_native(str(path))
        rows = bridge.spool.read_after(0, 20, 1 << 20)
        results = {r["payload"]["call_id"]: r["payload"]["result"]
                   for r in rows if r["kind"] == "tool_result"}
        self.assertEqual(results["toolu_img"], "")
        self.assertEqual(results["toolu_mix"], "see shot")
        self.assertNotIn("aGVsbG8=", json.dumps(rows))

    def test_unknown_native_row_types_are_dropped(self):
        import tempfile as _tf
        tmp = Path(_tf.mkdtemp(prefix="fleet-native-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bridge = self._native_bridge()
        path = self._tail_file(tmp, [
            {"type": "attachment", "data": "x"},
            {"type": "file-history-snapshot", "v": 1},
            {"type": "user", "message": {"role": "user",
                                         "content": "keep me"}},
        ])
        self.assertTrue(bridge.ingest_native(str(path)) > 0)
        rows = bridge.spool.read_after(0, 20, 1 << 20)
        kinds = [r.get("kind") for r in rows]
        self.assertEqual([k for k in kinds if k != "session_start"],
                         ["user_message"])


class CodexRolloutShapeTests(BridgeBase):
    """codex rollout JSONL rows ({type: event_msg|response_item, payload})
    must map onto content events; before the fix only claude's flat
    user/assistant rows were recognized so codex sessions surfaced only
    session_start."""

    def _native_bridge(self, **kw):
        manifest = _manifest(native_transcript=True)
        return SessionBridge.open(
            manifest, self._config(managed=True,
                                   process_group_id="grp_codex",
                                   **kw))

    def _tail_file(self, tmp, rows):
        path = tmp / "rollout.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        return path

    def test_event_msg_user_and_agent_messages_map(self):
        import tempfile as _tf
        tmp = Path(_tf.mkdtemp(prefix="fleet-codex-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bridge = self._native_bridge()
        path = self._tail_file(tmp, [
            {"type": "session_meta", "payload": {"id": "t"}},
            {"type": "event_msg", "payload": {"type": "user_message",
                                              "message": "继续"}},
            {"type": "event_msg", "payload": {"type": "agent_message",
                                              "message": "done"}},
        ])
        self.assertTrue(bridge.ingest_native(str(path)) > 0)
        rows = bridge.spool.read_after(0, 20, 1 << 20)
        kinds = [r.get("kind") for r in rows]
        self.assertIn("user_message", kinds)
        self.assertIn("assistant_message", kinds)
        user = next(r for r in rows if r["kind"] == "user_message")
        self.assertEqual(user["payload"]["text"], "继续")

    def test_function_and_custom_tool_calls_map(self):
        import tempfile as _tf
        tmp = Path(_tf.mkdtemp(prefix="fleet-codex-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bridge = self._native_bridge()
        path = self._tail_file(tmp, [
            {"type": "response_item", "payload": {"type": "function_call",
             "name": "request_user_input", "arguments": "{}",
             "call_id": "call_1"}},
            {"type": "response_item", "payload": {
             "type": "function_call_output", "call_id": "call_1",
             "output": "root only"}},
            {"type": "response_item", "payload": {"type": "custom_tool_call",
             "name": "exec", "input": "tools.exec_command({})",
             "call_id": "call_2"}},
            {"type": "response_item", "payload": {
             "type": "custom_tool_call_output", "call_id": "call_2",
             "output": [{"type": "input_text", "text": "ok"}]}},
        ])
        bridge.ingest_native(str(path))
        rows = bridge.spool.read_after(0, 20, 1 << 20)
        calls = [r for r in rows if r["kind"] == "tool_call"]
        results = [r for r in rows if r["kind"] == "tool_result"]
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(results), 2)
        exec_call = next(c for c in calls if c["payload"]["tool_name"] == "exec")
        self.assertEqual(exec_call["payload"]["call_id"], "call_2")
        self.assertIn("exec_command", exec_call["payload"]["arguments"])
        self.assertIn("ok", results[1]["payload"]["result"])

    def test_mcp_tool_call_end_maps_to_tool_result(self):
        import tempfile as _tf
        tmp = Path(_tf.mkdtemp(prefix="fleet-codex-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bridge = self._native_bridge()
        path = self._tail_file(tmp, [
            {"type": "event_msg", "payload": {"type": "mcp_tool_call_end",
             "call_id": "exec-1",
             "invocation": {"server": "codex_app", "tool": "list_projects",
                            "arguments": {}},
             "result": {"Ok": {"content": [{"type": "text", "text": "{}"}]}}},
             "duration": {"secs": 0, "nanos": 1}},
        ])
        bridge.ingest_native(str(path))
        rows = bridge.spool.read_after(0, 20, 1 << 20)
        result = next(r for r in rows if r["kind"] == "tool_result")
        self.assertEqual(result["payload"]["tool_name"], "list_projects")
        self.assertEqual(result["payload"]["call_id"], "exec-1")
        self.assertEqual(result["payload"]["status"], "ok")

    def test_internal_codex_rows_are_dropped(self):
        import tempfile as _tf
        tmp = Path(_tf.mkdtemp(prefix="fleet-codex-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bridge = self._native_bridge()
        path = self._tail_file(tmp, [
            {"type": "event_msg", "payload": {"type": "token_count",
                                              "info": {}}},
            {"type": "response_item", "payload": {"type": "reasoning",
                                                  "summary": []}},
            {"type": "response_item", "payload": {"type": "message",
             "role": "assistant", "content": [{"type": "output_text",
                                               "text": "dup"}]}},
            {"type": "turn_context", "payload": {"cwd": "/x"}},
            {"type": "event_msg", "payload": {"type": "task_started"}},
        ])
        count = bridge.ingest_native(str(path))
        rows = bridge.spool.read_after(0, 20, 1 << 20)
        kinds = [r.get("kind") for r in rows if r.get("kind") != "session_start"]
        self.assertEqual(count, 0)
        self.assertEqual(kinds, [])


class PiTranscriptShapeTests(BridgeBase):
    """pi transcript JSONL rows ({type: message, message: {role, content}})
    must map onto content events; pi's toolResult is its own ROLE (with
    top-level toolName/toolCallId/isError) and toolCall blocks carry dict
    arguments — a third envelope shape alongside claude and codex."""

    def _native_bridge(self, **kw):
        manifest = _manifest(native_transcript=True)
        return SessionBridge.open(
            manifest, self._config(managed=True,
                                   process_group_id="grp_pi",
                                   **kw))

    def _tail_file(self, tmp, rows):
        path = tmp / "pi.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        return path

    def test_user_and_assistant_messages_map(self):
        import tempfile as _tf
        tmp = Path(_tf.mkdtemp(prefix="fleet-pi-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bridge = self._native_bridge()
        path = self._tail_file(tmp, [
            {"type": "session", "cwd": "/x", "id": "s1",
             "timestamp": "2026-09-01T00:00:00Z", "version": "0.84.4"},
            {"type": "message", "message": {"role": "user",
                                            "content": "继续"}},
            {"type": "message", "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "internal"},
                {"type": "text", "text": "done"}]}},
            {"type": "message", "message": {"role": "assistant",
                                            "content": []}},
        ])
        self.assertTrue(bridge.ingest_native(str(path)) > 0)
        rows = bridge.spool.read_after(0, 20, 1 << 20)
        kinds = [r.get("kind") for r in rows]
        self.assertIn("user_message", kinds)
        self.assertIn("assistant_message", kinds)
        user = next(r for r in rows if r["kind"] == "user_message")
        self.assertEqual(user["payload"]["text"], "继续")
        assistant = next(r for r in rows if r["kind"] == "assistant_message")
        self.assertEqual(assistant["payload"]["text"], "done")
        # the empty assistant row yields nothing; thinking never leaks
        self.assertEqual([k for k in kinds if k != "session_start"],
                         ["user_message", "assistant_message"])

    def test_tool_call_and_tool_result_roles_map(self):
        import tempfile as _tf
        tmp = Path(_tf.mkdtemp(prefix="fleet-pi-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bridge = self._native_bridge()
        path = self._tail_file(tmp, [
            {"type": "message", "message": {"role": "assistant", "content": [
                {"type": "toolCall", "id": "tc_1", "name": "read",
                 "arguments": {"target": "notes file"}}]}},
            {"type": "message", "message": {"role": "toolResult",
             "toolName": "read", "toolCallId": "tc_1", "isError": False,
             "content": [{"type": "text", "text": "file body"}]}},
            {"type": "message", "message": {"role": "toolResult",
             "toolName": "bash", "toolCallId": "tc_2", "isError": True,
             "content": [{"type": "text", "text": "boom"}]}},
        ])
        bridge.ingest_native(str(path))
        rows = bridge.spool.read_after(0, 20, 1 << 20)
        call = next(r for r in rows if r["kind"] == "tool_call")
        ok = next(r for r in rows if r["kind"] == "tool_result"
                  and r["payload"]["call_id"] == "tc_1")
        err = next(r for r in rows if r["kind"] == "tool_result"
                   and r["payload"]["call_id"] == "tc_2")
        self.assertEqual(call["payload"]["tool_name"], "read")
        self.assertEqual(call["payload"]["call_id"], "tc_1")
        self.assertIn("notes file", call["payload"]["arguments"])
        self.assertEqual(ok["payload"]["tool_name"], "read")
        self.assertEqual(ok["payload"]["status"], "ok")
        self.assertIn("file body", ok["payload"]["result"])
        self.assertEqual(err["payload"]["status"], "error")

    def test_non_message_envelopes_are_dropped(self):
        import tempfile as _tf
        tmp = Path(_tf.mkdtemp(prefix="fleet-pi-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bridge = self._native_bridge()
        path = self._tail_file(tmp, [
            {"type": "model_change", "model": "m"},
            {"type": "compaction", "summary": "x"},
            {"type": "session", "cwd": "/x", "id": "s1"},
        ])
        count = bridge.ingest_native(str(path))
        rows = bridge.spool.read_after(0, 20, 1 << 20)
        kinds = [r.get("kind") for r in rows if r.get("kind") != "session_start"]
        self.assertEqual(count, 0)
        self.assertEqual(kinds, [])

    def test_text_plus_tool_call_row_maps_to_both(self):
        """A pi assistant row mixing text and toolCall blocks maps onto BOTH
        a message event (the narration) and one tool_call per block, in row
        order — the dominant real-world shape (~31% of observed assistant
        rows); before multi-event shaping the text was silently dropped."""
        import tempfile as _tf
        tmp = Path(_tf.mkdtemp(prefix="fleet-pi-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bridge = self._native_bridge()
        path = self._tail_file(tmp, [
            {"type": "message", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "running tool"},
                {"type": "toolCall", "id": "tc_1", "name": "read",
                 "arguments": {"target": "a"}},
                {"type": "text", "text": "now bash"},
                {"type": "toolCall", "id": "tc_2", "name": "bash",
                 "arguments": {"command": "ls"}},
            ]}},
        ])
        self.assertTrue(bridge.ingest_native(str(path)) > 0)
        rows = bridge.spool.read_after(0, 20, 1 << 20)
        shaped = [(r["kind"], r["payload"]) for r in rows
                  if r["kind"] != "session_start"]
        self.assertEqual([k for k, _ in shaped],
                         ["assistant_message", "tool_call",
                          "assistant_message", "tool_call"])
        self.assertEqual(shaped[0][1]["text"], "running tool")
        self.assertEqual(shaped[1][1]["call_id"], "tc_1")
        self.assertEqual(shaped[2][1]["text"], "now bash")
        self.assertEqual(shaped[3][1]["call_id"], "tc_2")

    def test_multiple_tool_calls_in_one_row_all_map(self):
        """Every toolCall block in one pi row maps to its own tool_call —
        real sessions emit 2-6 calls per row; only the first was captured."""
        import tempfile as _tf
        tmp = Path(_tf.mkdtemp(prefix="fleet-pi-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bridge = self._native_bridge()
        path = self._tail_file(tmp, [
            {"type": "message", "message": {"role": "assistant", "content": [
                {"type": "toolCall", "id": "c1", "name": "bash",
                 "arguments": {}},
                {"type": "toolCall", "id": "c2", "name": "read",
                 "arguments": {}},
                {"type": "toolCall", "id": "c3", "name": "bash",
                 "arguments": {}},
            ]}},
        ])
        bridge.ingest_native(str(path))
        rows = bridge.spool.read_after(0, 20, 1 << 20)
        calls = [r["payload"]["call_id"] for r in rows
                 if r["kind"] == "tool_call"]
        self.assertEqual(calls, ["c1", "c2", "c3"])

    def test_tool_result_non_text_content_stays_empty(self):
        """A toolResult whose content carries no text block (e.g. image
        payloads) keeps result empty instead of dumping the base64 blob
        into the durable record."""
        import tempfile as _tf
        tmp = Path(_tf.mkdtemp(prefix="fleet-pi-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bridge = self._native_bridge()
        path = self._tail_file(tmp, [
            {"type": "message", "message": {"role": "toolResult",
             "toolName": "shot", "toolCallId": "tc_9", "isError": False,
             "content": [{"type": "image", "data": "aGVsbG8=",
                          "mimeType": "image/png"}]}},
        ])
        bridge.ingest_native(str(path))
        rows = bridge.spool.read_after(0, 20, 1 << 20)
        result = next(r for r in rows if r["kind"] == "tool_result")
        self.assertEqual(result["payload"]["result"], "")
        self.assertNotIn("aGVsbG8=", json.dumps(rows))
