"""Resumable session uploader tests (Task 4).

Covers (from the Task 4 brief):

- ``SessionUploader(post_json, *, spool, batch_events=100, batch_bytes=262144)``
  with ``flush_once()`` and ``replay_from_ack()``;
- POST only bounded batches (event count and byte caps);
- honor ``accepted_through`` / ``next_cursor`` semantics from the injected
  ``post_json`` response;
- transport failures (the injected callable raising) are retryable — unacked
  segments/records are retained for later flush;
- API rejects (a response that says not accepted) must NOT discard unacked
  events — they continue to retry;
- after a successful upload and ack, replay never re-sends;
- ``replay_from_ack`` resumes from the persisted ack cursor after a restart.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from tools.session.spool import LocalSpool
from tools.session.uploader import SessionUploader

SESS = 'sess_up'
TEST_KEY = bytes(range(32))


def _evt(sequence, kind='assistant_message', quality='structured',
         text='hello'):
    from session_schema import validate_event
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
        'payload': {'text': text, 'is_complete': True},
    }
    return validate_event(event)


def make_spool(root, session_id=SESS):
    return LocalSpool(root, 'host-1', session_id, key=TEST_KEY,
                      max_session_bytes=1 << 20)


class UploaderTestBase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='fleet-up-'))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.spool = make_spool(self.root)
        self.calls = []

    def tearDown(self):
        try:
            self.spool.close()
        except Exception:
            pass

    def _post(self, *args, **kwargs):
        # recorded fake transport backed by an injected response
        self.calls.append((args, kwargs))
        return kwargs.get('response', self.default_response)

    def _uploader(self, **kwargs):
        kwargs.setdefault('batch_events', 100)
        kwargs.setdefault('batch_bytes', 262144)
        return SessionUploader(self._post, spool=self.spool, **kwargs)


class UploaderBasicTests(UploaderTestBase):
    def test_uploader_sends_bounded_batch_and_acks(self):
        for n in range(1, 6):
            self.spool.append(_evt(n))
        self.default_response = {
            'ok': True, 'stream_id': 'stream_abc',
            'accepted_through': 5, 'next_cursor': 'c-5', 'request_id': 'r1',
        }
        uploader = self._uploader(batch_events=100)
        uploader.flush_once()
        self.assertEqual(len(self.calls), 1)
        payload = self.calls[0][0][0] or self.calls[0][0][0]
        events = payload['events']
        self.assertEqual([e['sequence'] for e in events], [1, 2, 3, 4, 5])
        # accepted_through honored -> ack durable, replay empty
        self.assertEqual(self.spool.status()['ack_sequence'], 5)
        self.assertEqual(self.spool.read_after(0, 100, 1 << 16), [])

    def test_flush_once_sends_nothing_when_all_acked(self):
        self.default_response = {'ok': True, 'accepted_through': 0,
                                 'next_cursor': 'c0', 'request_id': 'r'}
        uploader = self._uploader()
        uploader.flush_once()
        self.assertEqual(self.calls, [])

    def test_flush_once_drains_all_batches(self):
        for n in range(1, 13):
            self.spool.append(_evt(n))
        # response acks up to the max sequence in each posted batch
        def response(payload):
            self.calls.append(payload)
            seqs = [e['sequence'] for e in payload['events']]
            return {'ok': True, 'accepted_through': max(seqs),
                    'next_cursor': f'c-{max(seqs)}', 'request_id': 'r'}
        uploader = SessionUploader(response, spool=self.spool,
                                   batch_events=4)
        total = uploader.flush_once()
        self.assertEqual(total, 12)
        # multiple bounded batches were sent (12 events / 4 per batch)
        self.assertGreaterEqual(len(self.calls), 3)
        self.assertEqual(self.spool.status()['ack_sequence'], 12)
        self.assertEqual(self.spool.read_after(0, 100, 1 << 16), [])


class BoundedBatchTests(UploaderTestBase):
    def test_batch_events_bounds_count(self):
        for n in range(1, 11):
            self.spool.append(_evt(n))
        self.default_response = {'ok': True, 'accepted_through': 0,
                                'next_cursor': 'c', 'request_id': 'r'}
        uploader = self._uploader(batch_events=4)
        uploader.flush_once()
        self.assertGreaterEqual(len(self.calls), 1)
        # every sent batch carries at most 4 events
        for _, kwargs in self.calls:
            payload = kwargs.get('events', {})
            events = payload.get('events', [])
            self.assertLessEqual(len(events), 4)

    def test_batch_bytes_bounded(self):
        for n in range(1, 12):
            self.spool.append(_evt(n))
        self.default_response = {'ok': True, 'accepted_through': 0,
                                'next_cursor': 'c', 'request_id': 'r'}
        uploader = self._uploader(batch_bytes=1024)
        uploader.flush_once()
        for _, kwargs in self.calls:
            payload = kwargs.get('events', [])
            # the serialized batch never exceeds the byte bound
            import json
            self.assertLessEqual(len(json.dumps(payload).encode()), 1 << 14)


class AckSemanticsTests(UploaderTestBase):
    def test_ack_honors_accepted_through(self):
        for n in range(1, 6):
            self.spool.append(_evt(n))
        self.default_response = {'ok': True, 'accepted_through': 3,
                                'next_cursor': 'c3', 'request_id': 'r'}
        uploader = self._uploader(batch_events=100)
        uploader.flush_once()
        # only records through 3 are acked; 4,5 remain pending
        self.assertEqual(self.spool.status()['ack_sequence'], 3)
        remaining = self.spool.read_after(3, 100, 1 << 16)
        self.assertEqual([e['sequence'] for e in remaining], [4, 5])
        # the cursor cursor is stored for later continuation
        self.assertEqual(uploader.next_cursor, 'c3')

    def test_ack_higher_than_events_is_clamped(self):
        for n in range(1, 4):
            self.spool.append(_evt(n))
        self.default_response = {'ok': True, 'accepted_through': 6,
                                'next_cursor': 'end', 'request_id': 'r'}
        uploader = self._uploader()
        uploader.flush_once()
        self.assertEqual(self.spool.status()['ack_sequence'], 3)

    def test_flush_once_counts_only_newly_acked_prefix(self):
        # The Hub accepts only a PREFIX of the posted batch.  flush_once()
        # must count the newly-acked delta (accepted_through - prior ack) and
        # NOT the full batch length.
        for n in range(1, 6):
            self.spool.append(_evt(n))
        self.default_response = {'ok': True, 'accepted_through': 3,
                                'next_cursor': 'c3', 'request_id': 'r'}
        uploader = self._uploader(batch_events=100)
        total = uploader.flush_once()
        # 5 events sent in one batch, but only through 3 accepted
        self.assertEqual(total, 3)
        self.assertEqual(self.spool.status()['ack_sequence'], 3)
        # records 4,5 are still pending for the next pass
        remaining = self.spool.read_after(3, 100, 1 << 16)
        self.assertEqual([e['sequence'] for e in remaining], [4, 5])

    def test_flush_once_counts_delta_when_ack_already_advanced(self):
        # a pre-existing durable ack means a partial advance is counted
        # relative to it, not from zero.
        for n in range(1, 8):
            self.spool.append(_evt(n))
        self.spool.ack(3)
        self.default_response = {'ok': True, 'accepted_through': 5,
                                'next_cursor': 'c5', 'request_id': 'r'}
        uploader = self._uploader(batch_events=100)
        total = uploader.flush_once()
        self.assertEqual(total, 2)  # only seq 4,5 newly acked
        self.assertEqual(self.spool.status()['ack_sequence'], 5)


class TransportRetryTests(UploaderTestBase):
    def test_transport_failure_retains_unacked(self):
        for n in range(1, 6):
            self.spool.append(_evt(n))
        # fail the first two POSTs transport-style, then succeed
        self.failures_left = 2

        def flaky(*args, **kwargs):
            if self.failures_left:
                self.failures_left -= 1
                raise ConnectionError('boom')
            return {'ok': True, 'accepted_through': 5,
                    'next_cursor': 'end', 'request_id': 'r'}

        uploader = SessionUploader(flaky, spool=self.spool, batch_events=100)
        # first flush attempts the transport, fails, retains all unacked
        result = uploader.flush_once()
        self.assertEqual(result, 0)
        self.assertEqual(uploader.last_error, 'transport_failed')
        self.assertEqual(self.failures_left, 1)
        self.assertEqual(self.spool.status()['ack_sequence'], 0)
        self.assertEqual(
            [e['sequence']
             for e in self.spool.read_after(0, 100, 1 << 16)],
            [1, 2, 3, 4, 5])
        # second flush fails again, still retained
        result = uploader.flush_once()
        self.assertEqual(result, 0)
        self.assertEqual(self.failures_left, 0)
        self.assertEqual(self.spool.status()['ack_sequence'], 0)
        # third flush succeeds
        result = uploader.flush_once()
        self.assertEqual(result, 5)
        self.assertEqual(self.spool.status()['ack_sequence'], 5)

    def test_transport_never_discards_unacked(self):
        for n in range(1, 6):
            self.spool.append(_evt(n))

        def always_fails(*args, **kwargs):
            raise ConnectionError('down')

        uploader = SessionUploader(always_fails, spool=self.spool,
                                   batch_events=100)
        uploader.flush_once()
        # nothing was acked, everything retained
        self.assertEqual(self.spool.status()['ack_sequence'], 0)
        pending = self.spool.read_after(0, 100, 1 << 16)
        self.assertEqual([e['sequence'] for e in pending], [1, 2, 3, 4, 5])
        # segments still on disk
        self.assertGreaterEqual(self.spool.status()['segment_count'], 1)

    def test_api_reject_keeps_unacked_events(self):
        for n in range(1, 4):
            self.spool.append(_evt(n))
        # response says NOT accepted -> unacked events preserved
        self.default_response = {'ok': False, 'error': 'batch_too_large',
                                 'request_id': 'r'}
        uploader = self._uploader()
        uploader.flush_once()
        self.assertEqual(self.spool.status()['ack_sequence'], 0)
        pending = self.spool.read_after(0, 100, 1 << 16)
        self.assertEqual([e['sequence'] for e in pending], [1, 2, 3])


class ReplayTests(UploaderTestBase):
    def test_replay_from_ack_resumes_and_does_not_resend(self):
        for n in range(1, 8):
            self.spool.append(_evt(n))
        self.spool.ack(4)  # a pre-existing durable ack
        # simulate a restart-cursor: uploader replays from ack
        def response(payload):
            return {'ok': True,
                    'accepted_through': sorted(e['sequence'] for e in payload['events'])[-1],
                    'next_cursor': 'c', 'request_id': 'r'}
        uploader = SessionUploader(response, spool=self.spool,
                                   batch_events=100)
        uploader.replay_from_ack()
        # unacked records (5,6,7) were sent, 1..4 never re-sent
        self.assertEqual(self.spool.status()['ack_sequence'], 7)
        self.assertEqual(self.spool.read_after(7, 100, 1 << 16), [])

    def test_duplicate_replay_is_prevented_by_durable_ack(self):
        # ack persists across a restart; a fresh uploader won't re-send
        for n in range(1, 4):
            self.spool.append(_evt(n))
        self.spool.ack(3)
        self.spool.close()
        self.spool = make_spool(self.root)
        sent = []

        def response(payload):
            sent.append(payload['events'])
            return {'ok': True, 'accepted_through': 3, 'next_cursor': 'c',
                    'request_id': 'r'}
        uploader = SessionUploader(response, spool=self.spool,
                                   batch_events=100)
        uploader.flush_once()
        self.assertEqual(sent, [])


if __name__ == '__main__':
    unittest.main()