"""Application-layer event publisher.

Coordinates event creation, durable persistence, and live distribution:

* ``emit`` builds the canonical event dict, then serializes persistence,
  durable sequencing, and live distribution in one critical section.
* A persistence failure still reaches live subscribers as an ephemeral event,
  but never consumes or exposes a durable ``event_seq`` cursor.
* ``subscribe`` returns an idempotent unsubscribe closure.
* ``subscribe_sse`` registers one queue/callback lifecycle object; queue
  overflow clears the stale queue, inserts a gap sentinel, and lets the SSE
  transport close so the client can replay from its last confirmed sequence.
* instances never share subscriber lists.

`EventRepository` follows the ``hub.domain.events.EventRepository`` protocol.
"""

import logging
import queue
import threading
import time

logger = logging.getLogger(__name__)


class EventPublisher:
    """Create, persist, and distribute hub events to in-process subscribers."""

    SSE_GAP = object()

    def __init__(self, event_repository, *, queue_size=200) -> None:
        self.repository = event_repository
        self.queue_size = int(queue_size)
        self._subscribers = []
        self._sse_pairs = []  # (queue, callback) lifecycle pairs
        self._lock = threading.Lock()
        # This ordering boundary covers persistence, sequencing, and delivery.
        self._emit_lock = threading.RLock()
        self._next_local_event_seq = 0

    # -- event creation -----------------------------------------------------

    def emit(self, event_type, machine=None, changes=None, snapshot=None,
             **extra) -> dict:
        """Create and distribute one event. Returns the event dict."""
        if snapshot is not None:
            extra["snapshot"] = snapshot
        ev = {
            "event": event_type,
            "machine": machine,
            "ts": time.time(),
            "changes": list(changes or []),
            "extra": extra,
        }
        with self._emit_lock:
            persisted = self._persist(ev)
            if persisted:
                # Compatibility repositories do not assign a sequence; use a
                # local durable cursor only after append succeeds.
                if (not isinstance(ev.get("event_seq"), int)
                        or ev["event_seq"] <= 0):
                    self._next_local_event_seq += 1
                    ev["event_seq"] = self._next_local_event_seq
                else:
                    self._next_local_event_seq = max(
                        self._next_local_event_seq, ev["event_seq"])
            else:
                # A repository may mutate before raising; discard any
                # provisional value so it cannot become an SSE cursor.
                ev.pop("event_seq", None)
                ev["durable"] = False
            self._distribute(ev)
        return ev

    def _persist(self, ev: dict) -> bool:
        try:
            result = self.repository.append(ev)
            if (not isinstance(ev.get("event_seq"), int)
                    and isinstance(result, int) and result > 0):
                ev["event_seq"] = result
            return True
        except Exception:
            logger.exception(
                "event persistence failed; live event is ephemeral %r",
                ev.get("event"))
            return False

    def _distribute(self, ev: dict) -> None:
        with self._lock:
            subs = list(self._subscribers)
        for cb in subs:
            try:
                cb(ev)
            except Exception:
                logger.exception("event subscriber failed for %r", ev.get("event"))

    # -- subscription -------------------------------------------------------

    def subscribe(self, callback):
        """Register an event subscriber. Returns an idempotent unsubscribe."""
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe():
            with self._lock:
                try:
                    self._subscribers.remove(callback)
                except ValueError:
                    pass

        return unsubscribe

    def subscribe_sse(self, *, queue_size=None):
        """Register an SSE queue subscriber.

        Returns ``(queue.Queue, unsubscribe)``. When the queue is full the
        event is dropped (realtime streams tolerate loss, never block emit).
        ``queue_size`` is a compatibility override for the legacy facade;
        the publisher instance remains the owner of event delivery.
        """
        size = self.queue_size if queue_size is None else int(queue_size)
        q = queue.Queue(maxsize=size)

        gap = False
        delivery_lock = threading.Lock()

        def _cb(ev):
            nonlocal gap
            with delivery_lock:
                if gap:
                    return
                try:
                    q.put_nowait(ev)
                except queue.Full:
                    # A consumer that cannot keep up must never receive a
                    # partial stream. Drop all stale rows, publish one gap,
                    # and let HTTP close so the client replays by event_seq.
                    while True:
                        try:
                            q.get_nowait()
                        except queue.Empty:
                            break
                    try:
                        q.put_nowait(self.SSE_GAP)
                    except queue.Full:
                        pass
                    gap = True

        with self._lock:
            self._sse_pairs.append((q, _cb))
            self._subscribers.append(_cb)

        def unsubscribe():
            with self._lock:
                try:
                    self._sse_pairs.remove((q, _cb))
                except ValueError:
                    pass
                try:
                    self._subscribers.remove(_cb)
                except ValueError:
                    pass

        return q, unsubscribe

    # -- reads --------------------------------------------------------------

    def read_recent(self, limit=50):
        """Return recent persisted events; a failing read degrades to [].

        Storage read errors are isolated so ``/api/events`` never raises; live
        subscriber distribution is untouched by this path.
        """
        try:
            return self.repository.read_recent(limit)
        except Exception:
            logger.exception("event read failed; returning empty recent list")
            return []

    def read_since(self, ts, limit=200):
        """Return events after ``ts`` for SSE replay; a failing read degrades
        to an empty list so the stream stays connected without exceptions."""
        try:
            return self.repository.read_since(ts, limit)
        except Exception:
            logger.exception("event read failed; returning empty replay list")
            return []

    def read_since_sequence(self, sequence, limit=200):
        """Return durable events after an integer event sequence cursor."""
        reader = getattr(self.repository, "read_since_sequence", None)
        if not callable(reader):
            return []
        try:
            return reader(sequence, limit)
        except Exception:
            logger.exception(
                "event sequence replay read failed; returning empty replay list")
            return []
