"""Application-layer event publisher.

Coordinates event creation, best-effort persistence, and live distribution:

* ``emit`` builds the canonical event dict (``event``, ``machine``, ``ts``,
  ``changes``, ``extra``), persists it via the injected repository
  (best-effort: repository failures are logged, never raised into callers),
  then dispatches a snapshot copy to each subscriber.
* ``subscribe`` returns an idempotent unsubscribe closure.
* ``subscribe_sse`` registers one queue/callback lifecycle object; queue
  overflow catches ``queue.Full`` and returns without blocking ``emit``.
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

    def __init__(self, event_repository, *, queue_size=200) -> None:
        self.repository = event_repository
        self.queue_size = int(queue_size)
        self._subscribers = []
        self._sse_pairs = []  # (queue, callback) lifecycle pairs
        self._lock = threading.Lock()

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
        self._persist(ev)
        self._distribute(ev)
        return ev

    def _persist(self, ev: dict) -> None:
        try:
            self.repository.append(ev)
        except Exception:
            logger.exception("event persistence failed; dropping event %r", ev.get("event"))

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

        def _cb(ev):
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass

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
