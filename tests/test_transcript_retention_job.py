import threading
import tempfile
import unittest
from pathlib import Path

from hub.bootstrap import create_app, start_background_jobs
from hub.config import FleetConfig


class RecordingTranscriptRepository:
    def __init__(self):
        self.called = threading.Event()
        self.calls = []

    def purge_expired_raw(self, *, now):
        self.calls.append(now)
        self.called.set()
        return []


class TranscriptRetentionJobTests(unittest.TestCase):
    def test_enabled_transcript_store_starts_a_stoppable_retention_job(self):
        transcript = RecordingTranscriptRepository()
        config = FleetConfig.from_root(
            self._root(),
            session_repositories_enabled=True,
            session_encryption_raw=b"k" * 32,
        )
        app = create_app(config, repositories={"transcript": transcript})

        jobs = start_background_jobs(
            app, reconcile_interval_s=3600, lease_reconciler_interval_s=3600)
        try:
            self.assertIn("transcript_retention", jobs)
            self.assertTrue(transcript.called.wait(timeout=2))
            self.assertEqual(len(transcript.calls), 1)
            self.assertTrue(transcript.calls[0].endswith("Z"))
        finally:
            for stop in jobs.values():
                stop.set()

    def test_disabled_transcript_store_does_not_start_retention(self):
        transcript = RecordingTranscriptRepository()
        app = create_app(
            FleetConfig.from_root(self._root()),
            repositories={"transcript": transcript},
        )
        jobs = start_background_jobs(
            app, reconcile_interval_s=3600, lease_reconciler_interval_s=3600)
        try:
            self.assertNotIn("transcript_retention", jobs)
            self.assertFalse(transcript.called.wait(timeout=0.1))
        finally:
            for stop in jobs.values():
                stop.set()

    def _root(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        return Path(root.name)


if __name__ == "__main__":
    unittest.main()
