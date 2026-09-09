"""Resumable bounded uploader for the local session spool (Task 4).

This module is deliberately decoupled from any concrete transport: the
caller (e.g. the Session Bridge in a later task) injects a ``post_json``
callable that performs the network POST and returns a response Mapping.  The
uploader itself is transport-agnostic, never imports requests/flask, never
reuses Runner result files or heartbeat buffers, and keeps its persisted
cursor in the spool checkpoint.

Response contract honored here (the concrete fields from the Task 6 ingest
contract):

- ``accepted_through`` — the highest event ``sequence`` that the Hub durably
  accepted; the uploader advances the spool ack cursor to that value so
  replay never re-sends those records and the spool compacts acked segments.
- ``next_cursor`` — an opaque continuation token returned by the Hub; it is
  stored and surfaced on the uploader for the next POST (this task does not
  define a real endpoint, so it is kept as an opaque bounded string only).

Failure semantics:

- A transport failure (the injected ``post_json`` raising) is retryable:
  nothing is acked and nothing is discarded — unacked segments/records stay
  durable for a later ``flush_once()``/``replay_from_ack()``.
- An API reject (a response that is not accepted, e.g. ``ok=False`` or a
  missing ``accepted_through``) must NOT discard unacked events: the batch is
  retained and the cursor stays, so a later attempt can retry.

Bounds: batches never exceed ``batch_events`` events or ``batch_bytes``
serialized bytes; ``read_after`` bounds are propagated from the spool; and
public status output is leak-free (no paths, exception text or payloads).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tools.session.spool import LocalSpool

DEFAULT_BATCH_EVENTS = 100
DEFAULT_BATCH_BYTES = 262144


class UploadTransportError(RuntimeError):
    """Stable bounded transport diagnostic — carries only a short code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

    def __str__(self) -> str:
        return self.code


class SessionUploader:
    """Ships bounded batches of unacked spool events to the Hub.

    ``post_json(payload: Mapping) -> Mapping`` is the injected transport: it
    performs the actual POST and returns a response Mapping with the
    ``accepted_through`` / ``next_cursor`` semantics above.  The uploader
    NEVER reuses Runner result files or heartbeat buffers.
    """

    def __init__(
        self,
        post_json,
        *,
        spool: LocalSpool,
        batch_events: int = DEFAULT_BATCH_EVENTS,
        batch_bytes: int = DEFAULT_BATCH_BYTES,
    ):
        if not callable(post_json):
            raise TypeError("post_json must be callable")
        self._post_json = post_json
        self._spool = spool
        self._batch_events = max(1, int(batch_events))
        self._batch_bytes = max(1, int(batch_bytes))
        self._next_cursor: str | None = None
        self.last_error: str | None = None

    # -- public -------------------------------------------------------------

    @property
    def spool(self) -> LocalSpool:
        return self._spool

    @property
    def next_cursor(self) -> str | None:
        """Opaque continuation token from the last accepted response."""
        return self._next_cursor

    def flush_once(self) -> int:
        """Upload ALL pending unacked records in bounded batches.

        Returns the number of records successfully acknowledged across all
        batches sent in this pass.  A retryable failure (transport raise or
        API reject) stops the pass early with the remaining records retained
        durably, so a later ``flush_once`` / ``replay_from_ack`` resumes
        exactly where it failed.
        """
        total = 0
        while True:
            cursor = self._spool.status()["ack_sequence"]
            pending = self._spool.read_after(cursor, self._batch_events,
                                             self._batch_bytes)
            if not pending:
                self.last_error = None
                return total
            acked = self._upload_pending(pending)
            if acked == 0:
                # retryable failure: nothing durable changed; stop the pass.
                return total
            total += acked
            # If this pass is still below the batch caps and no records
            # remain, the next loop iteration returns early.

    def replay_from_ack(self) -> int:
        """Send every unacknowledged record after the persisted ack
        cursor (resume after a restart).  Bounded like flush_once."""
        return self.flush_once()

    # -- internals -----------------------------------------------------------

    def _upload_pending(self, pending: list) -> int:
        """Upload one bounded batch; returns records acknowledged in this
        call (0 on any retryable failure)."""
        payload = {"events": pending}
        response = self._transport_send(payload)
        return self._apply_response(payload, response)

    def _transport_send(self, payload: dict) -> Mapping:
        """Call the injected transport, normalizing retryable failures.

        On any transport/connection failure or a non-Mapping response the
        uploader records a stable short code in ``last_error`` and returns a
        retryable sentinel — it NEVER advances the spool cursor, so unacked
        records are retained for a later attempt.
        """
        try:
            raw = self._post_json(payload)
        except UploadTransportError as exc:
            self.last_error = exc.code
            return {}
        except Exception:
            self.last_error = "transport_failed"
            return {}
        if isinstance(raw, Mapping):
            return raw
        self.last_error = "invalid_response"
        return {}

    def _apply_response(self, payload: dict, response: Mapping) -> int:
        accepted_through = response.get("accepted_through")
        ok = response.get("ok", True)
        if accepted_through is None or ok is False:
            # Not accepted: do NOT discard unacked events; keep the cursor
            # unchanged so the next attempt retries them.
            return 0
        try:
            accepted = int(accepted_through)
        except (TypeError, ValueError):
            return 0
        ack_before = self._spool.status()["ack_sequence"]
        if accepted > ack_before:
            self._spool.ack(accepted)
            self._next_cursor = self._safe_cursor(response.get("next_cursor"))
            # The true newly-acked count is the durable delta measured AFTER
            # the spool clamps (ack beyond the last durable record is
            # clamped).  When the Hub accepts only a prefix of the batch
            # (e.g. accepted_through=3 for events 1..5) that prefix is the
            # newly-acked delta, never the whole batch length.
            return self._spool.status()["ack_sequence"] - ack_before
        return 0

    @staticmethod
    def _safe_cursor(value: Any) -> str | None:
        if value is None:
            return None
        try:
            return str(value)[:256]
        except Exception:
            return None


__all__ = [
    "DEFAULT_BATCH_BYTES",
    "DEFAULT_BATCH_EVENTS",
    "SessionUploader",
    "UploadTransportError",
]