"""Review fix #2: spool frame write failures never advance the sequence.

A flush/fsync failure (ENOSPC) must raise ``SpoolError``, leave
``last_sequence``/checkpoint untouched, and truncate the partial frame so
later frames stay readable (no ``invalid_ack_prefix``) and restart repair
never drops acknowledged frames.
"""
import errno
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.session.spool import CODE_WRITE, LocalSpool, SpoolError

KEY = bytes(range(32))
MACH = "host-spool"
SESS = "sess_spool"


def _event(text):
    return {
        "schema_version": 1, "event_id": "evt_" + text, "stream_id": "s1",
        "machine_id": MACH, "session_id": SESS, "sequence": 1,
        "kind": "user_message", "capture_quality": "structured",
        "source": "bridge", "emitted_at": "2026-08-26T00:00:00Z",
        "payload": {"text": text, "is_complete": True},
    }


class _FailingWriter:
    """Wrap a real file: write() lands bytes partially, flush() hits ENOSPC."""

    def __init__(self, real):
        self._real = real

    def write(self, data):
        # 模拟只写入一半就出错的磁盘满场景
        self._real.write(data[: len(data) // 2])
        self._real.flush()
        return len(data) // 2

    def flush(self):
        raise OSError(errno.ENOSPC, "No space left on device")

    def tell(self):
        return self._real.tell()

    def fileno(self):
        return self._real.fileno()

    def close(self):
        self._real.close()


class SpoolWriteFailureTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="fleet-spool-enospc-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def _spool(self):
        return LocalSpool(self.dir, MACH, SESS, key=KEY)

    def test_flush_enospc_raises_and_does_not_advance(self):
        spool = self._spool()
        self.assertEqual(spool.append(_event("one")).sequence, 1)
        spool.ack(1)
        spool._open_writer = _FailingWriter(spool._open_writer)
        with self.assertRaises(SpoolError) as ctx:
            spool.append(_event("two"))
        self.assertEqual(ctx.exception.code, CODE_WRITE)
        self.assertEqual(spool.status()["last_sequence"], 1)

        # 磁盘恢复后：下一帧重用序号 2，并且可完整回放（坏帧已截断）
        self.assertEqual(spool.append(_event("three")).sequence, 2)
        self.assertEqual(spool.append(_event("four")).sequence, 3)
        texts = [e["payload"]["text"] for e in spool.read_after(0, 100, 1 << 20)]
        self.assertEqual(texts, ["three", "four"])
        spool.close()

        # 重启修复不会截断已确认/已写入的帧
        reopened = self._spool()
        self.assertEqual(reopened.status()["last_sequence"], 3)
        texts = [e["payload"]["text"]
                 for e in reopened.read_after(0, 100, 1 << 20)]
        self.assertEqual(texts, ["three", "four"])
        self.assertEqual(reopened.append(_event("five")).sequence, 4)
        reopened.close()

    def test_fsync_failure_also_rejected(self):
        spool = self._spool()
        with mock.patch("tools.session.spool.os.fsync",
                        side_effect=OSError(errno.EIO, "io")):
            with self.assertRaises(SpoolError):
                spool.append(_event("x"))
        self.assertEqual(spool.status()["last_sequence"], 0)
        self.assertEqual(spool.append(_event("y")).sequence, 1)
        self.assertEqual([e["payload"]["text"]
                          for e in spool.read_after(0, 10, 1 << 20)], ["y"])
        spool.close()

    def test_untruncatable_partial_frame_moves_to_new_segment(self):
        spool = self._spool()
        spool.append(_event("a"))
        spool._open_writer = _FailingWriter(spool._open_writer)
        with mock.patch("tools.session.spool.os.truncate",
                        side_effect=OSError(errno.EROFS, "ro")):
            with self.assertRaises(SpoolError):
                spool.append(_event("b"))
        self.assertEqual(spool.append(_event("c")).sequence, 2)
        texts = [e["payload"]["text"] for e in spool.read_after(0, 10, 1 << 20)]
        self.assertEqual(texts, ["a", "c"])
        spool.close()


class BridgeSurvivesWriteFailureTests(unittest.TestCase):
    def test_bridge_reports_reject_not_crash(self):
        from tools.session.bridge import SessionBridge
        tmp = Path(tempfile.mkdtemp(prefix="fleet-spool-bridge-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bridge = SessionBridge({"agent": "codex", "structured_stream": True}, {
            "session_id": SESS, "machine_id": MACH,
            "spool_root": tmp, "spool_key": KEY})
        self.addCleanup(bridge.close)
        with mock.patch("tools.session.spool.os.fsync",
                        side_effect=OSError(errno.ENOSPC, "full")):
            self.assertFalse(bridge.ingest_one(
                {"kind": "user_message", "payload": {"text": "hi"}}))
        self.assertTrue(os.path.isdir(tmp))


if __name__ == "__main__":
    unittest.main()
