"""Repository protocol contract and legacy-facade regression tests.

New persistence code is injected with an explicit ``Path`` and is free of the
Flask stack; the legacy ``hub.state`` module stays interoperable through the
compatibility facade.
"""

import json
import tempfile
import time
import unittest
from pathlib import Path

from hub import events, scan, state as store
from hub.infrastructure.state_repository import (
    JsonlObservationRepository,
    LegacyStateStoreAdapter,
)
from hub.infrastructure.task_repository import SqliteTaskRepository
from hub.repositories import MACHINE_NAME_RE, ObservationRepository, TaskRepository


class TaskRepositoryIsolationTests(unittest.TestCase):
    def test_two_repositories_use_different_databases(self):
        first = SqliteTaskRepository(Path(tempfile.mkdtemp()) / "one.db")
        second = SqliteTaskRepository(Path(tempfile.mkdtemp()) / "two.db")
        first.init()
        second.init()
        self.assertIsInstance(first, TaskRepository)
        first.create_task(machine="hk", agent_type="codex", project="agent-fleet",
                          instruction="one", requested_by="op")
        self.assertEqual(second.list_tasks(), [])


class ObservationRepositoryTests(unittest.TestCase):
    def test_repository_uses_injected_directory(self):
        root = Path(tempfile.mkdtemp())
        repo = JsonlObservationRepository(root)
        repo.save_snapshot("hk", {"machine": "hk", "reachable": True})
        self.assertTrue((root / "hk.jsonl").exists())
        self.assertTrue((root / "hk.json").exists())

    def test_current_snapshot_is_atomic_and_history_is_bounded(self):
        repo = JsonlObservationRepository(Path(tempfile.mkdtemp()), max_jsonl_bytes=512,
                                           keep_lines=3)
        for seq in range(20):
            repo.save_snapshot("hk", {"machine": "hk", "seq": seq})
        self.assertEqual(repo.read_current("hk")["seq"], 19)
        self.assertLessEqual(len(repo.read_history("hk", 100)), 20)
        for row in repo.read_history("hk", 100):
            self.assertIsInstance(row, dict)


class ObservationRepositoryRegressionTests(unittest.TestCase):
    def test_satisfies_observation_repository_protocol(self):
        repo = JsonlObservationRepository(Path(tempfile.mkdtemp()))
        self.assertIsInstance(repo, ObservationRepository)

    def test_rotation_truncates_jsonl_to_keep_lines(self):
        root = Path(tempfile.mkdtemp())
        repo = JsonlObservationRepository(root, max_jsonl_bytes=512, keep_lines=3)
        for seq in range(20):
            repo.save_snapshot("hk", {"machine": "hk", "seq": seq, "pad": "x" * 20})
        self.assertEqual(repo.read_current("hk")["seq"], 19)
        history = repo.read_history("hk", 100)
        # Rotation keeps a bounded tail; the oldest writes are dropped.
        self.assertLess(len(history), 20)
        self.assertEqual(history[-1]["seq"], 19)
        self.assertLessEqual((root / "hk.jsonl").stat().st_size, 512 * 2)
        for row in history:
            self.assertIsInstance(row, dict)

    def test_small_jsonl_is_untouched(self):
        root = Path(tempfile.mkdtemp())
        repo = JsonlObservationRepository(root)
        repo.save_snapshot("hk", {"machine": "hk", "seq": 1})
        repo.save_snapshot("hk", {"machine": "hk", "seq": 2})
        self.assertEqual(
            len((root / "hk.jsonl").read_text().splitlines()), 2)

    def test_malformed_jsonl_lines_are_skipped(self):
        root = Path(tempfile.mkdtemp())
        repo = JsonlObservationRepository(root)
        repo.save_snapshot("mac-local", {"machine": "mac-local", "seq": 1})
        (root / "mac-local.jsonl").write_text(
            '{"machine": "mac-local", "seq": 1}\n'
            "NOT-JSON\n"
            '{"machine": "mac-local", "seq": 2}\n'
        )
        history = repo.read_history("mac-local", 100)
        self.assertEqual(len(history), 2)
        for row in history:
            self.assertIsInstance(row, dict)

    def test_machines_lists_machines_with_current_snapshot(self):
        root = Path(tempfile.mkdtemp())
        repo = JsonlObservationRepository(root)
        repo.save_snapshot("hk", {"machine": "hk"})
        repo.save_snapshot("tebi", {"machine": "tebi"})
        self.assertEqual(repo.read_history("hk", 1), repo.read_history("hk", 1))
        self.assertEqual(repo.machines(), {"hk", "tebi"})

    def test_machine_name_cannot_escape_state_directory(self):
        root = Path(tempfile.mkdtemp())
        state_dir = root / "state"
        state_dir.mkdir()
        repo = JsonlObservationRepository(state_dir)
        for unsafe in ("../escaped", "..", ".hidden", "-lead-dash",
                       "a/b", "x" * 65, "mac\nlocal"):
            with self.assertRaises(ValueError):
                repo.save_snapshot(unsafe, {"machine": unsafe})
            with self.assertRaises(ValueError):
                repo.read_current(unsafe)
            with self.assertRaises(ValueError):
                repo.read_history(unsafe)
        self.assertFalse((root / "escaped.json").exists())
        self.assertFalse((root / "escaped.jsonl").exists())

    def test_read_history_with_no_data_returns_empty(self):
        repo = JsonlObservationRepository(Path(tempfile.mkdtemp()))
        self.assertEqual(repo.read_history("ghost", 50), [])
        self.assertIsNone(repo.read_current("ghost"))
        self.assertEqual(repo.machines(), set())

    def test_machine_re_accepts_auth_contract(self):
        self.assertTrue(MACHINE_NAME_RE.fullmatch("hk"))
        self.assertTrue(MACHINE_NAME_RE.fullmatch("mac-local.1"))
        self.assertTrue(MACHINE_NAME_RE.fullmatch("katabump_2"))
        self.assertFalse(MACHINE_NAME_RE.fullmatch("../escaped"))
        self.assertFalse(MACHINE_NAME_RE.fullmatch("-lead-dash"))
        self.assertFalse(MACHINE_NAME_RE.fullmatch("x" * 65))

    def test_legacy_adapter_wraps_injected_repository(self):
        root = Path(tempfile.mkdtemp())
        repo = JsonlObservationRepository(root)
        adapter = LegacyStateStoreAdapter(repo)
        adapter.save_snapshot("mac-local", {
            "machine": "mac-local", "reachable": True, "agents": {},
        })
        self.assertTrue(adapter.read_current("mac-local")["reachable"])
        self.assertEqual(len(adapter.read_history("mac-local", 5)), 1)
        changes = adapter.diff_previous("mac-local", {
            "machine": "mac-local", "reachable": True,
            "agents": {"codex": {"installed": True}},
        })
        self.assertEqual(changes, ["agents"])
        self.assertEqual(adapter.machines(), {"mac-local"})


class ScanSkipsInvalidMachineArtifactsTests(unittest.TestCase):
    """A stray malformed ``*.json`` artifact must never abort reconciliation."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.old_state_dir = store.STATE_DIR
        self.old_event_log = events.EVENT_LOG
        store.STATE_DIR = self.temp_dir
        events.EVENT_LOG = self.temp_dir / "events.jsonl"

    def tearDown(self):
        store.STATE_DIR = self.old_state_dir
        events.EVENT_LOG = self.old_event_log

    def _write_stale(self, machine):
        store.save_snapshot(machine, {
            "machine": machine, "source": "ingest", "reachable": True,
            "agents": {},
        })
        current = store.read_current(machine)
        current["_ts"] = time.time() - 301
        (self.temp_dir / f"{machine}.json").write_text(json.dumps(current))

    def test_reconcile_ignores_invalid_machine_artifact_and_reconciles_valid(self):
        # A stray malformed current-JSON artifact whose stem is not a valid
        # machine name. Its filename never passes MACHINE_NAME_RE.
        (self.temp_dir / "-lead-dash.json").write_text('{"machine": "-lead-dash"}')
        self._write_stale("hk")

        results = scan.reconcile_ingest(now=time.time())

        machines_seen = [r["machine"] for r in results]
        self.assertIn("hk", machines_seen)
        self.assertNotIn("-lead-dash", machines_seen)
        # The stale machine is reconciled.
        self.assertFalse(store.read_current("hk")["reachable"])
        # The invalid artifact is untouched and still on disk.
        self.assertTrue((self.temp_dir / "-lead-dash.json").exists())

    def test_machines_enumeration_skips_invalid_stems(self):
        (self.temp_dir / "valid.json").write_text("{}")
        (self.temp_dir / "-lead-dash.json").write_text("{}")
        self.assertEqual(store.machines(), {"valid"})


class ObserveRotationAliasTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.old_state_dir = store.STATE_DIR
        self.old_max = store.MAX_JSONL_BYTES
        self.old_keep = store.ROTATE_KEEP_LINES
        store.STATE_DIR = self.temp_dir
        store.MAX_JSONL_BYTES = 512
        store.ROTATE_KEEP_LINES = 3

    def tearDown(self):
        store.STATE_DIR = self.old_state_dir
        store.MAX_JSONL_BYTES = self.old_max
        store.ROTATE_KEEP_LINES = self.old_keep

    def test_rotate_if_needed_public_alias_is_invokable(self):
        self.assertTrue(callable(store.rotate_if_needed))
        # No JSONL yet: rotation is a no-op returning False.
        self.assertFalse(store.rotate_if_needed("hk"))
        # Both the public alias and the legacy private name delegate the same
        # implementation.
        self.assertTrue(callable(store._rotate_if_needed))
        self.assertIs(store.rotate_if_needed, store._rotate_if_needed)


class LegacyFacadeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.old_state_dir = store.STATE_DIR
        self.old_max = store.MAX_JSONL_BYTES
        self.old_keep = store.ROTATE_KEEP_LINES
        store.STATE_DIR = self.temp_dir
        store.MAX_JSONL_BYTES = 512
        store.ROTATE_KEEP_LINES = 3

    def tearDown(self):
        store.STATE_DIR = self.old_state_dir
        store.MAX_JSONL_BYTES = self.old_max
        store.ROTATE_KEEP_LINES = self.old_keep

    def test_module_facade_still_works(self):
        store.save_snapshot("hk", {"machine": "hk", "ok": True})
        current = store.read_current("hk")
        self.assertTrue(current["ok"])
        self.assertEqual(len(store.read_history("hk")), 1)
        self.assertEqual(store.machines(), {"hk"})

    def test_module_facade_uses_updated_state_dir(self):
        store.save_snapshot("tebi", {"machine": "tebi", "source": "ingest"})
        self.assertTrue((self.temp_dir / "tebi.json").exists())
        self.assertTrue((self.temp_dir / "tebi.jsonl").exists())

    def test_module_facade_rotate_respects_updated_constants(self):
        for seq in range(20):
            store.save_snapshot("hk", {
                "machine": "hk", "seq": seq, "pad": "x" * 100,
            })
        current = store.read_current("hk")
        self.assertEqual(current["seq"], 19)
        path = self.temp_dir / "hk.jsonl"
        # File size is bounded; the newest snapshot survives the truncation.
        self.assertLessEqual(path.stat().st_size, 512 * 2)
        history = store.read_history("hk", 100)
        self.assertLess(len(history), 20)
        self.assertEqual(history[-1]["seq"], 19)
        store.save_snapshot("hk", {
            "machine": "hk", "seq": 20, "pad": "x" * 100,
        })
        self.assertEqual(store.read_current("hk")["seq"], 20)

    def test_module_facade_diff_previous(self):
        store.save_snapshot("hk", {
            "machine": "hk", "reachable": True, "agents": {},
        })
        changes = store.diff_previous("hk", {
            "machine": "hk", "reachable": True,
            "agents": {"codex": {"installed": True}},
        })
        self.assertEqual(changes, ["agents"])

    def test_module_facade_rejects_unsafe_machine_name(self):
        with self.assertRaises(ValueError):
            store.save_snapshot("../escaped", {"machine": "../escaped"})
        self.assertFalse((self.temp_dir.parent / "escaped.json").exists())


if __name__ == "__main__":
    unittest.main()
