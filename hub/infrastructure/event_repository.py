"""JSONL event repository extracted from the legacy ``hub.events`` module.

Owns only persistence semantics, injected with an explicit ``Path``:

* appends one JSON event per line, creating the parent directory as needed
* rotation: when the file exceeds ``max_bytes`` the tail ``keep_lines`` rows
  are retained via atomic replace (``NamedTemporaryFile`` in the same dir +
  ``flush`` + ``fsync`` + ``os.replace``)
* a torn final record left by a crashed append (including one cut inside a
  multibyte UTF-8 sequence) is repaired before the first append, so the next
  row never concatenates onto a fragment and the ``event_seq`` high-water mark
  is never lost to one undecodable byte
* lines are decoded one by one as bytes; malformed lines are skipped and
  counted (``malformed_lines``) instead of discarding the whole log
* a ``threading.Lock`` serializes append + rotation; reads use a separate
  lock and an incremental parse cache keyed by inode/offset, so replay never
  re-parses the whole file while blocking appenders
* events keep the legacy shape and gain a durable, monotonic ``event_seq``
  cursor used by SSE replay; timestamp replay remains available for old clients

``hub.events`` becomes a thin compatibility facade over a default instance of
this repository; new services inject a repository built on an explicit
``Path``.
"""

import json
import logging
import math
import os
import tempfile
import threading
from pathlib import Path

from hub.infrastructure.jsonl import decode_jsonl_object, repair_jsonl_tail

_log = logging.getLogger(__name__)

MAX_EVENT_BYTES = 1_000_000
KEEP_EVENT_LINES = 1000
MAX_READ_EVENTS = 1000


class JsonlEventRepository:
    """Append-only JSONL event log with bounded rotation.

    Constructor-injected path keeps the persistence layer free of module-level
    path lookups and the Flask stack.
    """

    def __init__(self, path: Path, *, max_bytes=MAX_EVENT_BYTES,
                 keep_lines=KEEP_EVENT_LINES) -> None:
        self.path = Path(path)
        self.max_bytes = int(max_bytes)
        self.keep_lines = int(keep_lines)
        self._lock = threading.Lock()
        self._next_event_seq = None
        self._tail_repaired = False
        # Incremental read cache: parsed events of the complete lines in
        # [0, _cache_offset) of the file identified by _cache_identity.
        self._read_lock = threading.Lock()
        self._cache_identity = None
        self._cache_offset = 0
        self._cache_events: list[dict] = []
        self.malformed_lines = 0

    def append(self, event: dict) -> int:
        """Append one event and return its durable sequence.

        Failures are intentionally NOT swallowed here so the caller (the
        publisher) can decide whether to raise or degrade gracefully.
        """
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self._tail_repaired:
                # A crash mid-append leaves a torn last row; appending onto it
                # would corrupt the fragment and the new row together.
                if repair_jsonl_tail(self.path):
                    _log.warning("repaired torn tail of event log %s", self.path)
                self._tail_repaired = True
            if self._next_event_seq is None:
                self._next_event_seq = self._max_event_sequence_locked()
            next_event_seq = self._next_event_seq + 1
            # The repository is the persistence owner of the cursor.  Mutating
            # the caller's event keeps the live publisher payload identical to
            # the durable JSONL row without trusting a stale caller sequence.
            event["event_seq"] = next_event_seq
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(event, ensure_ascii=False) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
            except OSError:
                # A failed write may have left a partial row: repair before
                # the next append.
                self._tail_repaired = False
                # A write/fsync error can happen after the kernel accepted
                # the complete JSONL row.  Re-scan the file before bubbling
                # the error so the next append cannot reuse a cursor already
                # visible on disk, while a pre-write failure still leaves the
                # previous high-water mark intact.
                self._next_event_seq = max(
                    self._next_event_seq or 0,
                    self._max_event_sequence_locked(),
                )
                raise
            # Rotation is housekeeping after the append.  If the directory is
            # temporarily read-only/full, the event is still durable in the
            # JSONL and its sequence must be committed; propagating the
            # rotation error would make the publisher expose an ephemeral row
            # and reuse the same cursor on the next append.
            try:
                self._rotate_if_needed()
            except OSError:
                # The event has already been appended; leave it in place and
                # let the next append retry housekeeping.
                pass
            # Commit the in-memory high-water mark only after the append has
            # completed.  A failed open/write must not burn a replay cursor.
            self._next_event_seq = next_event_seq
            return next_event_seq

    def read_recent(self, limit: int = 50) -> list[dict]:
        """Return the most recent *limit* events, newest last."""
        if not self.path.exists():
            return []
        try:
            bounded = min(int(limit), MAX_READ_EVENTS)
        except (TypeError, ValueError):
            bounded = 50
        if bounded <= 0:
            return []
        with self._read_lock:
            return [dict(ev) for ev in self._cached_events_locked()[-bounded:]]

    def read_since(self, ts: float, limit: int = 200) -> list[dict]:
        """Return events with ``ts > ts``, up to *limit*, oldest first."""
        if not self.path.exists():
            return []
        try:
            cursor_ts = float(ts)
            if not math.isfinite(cursor_ts):
                cursor_ts = 0.0
        except (TypeError, ValueError, OverflowError):
            cursor_ts = 0.0
        try:
            bounded = min(int(limit), MAX_READ_EVENTS)
        except (TypeError, ValueError):
            bounded = 200
        if bounded <= 0:
            return []
        with self._read_lock:
            out = []
            for ev in self._cached_events_locked():
                if len(out) >= bounded:
                    break
                try:
                    event_ts = float(ev.get("ts") or 0)
                except (TypeError, ValueError, OverflowError):
                    continue
                if event_ts > cursor_ts:
                    out.append(dict(ev))
            return out

    def read_since_sequence(self, sequence: int, limit: int = 200) -> list[dict]:
        """Return events with durable ``event_seq > sequence``.

        Rows written by older releases without a sequence are deliberately
        excluded; timestamp replay remains the compatibility path for those
        clients and rows.
        """
        if not self.path.exists():
            return []
        try:
            cursor = max(0, int(sequence))
        except (TypeError, ValueError):
            cursor = 0
        try:
            bounded = min(int(limit), MAX_READ_EVENTS)
        except (TypeError, ValueError):
            bounded = 200
        if bounded <= 0:
            return []
        with self._read_lock:
            out = []
            for ev in self._cached_events_locked():
                if len(out) >= bounded:
                    break
                try:
                    event_seq = int(ev.get("event_seq"))
                except (TypeError, ValueError):
                    continue
                if event_seq > cursor:
                    out.append(dict(ev))
            return out

    # -- internal -----------------------------------------------------------

    def _read_lines(self) -> list[dict]:
        """Parse all complete lines, skipping (and counting) malformed ones."""
        with self._read_lock:
            return [dict(ev) for ev in self._cached_events_locked()]

    def _cached_events_locked(self) -> list[dict]:
        """Bring the parse cache up to date; caller holds ``_read_lock``.

        Only newline-terminated lines are consumed, so a row being appended
        concurrently (or a torn tail) is picked up once it is complete. A new
        inode (rotation) or a shrunk file (tail repair) resets the cache.
        """
        try:
            handle = open(self.path, "rb")
        except FileNotFoundError:
            self._reset_cache_locked(None)
            return self._cache_events
        with handle:
            stat = os.fstat(handle.fileno())
            identity = (stat.st_dev, stat.st_ino)
            if identity != self._cache_identity or stat.st_size < self._cache_offset:
                self._reset_cache_locked(identity)
            if stat.st_size == self._cache_offset:
                return self._cache_events
            handle.seek(self._cache_offset)
            chunk = handle.read(stat.st_size - self._cache_offset)
        end = chunk.rfind(b"\n")
        if end < 0:
            return self._cache_events
        bad = 0
        for line in chunk[:end].split(b"\n"):
            if not line.strip():
                continue
            parsed = decode_jsonl_object(line)
            if parsed is None:
                bad += 1
            else:
                self._cache_events.append(parsed)
        self._cache_offset += end + 1
        if bad:
            self.malformed_lines += bad
            _log.warning("skipped %d malformed line(s) in event log %s", bad, self.path)
        return self._cache_events

    def _reset_cache_locked(self, identity) -> None:
        self._cache_identity = identity
        self._cache_offset = 0
        # Rebind (never clear in place) so no reader sees a half-reset list.
        self._cache_events = []
        self.malformed_lines = 0

    def _max_event_sequence_locked(self) -> int:
        """Read the current durable high-water mark; caller holds ``_lock``."""
        maximum = 0
        with self._read_lock:
            events = self._cached_events_locked()
        for ev in events:
            try:
                maximum = max(maximum, int(ev.get("event_seq")))
            except (TypeError, ValueError):
                continue
        return maximum

    def _rotate_if_needed(self) -> None:
        """Truncate an oversized JSONL to the tail ``keep_lines`` rows."""
        try:
            if not self.path.exists() or self.path.stat().st_size <= self.max_bytes:
                return
        except OSError:
            return
        try:
            raw = self.path.read_bytes()
        except OSError:
            return
        # Work in bytes: one undecodable line must not block rotation (the
        # file would grow without bound); such lines are dropped here.
        lines = [line for line in raw.split(b"\n")
                 if line.strip() and decode_jsonl_object(line) is not None]
        keep = lines[-self.keep_lines:]
        tmp = tempfile.NamedTemporaryFile(
            mode="wb", dir=self.path.parent, prefix="rotate-",
            suffix=".tmp", delete=False,
        )
        tmp_name = tmp.name
        try:
            with tmp:
                tmp.write(b"".join(line + b"\n" for line in keep))
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, self.path)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
