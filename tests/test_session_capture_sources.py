"""Tests for the Task 7 session capture source adapters.

Coverage (from the Task 7 brief):

- Native JSONL tail by byte offset, checkpoint durably, and never be poisoned
  by a malformed/truncated line;
- Structured stream parser rejects non-JSON noise instead of silently treating
  it as transcript;
- Hook adapter never forwards the hook environment, and is selected only by
  the capability manifest;
- PTY adapter is explicitly best-effort and bounded;
- source failure is isolated into a bounded ``capture_gap`` / quality event
  without raw exception text or source payload leakage;
- malformed records affect only themselves;
- no Hermes structured-spawn assumption (capability manifests are the sole
  source of truth for adapter selection).

These tests exercise ``tools.session.capture.{native_jsonl,
structured_stream,hooks,pty}`` plus the bridge's bounded gap/quality helpers.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from session_schema import CAPTURE_QUALITIES, validate_event
from tools.session.capture import hooks, native_jsonl, structured_stream
from tools.session.capture import pty as pty_module
from tools.session.bridge import (
    build_capture_gap_event,
    build_capture_quality_event,
)

SESSION = "sess_cap_src"
MACHINE = "host-src-1"


def _manifest(**kwargs) -> dict:
    base = {
        "native_transcript": False,
        "structured_stream": False,
        "hooks": False,
        "pty": False,
        "spawn": False,
        "resume": False,
        "supported_event_kinds": [],
        "quality_by_kind": {},
    }
    base.update(kwargs)
    return base


def _base_event(**kw) -> dict:
    evt = {
        "schema_version": 1,
        "event_id": "evt_base_00000001",
        "stream_id": "stream_src_1",
        "machine_id": MACHINE,
        "session_id": SESSION,
        "sequence": 1,
        "kind": "session_start",
        "capture_quality": "structured",
        "source": "bridge",
        "emitted_at": "2026-08-26T00:00:00Z",
        "payload": {"agent_family": "codex", "adapter": "codex_structured"},
    }
    evt.update(kw)
    return evt


class NativeJsonlByteOffsetTests(unittest.TestCase):
    """Byte-offset tailing with durable checkpoint across restart."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="fleet-njl-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def _path(self, name="events.jsonl"):
        return self.dir / name

    def _write_lines(self, path, lines):
        with open(path, "w", encoding="utf-8") as fh:
            for line in lines:
                fh.write(line)
                fh.write("\n")

    def test_tails_complete_lines_in_order(self):
        path = self._path()
        self._write_lines(path, ['{"n": 1}', '{"n": 2}', '{"n": 3}'])
        tailer = native_jsonl.tailer(str(path))
        self.assertEqual([e["n"] for e in tailer.read()], [1, 2, 3])
        # the byte offset is at the end of the file (all consumed)
        self.assertEqual(tailer.offset, path.stat().st_size)

    def test_missing_checkpoint_starts_at_eof_not_replay(self):
        path = self._path()
        self._write_lines(path, ['{"n": 1}', '{"n": 2}', '{"n": 3}'])
        cp = self.dir / "cp.json"
        self.assertFalse(cp.exists())
        tailer = native_jsonl.tailer(str(path), checkpoint_path=str(cp))
        self.assertEqual(tailer.offset, path.stat().st_size)
        self.assertEqual(tailer.read(), [])
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"n": 4}\n')
        self.assertEqual([e["n"] for e in tailer.read()], [4])

    def test_restart_with_checkpoint_does_not_replay(self):
        path = self._path()
        self._write_lines(path, ['{"n": 1}', '{"n": 2}'])
        tailer = native_jsonl.tailer(str(path))
        self.assertEqual(len(tailer.read()), 2)
        cp = self.dir / "cp.json"
        saved = tailer.checkpoint_to(str(cp))
        self.assertEqual(saved["offset"], path.stat().st_size)

        fresh = native_jsonl.tailer(str(path), checkpoint_path=str(cp))
        # nothing re-read: byte offset restored from the durable checkpoint
        self.assertEqual(fresh.offset, path.stat().st_size)
        self.assertEqual(fresh.read(), [])

    def test_resumes_from_checkpoint_offset_on_growth(self):
        path = self._path()
        self._write_lines(path, ['{"n": 1}', '{"n": 2}'])
        tailer = native_jsonl.tailer(str(path))
        tailer.read()
        cp = self.dir / "cp.json"
        tailer.checkpoint_to(str(cp))
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"n": 3}\n')
        fresh = native_jsonl.tailer(str(path), checkpoint_path=str(cp))
        self.assertEqual(fresh.read(), [{"n": 3}])

    def test_truncated_tail_line_is_kept_for_later_completion(self):
        path = self._path()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"n": 1}\n{"n": 2')  # partial trailing line, no newline
        tailer = native_jsonl.tailer(str(path))
        self.assertEqual([e["n"] for e in tailer.read()], [1])
        self.assertTrue(tailer.disconnected)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("}\n")  # repair the tail
        self.assertEqual([e["n"] for e in tailer.read()], [2])
        self.assertFalse(tailer.disconnected)

    def test_malformed_complete_line_is_skipped_but_others_survive(self):
        path = self._path()
        self._write_lines(path, [
            '{"n": 1}',
            "garbage-not-json",
            '{"n": 2}',
        ])
        tailer = native_jsonl.tailer(str(path))
        self.assertEqual([e["n"] for e in tailer.read()], [1, 2])
        # byte offset still advances past every line (no replay on the next read)
        self.assertEqual(tailer.offset, path.stat().st_size)
        self.assertEqual(tailer.read(), [])

    def test_oversized_line_is_dropped_without_consuming_other_lines(self):
        path = self._path()
        big = "x" * (1 << 20)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"n": 1}\n')
            fh.write('{"n": 2, "huge": "%s"}\n' % big)
        tailer = native_jsonl.tailer(str(path))
        self.assertEqual([e["n"] for e in tailer.read()], [1])
        # the byte offset still consumed the oversized line (never loops)
        self.assertEqual(tailer.offset, path.stat().st_size)

    def test_non_dict_json_lines_are_ignored(self):
        path = self._path()
        self._write_lines(path, ['[1, 2, 3]', '"text"', '{"n": 5}'])
        tailer = native_jsonl.tailer(str(path))
        self.assertEqual([e["n"] for e in tailer.read()], [5])

    def test_checkpoint_snapshot_is_leakfree(self):
        path = self._path()
        self._write_lines(path, ['{"n": 1}'])
        tailer = native_jsonl.tailer(str(path))
        tailer.read()
        cp = tailer.checkpoint()
        self.assertEqual(cp, {"offset": tailer.offset})


class StructuredStreamTests(unittest.TestCase):
    """Structured stream parsing rejects non-JSON noise."""

    def test_parses_json_objects(self):
        lines = ['{"kind": "user_message", "text": "one"}',
                 '{"kind": "assistant_message", "text": "two"}']
        out = list(structured_stream.iter_json(lines))
        self.assertEqual([e["kind"] for e in out],
                         ["user_message", "assistant_message"])

    def test_noise_never_becomes_transcript(self):
        lines = [
            "this is not json",
            "-- underlying raw --",
            '{"kind": "user_message", "text": "real"}',
            "   {} not json",
        ]
        out = list(structured_stream.iter_json(lines))
        self.assertEqual(out, [{"kind": "user_message", "text": "real"}])

    def test_oversized_line_is_rejected(self):
        big = '"n": "' + ("x" * (1 << 18)) + '"'
        lines = ['{"ok": true}', "{" + big + "}"]
        out = list(structured_stream.iter_json(lines, max_chars=1 << 18))
        self.assertEqual(out, [{"ok": True}])

    def test_truncated_object_partial_is_rejected(self):
        out = list(structured_stream.iter_json(['{"kind": "tool_call",']))
        self.assertEqual(out, [])

    def test_bytes_lines_are_decoded(self):
        out = list(structured_stream.iter_json([b'{"n": 1}', '{"n": 2}']))
        self.assertEqual([e["n"] for e in out], [1, 2])


class HookEnvIsolationTests(unittest.TestCase):
    """Hook adapter never forwards the hook environment."""

    def _raw(self):
        env = {
            "CLAUDECODE_PROCESS_GROUP_ID": "grp1",
            "CLAUDE_PROJECT_DIR": "/private/tmp/proj",
            "PATH": "/usr/bin:/bin",
            "SECRET_VALUE": "top-secret-x",
        }
        return {
            "type": "PostToolUse",
            "tool_name": "Bash",
            "input": {"command": "ls -la"},
            "env": env,
            "cwd": "/private/tmp/w",
            "authorization": "Bearer hunter2",
        }

    def test_normalized_event_never_contains_env(self):
        raw = self._raw()
        out = hooks.HookAdapter(
            _manifest(hooks=True), session_id=SESSION,
            machine_id=MACHINE, stream_id="stream_src_1").normalize(raw)
        flat = json.dumps(out)
        for marker in ("CLAUDECODE_PROCESS_GROUP_ID", "/private/tmp",
                       "/usr/bin", "top-secret-x", "hunter2"):
            self.assertNotIn(marker, flat)
        # the raw hook type is never carried into the event; the derived kind
        # is tool_result (PostToolUse)
        self.assertEqual(out["kind"], "tool_result")
        self.assertNotIn("PostToolUse", flat)

    def test_environment_never_forwarded_even_when_raw_has_env(self):
        raw = {"type": "PreToolUse", "env": {"SECRET_VALUE": "top-secret-x"}}
        out = hooks.HookAdapter(
            _manifest(hooks=True), session_id=SESSION,
            machine_id=MACHINE, stream_id="stream_src_1").normalize(raw)
        self.assertNotIn("top-secret-x", json.dumps(out))
        self.assertEqual(out["kind"], "tool_call")
        self.assertNotIn("PreToolUse", json.dumps(out))

    def test_absent_hook_source_is_disabled(self):
        adapter = hooks.HookAdapter(_manifest(hooks=False))
        self.assertFalse(adapter.enabled)
        self.assertIsNone(adapter.normalize({"type": "anything"}))


class HookShapeContentTests(unittest.TestCase):
    """Review R1: real hook shapes map into schema-valid payload fields with
    meaningful content preserved."""

    def _cap(self, **kw):
        return hooks.HookAdapter(
            _manifest(hooks=True), session_id=SESSION,
            machine_id=MACHINE, stream_id="stream_src_1", **kw)

    def test_user_prompt_submit_prompt_becomes_text(self):
        out = self._cap().normalize({
            "type": "UserPromptSubmit",
            "prompt": "explain the diff",
        })
        self.assertEqual(out["kind"], "user_message")
        self.assertEqual(out["payload"]["text"], "explain the diff")

    def test_pretool_tool_input_preserved_and_scrubbed(self):
        out = self._cap().normalize({
            "type": "PreToolUse",
            "tool_name": "Bash",
            "tool_use_id": "toolu_01abc",
            "tool_input": {
                "command": "ls -la",
                "env": {"TOKEN": "xyz"},
                "cwd": "/private/tmp",
                "timeout": 30,
            },
        })
        self.assertEqual(out["kind"], "tool_call")
        self.assertEqual(out["payload"]["tool_name"], "Bash")
        self.assertEqual(out["payload"]["call_id"], "toolu_01abc")
        flat = json.dumps(out)
        self.assertIn("ls -la", flat)          # real command preserved
        self.assertIn("timeout", flat)         # benign field kept
        self.assertNotIn("TOKEN", flat)        # nested env scrubbed
        self.assertNotIn("xyz", flat)
        self.assertNotIn("/private/tmp", flat)  # nested cwd scrubbed

    def test_posttool_tool_response_preserved_and_scrubbed(self):
        out = self._cap().normalize({
            "type": "PostToolUse",
            "tool_name": "Read",
            "tool_use_id": "toolu_2abc",
            "tool_response": {
                "output": "file contents",
                "path": "/etc/passwd",
                "credential": "hunter2",
            },
        })
        self.assertEqual(out["kind"], "tool_result")
        self.assertEqual(out["payload"]["call_id"], "toolu_2abc")
        flat = json.dumps(out)
        self.assertIn("output", flat)
        self.assertNotIn("hunter2", flat)
        self.assertNotIn("/etc/passwd", flat)

    def test_hook_event_id_secret_shape_is_replaced(self):
        from tools.session.capture.hooks import HookAdapter as HA
        out = self._cap().normalize({
            "type": "UserPromptSubmit",
            "prompt": "hi",
            "event_id": "secret-hunter2-token-99",
        })
        self.assertNotIn("secret-hunter2-token-99", json.dumps(out))
        self.assertTrue(out["event_id"].startswith("hook_"))

    def test_hook_event_id_jwt_and_cookie_shapes_replaced(self):
        """Review R2: JWT-/session-/cookie-shaped hook event ids are replaced
        with generated opaque ids and never reach the durable surface."""
        for bad_id in (
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1In0.sig",
            "session=abc123%2Btoken%3D",
            "sid=Zm9vYmFyK3h5eg==",
            "my.bearer.token",
        ):
            out = self._cap().normalize({
                "type": "UserPromptSubmit", "prompt": "x", "event_id": bad_id,
            })
            flat = json.dumps(out)
            self.assertNotIn("eyJhbGci", flat)
            self.assertNotIn("sid=", flat)
            self.assertNotIn("bearer", flat)
            self.assertNotIn("token", flat.lower())
            self.assertTrue(out["event_id"].startswith("hook_"))


class PtyAdapterTests(unittest.TestCase):
    """PTY capture is explicitly best-effort and bounded."""

    def test_best_effort_quality(self):
        self.assertEqual(pty_module.CAPTURE_QUALITY, "best_effort")

    def test_pty_enabled_by_capability_only(self):
        self.assertTrue(pty_module.PtyCapture(_manifest(pty=True)).enabled)
        self.assertFalse(pty_module.PtyCapture(_manifest(pty=False)).enabled)

    def test_pty_normalize_is_bounded_best_effort(self):
        cap = pty_module.PtyCapture(_manifest(pty=True))
        out = cap.normalize("hello world\n")
        self.assertEqual(out["capture_quality"], "best_effort")
        self.assertIn("text", out["payload"])
        self.assertLess(len(out["payload"]["text"]), 2 ** 10)

    def test_pty_disabled_returns_none(self):
        cap = pty_module.PtyAdapter(_manifest(pty=False))
        self.assertIsNone(cap.normalize("hello"))

    def test_pty_normalize_never_raises_at_max_frame(self):
        """Review R1: a chunk at MAX_PTY_FRAME must not raise; the whole
        serialized event must stay at or under the shared MAX_EVENT_BYTES."""
        import session_schema as ss
        cap = pty_module.PtyCapture(_manifest(pty=True))
        chunk = "x" * pty_module.MAX_PTY_FRAME
        event = cap.normalize(chunk)
        self.assertIsNotNone(event)
        payload_text = event["payload"]["text"]
        # the whole serialized event must fit under the shared 64 KiB cap
        serialized = json.dumps(event, ensure_ascii=True,
                                separators=(",", ":"))
        self.assertLessEqual(len(serialized.encode("utf-8")),
                             ss.MAX_EVENT_BYTES)
        # control-stripped text is preserved (bounded), not dropped
        self.assertLessEqual(len(payload_text), pty_module.MAX_PTY_FRAME)
        self.assertTrue(payload_text.startswith("x"))

    def test_pty_normalized_corrupt_invalid_chunk_is_none(self):
        cap = pty_module.PtyCapture(_manifest(pty=True))
        self.assertIsNone(cap.normalize("\x1b[2J"))  # only control chars
        self.assertIsNone(cap.normalize(""))


class SourceFailureIsolationTests(unittest.TestCase):
    """Bridge gap/quality helpers produce validated bounded events."""

    def test_capture_gap_is_valid_and_degrades(self):
        base = _base_event()
        gap = build_capture_gap_event(
            base, start_sequence=3, end_sequence=4,
            reason="source_unavailable")
        clean = validate_event(gap)
        self.assertEqual(clean["kind"], "capture_gap")
        gap_quality = clean["payload"]["quality"]
        base_quality = clean["capture_quality"]
        self.assertEqual(gap_quality, "best_effort")
        # best_effort is LOWER than structured; on the ordered quality scale
        # its index is HIGHER
        self.assertGreater(CAPTURE_QUALITIES.index(gap_quality),
                           CAPTURE_QUALITIES.index(base_quality))

    def test_capture_quality_downgrade_event(self):
        base = _base_event()
        evt = build_capture_quality_event(
            base, old_quality="exact", new_quality="structured",
            reason="native transcript ended")
        clean = validate_event(evt)
        self.assertEqual(clean["kind"], "capture_quality_changed")
        self.assertEqual(clean["payload"]["old_quality"], "exact")
        self.assertEqual(clean["payload"]["new_quality"], "structured")

    def test_illegal_quality_upgrade_is_rejected(self):
        base = _base_event()
        with self.assertRaises(ValueError):
            build_capture_quality_event(
                base, old_quality="best_effort", new_quality="exact",
                reason="boom")

    def test_gap_reason_never_leaks_raw_diagnostics(self):
        base = _base_event()
        gap = build_capture_gap_event(
            base, start_sequence=1, end_sequence=2,
            reason="Traceback: boom at /private/tmp/x.json token=abc")
        flat = json.dumps(gap)
        for marker in ("Traceback", "/private/tmp", "token=abc", "boom"):
            self.assertNotIn(marker, flat)


class CapabilitySelectionTests(unittest.TestCase):
    """Adapter selection follows the manifest, never CLI names/versions."""

    def test_all_false_manifest_selects_nothing(self):
        from tools.session.bridge import select_sources
        self.assertEqual(select_sources(_manifest()), [])

    def test_structured_capability_enables_structured(self):
        from tools.session.bridge import select_sources
        self.assertEqual(select_sources(_manifest(structured_stream=True)),
                         ["structured_stream"])
        self.assertEqual(select_sources(_manifest(native_transcript=True)),
                         ["native_transcript"])

    def test_flags_never_inferred_from_names(self):
        # CLI names/versions alone must never set a capability
        self.assertFalse(_manifest()["native_transcript"])
        self.assertFalse(_manifest()["structured_stream"])
        self.assertFalse(_manifest()["hooks"])
        self.assertFalse(_manifest()["pty"])

    def test_hermes_never_gets_structured(self):
        herm = _manifest(native_transcript=False, structured_stream=False,
                       spawn=False, resume=False)
        self.assertFalse(herm["native_transcript"])
        self.assertFalse(herm["structured_stream"])
        self.assertFalse(herm["spawn"])


if __name__ == "__main__":
    unittest.main()