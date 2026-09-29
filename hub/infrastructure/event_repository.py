"""JSONL event repository extracted from the legacy ``hub.events`` module.

Owns only persistence semantics, injected with an explicit ``Path``:

* appends one JSON event per line, creating the parent directory as needed
* rotation: when the file exceeds ``max_bytes`` the tail ``keep_lines`` rows
  are retained via atomic replace (``NamedTemporaryFile`` in the same dir +
  ``flush`` + ``fsync`` + ``os.replace``)
* malformed lines are skipped silently on read
* a ``threading.Lock`` serializes append + rotation, and reads snapshot the
  file content under the same lock
* events keep the legacy shape and gain a durable, monotonic ``event_seq``
  cursor used by SSE replay; timestamp replay remains available for old clients

``hub.events`` becomes a thin compatibility facade over a default instance of
this repository; new services inject a repository built on an explicit
``Path``.
"""

import json
import math
import os
import tempfile
import threading
from pathlib import Path

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

    def append(self, event: dict) -> int:
        """Append one event and return its durable sequence.

        Failures are intentionally NOT swallowed here so the caller (the
        publisher) can decide whether to raise or degrade gracefully.
        """
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
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
        with self._lock:
            return self._read_lines()[-bounded:]

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
        with self._lock:
            out = []
            for ev in self._read_lines():
                if len(out) >= bounded:
                    break
                try:
                    event_ts = float(ev.get("ts") or 0)
                except (TypeError, ValueError, OverflowError):
                    continue
                if event_ts > cursor_ts:
                    out.append(ev)
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
        with self._lock:
            out = []
            for ev in self._read_lines():
                if len(out) >= bounded:
                    break
                try:
                    event_seq = int(ev.get("event_seq"))
                except (TypeError, ValueError):
                    continue
                if event_seq > cursor:
                    out.append(ev)
            return out

    # -- internal -----------------------------------------------------------

    def _read_lines(self) -> list[dict]:
        """Parse all lines; silently skip malformed ones. Caller holds lock."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except (FileNotFoundError, UnicodeDecodeError):
            return []
        out = []
        for line in text.splitlines():
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    out.append(parsed)
            except (TypeError, ValueError):
                continue
        return out

    def _max_event_sequence_locked(self) -> int:
        """Read the current durable high-water mark; caller holds ``_lock``."""
        maximum = 0
        for ev in self._read_lines():
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
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return
        keep = lines[-self.keep_lines:]
        tmp = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.path.parent, prefix="rotate-",
            suffix=".tmp", delete=False,
        )
        tmp_name = tmp.name
        try:
            with tmp:
                tmp.write("\n".join(keep) + "\n")
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, self.path)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
