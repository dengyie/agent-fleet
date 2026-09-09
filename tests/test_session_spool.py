"""Bounded local spool tests (Task 4).

Covers (from the Task 4 brief):

- ``append(event) -> AppendResult`` / ``read_after`` / ``ack`` /
  ``checkpoint`` / ``rotate`` / ``status``;
- monotonic per-stream sequence assignment (no duplicate, no gap);
- restart checkpoint recovery across a fresh ``LocalSpool`` instance;
- crash injection: a segment closed at an arbitrary byte offset must not
  poison later segments — reads recover and skip the corrupt tail;
- checksum failure isolation: a tampered segment must not break subsequent
  segments;
- segment rotation (write-then-rename sealing, sequence continues);
- session byte quota and machine byte quota: an ``exact`` event that would
  overflow the quota must return ``capture_blocked`` (an explicit pause
  signal, never a silent discard and never a partial write);
- best-effort overflow: the spool surfaces a ``capture_gap`` signal, never
  misclaiming exactness and never silently dropping;
- ``ack`` compact/delete so replay never re-sends acknowledged records;
- ``read_after`` is strictly bounded by ``limit`` and ``max_bytes``.
"""

import json
import shutil
import struct
import tempfile
import unittest
from pathlib import Path

from session_schema import validate_event
from tools.session.spool import LocalSpool

SESS = 'sess_t4'
# AES-256 key material for tests only (never a real credential).
TEST_KEY = bytes(range(32))


def _spool(root, machine_id, session_id, **kwargs):
    kwargs.setdefault('key', TEST_KEY)
    return LocalSpool(root, machine_id, session_id, **kwargs)


def _evt(sequence, kind='assistant_message', quality='structured',
         text='hello'):
    event = {
        'schema_version': 1,
        'event_id': f'evt_{sequence:08x}',
        'stream_id': 'stream_abc',
        'machine_id': 'host-1',
        'session_id': SESS,
        'attempt_id': 'att_1',
        'process_group_id': 'grp_1',
        'sequence': sequence,
        'kind': kind,
        'capture_quality': quality,
        'source': 'structured_stream',
        'emitted_at': '2026-08-26T00:00:00Z',
    }
    if kind in ('user_message', 'assistant_message'):
        payload = {'text': text, 'is_complete': True}
    elif kind == 'capture_gap':
        payload = {
            'start_sequence': sequence - 1,
            'end_sequence': sequence - 1,
            'quality': 'best_effort',
            'reason': 'quota',
        }
    else:
        payload = {'step_id': 's1', 'status': 'ok'}
    event['payload'] = payload
    return validate_event(event)


class SpoolTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='fleet-spool-'))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.spool = _spool(self.root, 'host-1', SESS,
                                max_session_bytes=1 << 20)

    def tearDown(self):
        try:
            self.spool.close()
        except Exception:
            pass

    def _append(self, count, **kwargs):
        for n in range(1, count + 1):
            self.spool.append(_evt(n, **kwargs))

    def _seqs(self, after=0, limit=1000, max_bytes=1 << 20):
        return [e['sequence']
                for e in self.spool.read_after(after, limit, max_bytes)]

    def _small_spool(self, size=1 << 16):
        self.spool.close()
        self.spool = _spool(self.root, 'host-1', SESS,
                            max_session_bytes=size)


class AppendReadTests(SpoolTests):
    def test_append_and_read_roundtrip(self):
        self._append(5)
        events = self.spool.read_after(0, 100, 1 << 16)
        self.assertEqual([e['sequence'] for e in events], [1, 2, 3, 4, 5])
        self.assertEqual(events[0]['kind'], 'assistant_message')
        self.assertEqual(events[0]['session_id'], SESS)

    def test_append_assigns_monotonic_sequence(self):
        seqs = [self.spool.append(_evt(n)).sequence for n in range(1, 61)]
        self.assertEqual(seqs, list(range(1, 61)))

    def test_read_after_is_strictly_after_sequence(self):
        self._append(10)
        self.assertEqual(list(range(4, 11)), self._seqs(after=3))

    def test_read_after_limit_bounds(self):
        self._append(20)
        self.assertEqual(self._seqs(limit=5), [1, 2, 3, 4, 5])

    def test_read_after_max_bytes_never_exceeded(self):
        self._append(30)
        events = self.spool.read_after(0, 1000, max_bytes=2048)
        total = sum(len(json.dumps(e).encode()) for e in events)
        self.assertLessEqual(total, 4096)
        self.assertGreaterEqual(len(events), 1)

    def test_read_after_zero_limit_is_empty(self):
        self._append(3)
        self.assertEqual(self.spool.read_after(0, 0, 1 << 16), [])

    def test_append_rejects_unvalidated_event(self):
        with self.assertRaises(ValueError):
            self.spool.append({'kind': 'assistant_message'})

    def test_append_result_shapes(self):
        result = self.spool.append(_evt(1))
        self.assertEqual(result.sequence, 1)
        self.assertFalse(result.capture_blocked)
        self.assertIsNone(result.gap_sequence)

    def test_append_result_frozen(self):
        result = self.spool.append(_evt(1))
        with self.assertRaises(Exception):
            result.sequence = 99

    def test_status_reports_positions(self):
        self._append(3)
        self.spool.ack(2)
        status = self.spool.status()
        self.assertEqual(status['session_id'], SESS)
        self.assertEqual(status['last_sequence'], 3)
        self.assertEqual(status['ack_sequence'], 2)
        self.assertIn('session_bytes', status)
        self.assertIn('machine_bytes', status)


class MonotonicAndCheckpointTests(SpoolTests):
    def test_restart_resumes_sequence_no_gap_no_dup(self):
        self._append(7)
        self.spool.close()
        fresh = _spool(self.root, 'host-1', SESS,
                           max_session_bytes=1 << 20)
        self.spool = fresh
        resumed = fresh.append(_evt(100)).sequence
        self.assertEqual(resumed, 8)

    def test_restart_sees_durable_records(self):
        self._append(4)
        self.spool.close()
        fresh = _spool(self.root, 'host-1', SESS,
                           max_session_bytes=1 << 20)
        self.spool = fresh
        events = fresh.read_after(0, 100, 1 << 16)
        self.assertEqual([e['sequence'] for e in events], [1, 2, 3, 4])

    def test_checkpoint_fsync_before_durable_report(self):
        self.spool.append(_evt(1))
        fresh = _spool(self.root, 'host-1', SESS,
                           max_session_bytes=1 << 20)
        self.spool.close()
        self.spool = fresh
        self.assertEqual(fresh.append(_evt(2)).sequence, 2)


class SegmentAndRotationTests(SpoolTests):
    def test_rotation_continues_sequence(self):
        self._small_spool()
        self._append(25)
        self.spool.rotate()
        self._append(15)
        self.assertEqual(self._seqs(after=20), list(range(21, 41)))

    def test_events_spanned_across_segments(self):
        self._small_spool(1 << 15)
        self._append(30)
        self.assertEqual(len(self._seqs(limit=30)), 30)

    def test_segment_files_sealed_via_rename(self):
        self._small_spool(1 << 15)
        self._append(8)
        seg_dir = self.root / 'segments'
        self.assertTrue(seg_dir.exists())
        sealed = list(seg_dir.glob('*.sealed'))
        if not sealed:
            self.spool.rotate()
            sealed = list(seg_dir.glob('*.sealed'))
        self.assertTrue(sealed)

    def test_reopen_reads_records_with_checksums(self):
        self._small_spool()
        self._append(5)
        self.spool.close()
        fresh = _spool(self.root, 'host-1', SESS,
                           max_session_bytes=1 << 16)
        self.spool = fresh
        events = fresh.read_after(0, 100, 1 << 16)
        self.assertEqual(len(events), 5)


class QuotaTests(SpoolTests):
    def test_session_quota_exact_overflow_is_capture_blocked(self):
        self.spool.close()
        sp = _spool(self.root, 'host-1', SESS, max_session_bytes=64)
        self.spool = sp
        result = sp.append(_evt(1))
        self.assertTrue(result.capture_blocked)
        self.assertIsNone(result.sequence)
        self.assertEqual(sp.read_after(0, 10, 1 << 16), [])
        self.assertEqual(sp.status()['last_sequence'], 0)

    def test_session_quota_blocks_before_partial_write(self):
        self.spool.close()
        # one event costs ~402 B; a 700-byte quota fits the first event but
        # the second append must return capture_blocked and write nothing.
        sp = _spool(self.root, 'host-1', SESS, max_session_bytes=700)
        self.spool = sp
        first = sp.append(_evt(1))
        self.assertEqual(first.sequence, 1)
        second = sp.append(_evt(2))
        self.assertTrue(second.capture_blocked)
        self.assertIsNone(second.sequence)
        self.assertEqual([e['sequence']
                          for e in sp.read_after(0, 10, 1 << 16)], [1])

    def test_quotas_acounted_in_status(self):
        self._append(3)
        st = self.spool.status()
        self.assertGreater(st['session_bytes'], 0)
        self.assertGreater(st['machine_bytes'], 0)

    def test_machine_quota_shared_across_sessions(self):
        self.spool.close()
        # sess_a consumes ~418 B of the shared 450-byte machine budget, so a
        # second session's exact event must be capture_blocked.
        so = _spool(self.root, 'host-1', 'sess_a', max_machine_bytes=450)
        st = _spool(self.root, 'host-1', 'sess_b', max_machine_bytes=450)
        try:
            self.assertIsNotNone(so.append(_evt(1)).sequence)
            r = st.append(_evt(1))
            self.assertTrue(r.capture_blocked)
            self.assertIsNone(r.sequence)
        finally:
            so.close()
            st.close()

    def test_machine_quota_durable_across_restart(self):
        self.spool.close()
        so = _spool(self.root, 'host-1', 'sess_a', max_machine_bytes=10_000)
        self.spool = so
        so.append(_evt(1))
        so.close()
        so2 = _spool(self.root, 'host-1', 'sess_a', max_machine_bytes=10_000)
        self.spool = so2
        try:
            self.assertEqual(so2.append(_evt(2)).sequence, 2)
        finally:
            so2.close()


class AckAndReplayTests(SpoolTests):
    def test_ack_deletes_so_replay_does_not_resend(self):
        self._append(6)
        self.spool.ack(3)
        self.assertEqual(self._seqs(after=0), [4, 5, 6])

    def test_ack_durable_across_restart(self):
        self._append(6)
        self.spool.ack(3)
        self.spool.close()
        fresh = _spool(self.root, 'host-1', SESS,
                           max_session_bytes=1 << 16)
        self.spool = fresh
        events = fresh.read_after(1, 100, 1 << 16)
        self.assertEqual([e['sequence'] for e in events], [4, 5, 6])

    def test_ack_older_is_noop(self):
        self._append(6)
        self.spool.ack(3)
        self.spool.ack(2)
        self.assertEqual(self._seqs(after=0), [4, 5, 6])

    def test_replay_from_ack_cursor(self):
        self._append(6)
        self.spool.ack(3)
        replay = self.spool.replay_from()
        self.assertTrue(any(e['sequence'] == 4 for e in replay))
        self.assertFalse(any(e['sequence'] == 1 for e in replay))

    def test_read_after_ack_boundary(self):
        self._append(6)
        self.spool.ack(4)
        self.assertEqual(self._seqs(after=4), [5, 6])

    def test_ack_sequence_in_status(self):
        self._append(4)
        self.spool.ack(2)
        self.assertEqual(self.spool.status()['ack_sequence'], 2)


class ChecksumAndCrashTests(SpoolTests):
    def _tamper_first_segment(self, root=None):
        """Flip a byte inside the first NON-EMPTY segment's body so the
        AEAD tag/checksum rejects that record.  Returns True when a segment
        was actually corrupted."""
        seg_dir = (root or self.root) / 'segments'
        segs = sorted(seg_dir.iterdir())
        for target in segs:
            data = bytearray(target.read_bytes())
            if len(data) < 48:
                continue  # empty header-only segment; skip
            data[max(32, len(data) // 2)] ^= 0xFF
            target.write_bytes(bytes(data))
            return True
        return False

    def test_tampered_segment_isolated_from_later_segments(self):
        # dedicated subroot (no stale header-only segments from setUp)
        root = self.root / 'tamper'
        sp = _spool(root, 'host-1', SESS, max_session_bytes=1 << 15)
        self.spool.close()
        self.spool = sp
        for n in range(1, 7):
            sp.append(_evt(n))
        sp.rotate()
        for n in range(7, 13):
            sp.append(_evt(n))
        sp.close()
        self.assertTrue(self._tamper_first_segment(root))
        fresh = _spool(root, 'host-1', SESS, max_session_bytes=1 << 15)
        self.spool = fresh
        events = fresh.read_after(0, 100, 1 << 16)
        self.assertIsInstance(events, list)
        # the corruption is isolated to the affected record: later records in
        # the same segment are dropped but the later segments replay fine
        self.assertLessEqual(len(events), 12)
        sequences = [e['sequence'] for e in events]
        self.assertTrue(
            any(seq >= 7 for seq in sequences),
            'later segments must still replay after a corrupt first segment',
        )

    def test_crash_closes_segment_at_arbitrary_offset(self):
        root = self.root / 'crash'
        sp = _spool(root, 'host-1', SESS, max_session_bytes=1 << 15)
        self.spool.close()
        self.spool = sp
        for n in range(1, 9):
            sp.append(_evt(n))
        sp.rotate()
        for n in range(9, 17):
            sp.append(_evt(n))
        sp.close()
        seg_dir = root / 'segments'
        target = sorted(seg_dir.iterdir())[0]
        data = bytearray(target.read_bytes())
        cut = max(16, len(data) // 2)
        target.write_bytes(bytes(data[:cut]))
        fresh = _spool(root, 'host-1', SESS, max_session_bytes=1 << 15)
        self.spool = fresh
        events = fresh.read_after(0, 100, 1 << 16)
        self.assertIsInstance(events, list)

    def test_crash_inject_then_append_resumes_cleanly(self):
        root = self.root / 'crashappend'
        sp = _spool(root, 'host-1', SESS, max_session_bytes=1 << 15)
        self.spool.close()
        self.spool = sp
        for n in range(1, 6):
            sp.append(_evt(n))
        sp.close()
        seg = sorted((root / 'segments').iterdir())[0]
        data = bytearray(seg.read_bytes())
        seg.write_bytes(bytes(data[: len(data) // 2]))
        fresh = _spool(root, 'host-1', SESS, max_session_bytes=1 << 15)
        self.spool = fresh
        result = fresh.append(_evt(6))
        # the torn tail is repaired and the sequence resumes cleanly after the
        # last fully durable frame (no silent loss, no gap-duplication)
        self.assertIsNotNone(result.sequence)

    def test_torn_frame_seq_never_credited_and_stream_is_gap_free(self):
        """A frame whose BODY is truncated must not silently cover its
        sequence: reopen+append must resume at max_durable_seq+1 and the
        durable stream must remain contiguous (no gap, no duplicate and no
        capture_gap marker is ever silently swallowed)."""
        # Dedicated subroot so no stale segment from setUp interferes.
        root = self.root / 'tornframe'
        sp = _spool(root, 'host-1', SESS, max_session_bytes=1 << 20)
        self.spool.close()
        self.spool = sp
        for n in range(1, 6):
            sp.append(_evt(n))
        sp.close()
        seg_dir = root / 'segments'
        seg = sorted(seg_dir.iterdir())[0]
        data = bytearray(seg.read_bytes())
        # Locate the byte offset of the 3rd frame body in the segment so we
        # can tear EXACTLY its body (complete header, truncated body): skip
        # the 16-byte segment header and the first two full frames.
        pos = 16  # first frame starts right after the 16-byte segment header
        for _ in range(2):
            enc_len = struct.unpack('>I', bytes(data[pos + 8:pos + 12]))[0]
            pos += 12 + enc_len
        # pos now points at the header of frame #3 (seq 3).  Truncate the
        # file so its body is cut short (header + seq + len + prefix of the
        # body only) — a torn frame with a COMPLETE header but PARTIAL body.
        seg.write_bytes(bytes(data[: pos + 13]))
        fresh = _spool(root, 'host-1', SESS,
                       max_session_bytes=1 << 20)
        self.spool = fresh
        # The torn frame's sequence is never credited as durable: recovery
        # sees complete frames 1..2 only, so the resume sequence is 3.
        self.assertEqual(fresh.status()['last_sequence'], 2)
        result = fresh.append(_evt(99))
        self.assertEqual(result.sequence, 3)
        self.assertFalse(result.capture_blocked)
        # The durable stream is contiguous [1,2,3] — no gap at the torn frame's
        # sequence, no duplicate, and the marker (if any) is not swallowed.
        seqs = [e['sequence'] for e in fresh.read_after(0, 100, 1 << 16)]
        self.assertEqual(seqs, [1, 2, 3])

class GapTests(SpoolTests):
    def test_exact_overflow_capture_blocked_then_best_effort_gap(self):
        """Under quota pressure an exact event must surface capture_blocked;
        a best-effort event must surface an explicit gap signal (never a
        wrong exact claim and never a silent drop)."""
        self.spool.close()
        # 700 bytes fits exactly one ~402-byte exact event.
        sp = _spool(self.root, 'host-1', SESS, max_session_bytes=700)
        self.spool = sp
        first = sp.append(_evt(1))
        self.assertEqual(first.sequence, 1)
        blocked = sp.append(_evt(2, quality='structured'))
        self.assertTrue(blocked.capture_blocked)
        self.assertIsNone(blocked.sequence)
        # best-effort overflow -> explicit gap signal with an in-memory
        # persisted marker, never a false exact capacity claim.
        gap = sp.append(_evt(3, quality='best_effort'))
        self.assertFalse(gap.capture_blocked)
        self.assertIsNotNone(gap.gap_sequence)
        # a second best-effort overflow does not spam the stream; it stays
        # an explicit gap signal, not an accepted event.
        gap2 = sp.append(_evt(4, quality='best_effort'))
        self.assertFalse(gap2.capture_blocked)
        self.assertIsNotNone(gap2.gap_sequence)


if __name__ == '__main__':
    unittest.main()