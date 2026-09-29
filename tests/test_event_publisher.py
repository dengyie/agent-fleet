"""Tests for EventPublisher lifecycle, isolation, and failure handling.

See Task 3 brief:
  - unsubscribe stops future delivery
  - full SSE queue does not block emit
  - repository failure does not drop live delivery
"""
import json
import queue
import threading
import time
import unittest
from pathlib import Path


# ---------------------------------------------------------------------------
# Fake repository for unit tests
# ---------------------------------------------------------------------------
class FakeEventRepository:
    """In-memory event repository with optional append failure."""

    def __init__(self, append_error=None):
        self.events = []
        self._lock = threading.Lock()
        self.append_error = append_error

    def append(self, event: dict) -> None:
        if self.append_error:
            raise self.append_error
        with self._lock:
            self.events.append(event)

    def read_recent(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return list(self.events[-limit:])

    def read_since(self, ts: float, limit: int = 200) -> list[dict]:
        out = []
        with self._lock:
            for ev in self.events:
                if len(out) >= limit:
                    break
                if float(ev.get("ts") or 0) > ts:
                    out.append(ev)
        return out

    def read_since_sequence(self, sequence: int, limit: int = 200) -> list[dict]:
        out = []
        with self._lock:
            for ev in self.events:
                if len(out) >= limit:
                    break
                if int(ev.get("event_seq") or 0) > sequence:
                    out.append(ev)
        return out


# ---------------------------------------------------------------------------
# EventPublisher tests
# ---------------------------------------------------------------------------
class EventPublisherTests(unittest.TestCase):
    """Lifecycle and isolation tests from the Task 3 brief."""

    def setUp(self):
        from hub.application.event_publisher import EventPublisher

        self._cls = EventPublisher

    def test_unsubscribe_stops_future_delivery(self):
        publisher = self._cls(FakeEventRepository())
        seen = []
        unsubscribe = publisher.subscribe(seen.append)
        publisher.emit("fleet_event", machine="hk")
        unsubscribe()
        publisher.emit("fleet_event", machine="tebi")
        self.assertEqual([e["machine"] for e in seen], ["hk"])

    def test_full_sse_queue_does_not_block_emit(self):
        publisher = self._cls(FakeEventRepository(), queue_size=1)
        q, unsubscribe = publisher.subscribe_sse()
        try:
            publisher.emit("one")
            publisher.emit("two")
            self.assertIs(q.get(timeout=1), publisher.SSE_GAP)
            with self.assertRaises(queue.Empty):
                q.get(timeout=0.1)
        finally:
            unsubscribe()

    def test_repository_failure_does_not_drop_live_delivery(self):
        repo = FakeEventRepository(append_error=OSError("disk unavailable"))
        publisher = self._cls(repo)
        seen = []
        unsubscribe = publisher.subscribe(seen.append)
        try:
            publisher.emit("state_changed", machine="hk")
        finally:
            unsubscribe()
        self.assertEqual(seen[0]["machine"], "hk")
        self.assertNotIn("event_seq", seen[0])

    def test_subscribe_returns_idempotent_unsubscribe(self):
        publisher = self._cls(FakeEventRepository())
        seen = []
        unsub = publisher.subscribe(seen.append)
        unsub()
        unsub()  # second call must not raise
        publisher.emit("state_changed", machine="hk")
        self.assertEqual(seen, [])

    def test_subscribe_sse_returns_independent_queues(self):
        publisher = self._cls(FakeEventRepository())
        q1, unsub1 = publisher.subscribe_sse()
        q2, unsub2 = publisher.subscribe_sse()
        try:
            publisher.emit("state_changed", machine="hk")
            self.assertEqual(q1.get(timeout=1)["machine"], "hk")
            self.assertEqual(q2.get(timeout=1)["machine"], "hk")
        finally:
            unsub1()
            unsub2()

    def test_emit_returns_event_dict(self):
        publisher = self._cls(FakeEventRepository())
        ev = publisher.emit("state_changed", machine="hk", changes=["agents"])
        self.assertEqual(ev["event"], "state_changed")
        self.assertEqual(ev["machine"], "hk")
        self.assertEqual(ev["changes"], ["agents"])
        self.assertIn("ts", ev)
        self.assertIn("extra", ev)

    def test_read_recent_delegates_to_repository(self):
        repo = FakeEventRepository()
        publisher = self._cls(repo)
        publisher.emit("one", machine="hk")
        publisher.emit("two", machine="tebi")
        recent = publisher.read_recent(10)
        self.assertEqual(len(recent), 2)
        self.assertEqual(recent[-1]["machine"], "tebi")

    def test_read_since_delegates_to_repository(self):
        repo = FakeEventRepository()
        publisher = self._cls(repo)
        publisher.emit("one", machine="hk")
        t = time.time() + 0.001  # ensure next event is strictly after
        time.sleep(0.002)
        publisher.emit("two", machine="tebi")
        recent = publisher.read_since(t)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["machine"], "tebi")

    def test_emit_assigns_monotonic_event_sequence_and_replays_by_sequence(self):
        publisher = self._cls(FakeEventRepository())
        first = publisher.emit("one")
        second = publisher.emit("two")
        self.assertEqual(first["event_seq"], 1)
        self.assertEqual(second["event_seq"], 2)
        self.assertEqual(
            [e["event"] for e in publisher.read_since_sequence(1)], ["two"])

    def test_concurrent_emit_delivery_preserves_durable_sequence_order(self):
        class BlockingRepository(FakeEventRepository):
            def __init__(self):
                super().__init__()
                self.first_started = threading.Event()
                self.release_first = threading.Event()

            def append(self, event):
                if event.get("machine") == "first":
                    self.first_started.set()
                    self.release_first.wait(timeout=2)
                super().append(event)

        repo = BlockingRepository()
        publisher = self._cls(repo)
        seen = []
        publisher.subscribe(lambda event: seen.append(
            (event.get("event_seq"), event.get("machine"))))
        first = threading.Thread(target=publisher.emit,
                                 kwargs={"event_type": "alert", "machine": "first"})
        second = threading.Thread(target=publisher.emit,
                                  kwargs={"event_type": "alert", "machine": "second"})
        first.start()
        self.assertTrue(repo.first_started.wait(timeout=1))
        second.start()
        # The second emitter must wait behind the first publish transaction.
        time.sleep(0.05)
        self.assertEqual(seen, [])
        repo.release_first.set()
        first.join(timeout=2)
        second.join(timeout=2)
        self.assertEqual(seen, [(1, "first"), (2, "second")])

    def test_persistence_failure_does_not_consume_replay_sequence(self):
        class RecoveringRepository(FakeEventRepository):
            def __init__(self):
                super().__init__()
                self.fail = True

            def append(self, event):
                if self.fail:
                    self.fail = False
                    raise OSError("disk unavailable")
                super().append(event)

        repo = RecoveringRepository()
        publisher = self._cls(repo)
        failed = publisher.emit("alert", machine="lost")
        recovered = publisher.emit("alert", machine="recovered")
        self.assertNotIn("event_seq", failed)
        self.assertEqual(recovered["event_seq"], 1)
        self.assertEqual([e["machine"] for e in publisher.read_since_sequence(0)],
                         ["recovered"])

    def test_sse_queue_overflow_marks_gap_and_discards_stale_rows(self):
        publisher = self._cls(FakeEventRepository(), queue_size=1)
        q, unsubscribe = publisher.subscribe_sse()
        try:
            publisher.emit("one")
            publisher.emit("two")
            self.assertIs(q.get(timeout=1), publisher.SSE_GAP)
            with self.assertRaises(queue.Empty):
                q.get(timeout=0.1)
        finally:
            unsubscribe()

    def test_emit_snapshot_in_extra(self):
        publisher = self._cls(FakeEventRepository())
        snapshot = {"reachable": True, "agents": {"codex": {"installed": True}}}
        ev = publisher.emit("state_changed", machine="hk", changes=["agents"],
                            snapshot=snapshot)
        self.assertEqual(ev["extra"]["snapshot"], snapshot)

    def test_publisher_instances_do_not_share_subscribers(self):
        repo = FakeEventRepository()
        p1 = self._cls(repo)
        p2 = self._cls(repo)
        seen = []
        p1.subscribe(seen.append)
        p2.emit("state_changed", machine="hk")
        self.assertEqual(seen, [])

    def test_legacy_facade_uses_injected_publisher_per_app_context(self):
        from hub import events
        from hub.bootstrap import create_app
        from hub.config import FleetConfig

        first = self._cls(FakeEventRepository())
        second = self._cls(FakeEventRepository())
        root = Path(__file__).resolve().parent / "__tmp_app_context"
        config = FleetConfig.from_root(root, ingest_token="secret")
        app1 = create_app(config, publisher=first)
        app2 = create_app(config, publisher=second)
        seen1 = []
        seen2 = []
        first.subscribe(seen1.append)
        second.subscribe(seen2.append)
        with app1.app_context():
            events.emit("state_changed", machine="one")
        with app2.app_context():
            events.emit("state_changed", machine="two")
        self.assertEqual([event["machine"] for event in seen1], ["one"])
        self.assertEqual([event["machine"] for event in seen2], ["two"])


# ---------------------------------------------------------------------------
# JsonlEventRepository tests
# ---------------------------------------------------------------------------
class JsonlEventRepositoryTests(unittest.TestCase):
    """Persistence, rotation, and concurrency tests."""

    def setUp(self):
        from hub.infrastructure.event_repository import JsonlEventRepository

        self._cls = JsonlEventRepository
        self.temp_dir = Path(__file__).resolve().parent / "__tmp_events"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.temp_dir / "events.jsonl"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_append_and_read_recent(self):
        repo = self._cls(self.path)
        repo.append({"event": "state_changed", "machine": "hk", "ts": 1.0})
        repo.append({"event": "state_changed", "machine": "tebi", "ts": 2.0})
        recent = repo.read_recent(10)
        self.assertEqual(len(recent), 2)
        self.assertEqual(recent[0]["machine"], "hk")

    def test_read_recent_returns_empty_for_missing_file(self):
        repo = self._cls(self.path)
        self.assertEqual(repo.read_recent(10), [])

    def test_read_recent_negative_limit_is_bounded(self):
        repo = self._cls(self.path)
        for i in range(5):
            repo.append({"event": f"e{i}", "machine": "hk",
                         "ts": float(i)})

        self.assertEqual(repo.read_recent(-1), [])

    def test_read_since_returns_events_after_timestamp(self):
        repo = self._cls(self.path)
        repo.append({"event": "one", "machine": "hk", "ts": 100.0})
        repo.append({"event": "two", "machine": "hk", "ts": 200.0})
        repo.append({"event": "three", "machine": "hk", "ts": 300.0})
        recent = repo.read_since(150.0, limit=10)
        self.assertEqual(len(recent), 2)
        self.assertEqual([e["event"] for e in recent], ["two", "three"])

    def test_append_persists_sequence_and_replay_keeps_same_timestamp_events(self):
        repo = self._cls(self.path)
        first = {"event": "one", "machine": "hk", "ts": 100.0}
        second = {"event": "two", "machine": "hk", "ts": 100.0}
        repo.append(first)
        repo.append(second)
        self.assertEqual(first["event_seq"], 1)
        self.assertEqual(second["event_seq"], 2)
        self.assertEqual(
            [e["event"] for e in repo.read_since_sequence(1)], ["two"])

    def test_sequence_continues_after_repository_reopen(self):
        repo = self._cls(self.path)
        repo.append({"event": "one", "machine": "hk", "ts": 1.0})
        reopened = self._cls(self.path)
        event = {"event": "two", "machine": "hk", "ts": 1.0}
        reopened.append(event)
        self.assertEqual(event["event_seq"], 2)

    def test_failed_append_does_not_consume_sequence(self):
        import hub.infrastructure.event_repository as repository_module
        from unittest import mock

        repo = self._cls(self.path)
        with mock.patch.object(repository_module, "open",
                               side_effect=OSError("disk unavailable"),
                               create=True):
            with self.assertRaises(OSError):
                repo.append({"event": "lost", "machine": "hk"})
        recovered = {"event": "recovered", "machine": "hk"}
        repo.append(recovered)
        self.assertEqual(recovered["event_seq"], 1)

    def test_rotation_failure_keeps_written_event_and_sequence_unique(self):
        import hub.infrastructure.event_repository as repository_module
        from unittest import mock

        repo = self._cls(self.path)
        first = {"event": "first", "machine": "hk"}
        with mock.patch.object(repo, "_rotate_if_needed",
                               side_effect=OSError("rotation unavailable")):
            repo.append(first)

        second = {"event": "second", "machine": "hk"}
        repo.append(second)
        self.assertEqual(first["event_seq"], 1)
        self.assertEqual(second["event_seq"], 2)
        self.assertEqual(
            [row["event_seq"] for row in repo.read_recent(10)], [1, 2])

    def test_write_or_fsync_failure_does_not_reuse_written_sequence(self):
        import hub.infrastructure.event_repository as repository_module
        from unittest import mock

        repo = self._cls(self.path)
        first = {"event": "first", "machine": "hk"}
        second = {"event": "second", "machine": "hk"}
        with mock.patch.object(
                repository_module.os, "fsync",
                side_effect=[OSError("fsync unavailable"), None]):
            with self.assertRaises(OSError):
                repo.append(first)
            repo.append(second)

        self.assertEqual(first["event_seq"], 1)
        self.assertEqual(second["event_seq"], 2)
        self.assertEqual(
            [row["event_seq"] for row in repo.read_recent(10)], [1, 2])

    def test_read_since_respects_limit(self):
        repo = self._cls(self.path)
        for i in range(5):
            repo.append({"event": f"e{i}", "machine": "hk", "ts": float(i * 10)})
        recent = repo.read_since(0, limit=3)
        self.assertEqual(len(recent), 3)

    def test_rotation_keeps_only_last_lines_when_oversized(self):
        repo = self._cls(self.path, max_bytes=100, keep_lines=3)
        for i in range(20):
            repo.append({"event": f"e{i}", "machine": "hk", "ts": float(i)})
        recent = repo.read_recent(100)
        self.assertLessEqual(len(recent), 3)
        # Should have the last 3 events
        self.assertEqual(recent[-1]["event"], "e19")

    def test_append_creates_directory(self):
        deeper = self.temp_dir / "sub" / "events.jsonl"
        repo = self._cls(deeper)
        repo.append({"event": "test", "machine": "hk", "ts": 1.0})
        self.assertTrue(deeper.exists())

    def test_malformed_line_skipped_silently(self):
        repo = self._cls(self.path)
        repo.append({"event": "good", "machine": "hk", "ts": 1.0})
        with open(self.path, "a") as f:
            f.write("not json\n")
        repo.append({"event": "bad_after", "machine": "hk", "ts": 2.0})
        recent = repo.read_recent(10)
        self.assertEqual(len(recent), 2)
        self.assertEqual([e["event"] for e in recent], ["good", "bad_after"])

    def test_non_object_and_invalid_timestamp_rows_do_not_break_reads(self):
        self.path.write_text(
            '[1, 2, 3]\n{"event": "bad-ts", "ts": "n/a"}\n',
            encoding="utf-8")
        repo = self._cls(self.path)

        self.assertEqual(repo.read_recent(10), [{"event": "bad-ts",
                                                  "ts": "n/a"}])
        self.assertEqual(repo.read_since(0, 10), [])

    def test_invalid_timestamp_cursor_is_safe_for_direct_repository_reads(self):
        repo = self._cls(self.path)
        repo.append({"event": "one", "machine": "hk", "ts": 1.0})

        self.assertEqual(
            [row["event"] for row in repo.read_since("not-a-timestamp", 10)],
            ["one"],
        )

    def test_concurrent_append_does_not_corrupt(self):
        repo = self._cls(self.path)
        n = 50
        errors = []

        def append(i):
            try:
                repo.append({"event": f"e{i}", "machine": "hk", "ts": float(i)})
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=append, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        recent = repo.read_recent(n * 2)
        self.assertEqual(len(recent), n)
        sequences = [event["event_seq"] for event in recent]
        self.assertEqual(len(set(sequences)), n)
        self.assertEqual(sorted(sequences), list(range(1, n + 1)))


# ---------------------------------------------------------------------------
# Compatibility facade tests
# ---------------------------------------------------------------------------
class LegacyFacadeTests(unittest.TestCase):
    """hub.events module-level API still works after refactoring."""

    def setUp(self):
        self.temp_dir = Path(__file__).resolve().parent / "__tmp_legacy"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.temp_dir / "events.jsonl"
        import hub.events as evmod
        self._old_log = evmod.EVENT_LOG
        evmod.EVENT_LOG = self.path

    def tearDown(self):
        import hub.events as evmod
        evmod.EVENT_LOG = self._old_log
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_module_level_emit_and_read_recent_work(self):
        from hub import events as evmod
        evmod.emit("state_changed", machine="hk", changes=["agents"])
        recent = evmod.read_recent(10)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["event"], "state_changed")
        self.assertEqual(recent[0]["machine"], "hk")

    def test_module_level_subscribe_and_emit(self):
        from hub import events as evmod
        seen = []
        evmod.subscribe(seen.append)
        evmod.emit("state_changed", machine="hk")
        self.assertEqual(seen[0]["machine"], "hk")

    def test_sse_subscribe_and_unsubscribe(self):
        from hub import events as evmod
        q = evmod.sse_subscribe()
        try:
            evmod.emit("state_changed", machine="hk")
            self.assertEqual(q.get(timeout=1)["machine"], "hk")
        finally:
            evmod.sse_unsubscribe(q)

    def test_sse_unsubscribe_stops_delivery(self):
        from hub import events as evmod
        q = evmod.sse_subscribe()
        evmod.sse_unsubscribe(q)
        evmod.emit("state_changed", machine="hk")
        with self.assertRaises(queue.Empty):
            q.get(timeout=0.3)

    def test_empty_log_returns_empty_list(self):
        from hub import events as evmod
        self.assertEqual(evmod.read_recent(10), [])


if __name__ == "__main__":
    unittest.main()
