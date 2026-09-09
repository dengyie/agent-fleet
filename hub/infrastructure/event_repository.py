"""JSONL event repository extracted from the legacy ``hub.events`` module.

Owns only persistence semantics, injected with an explicit ``Path``:

* appends one JSON event per line, creating the parent directory as needed
* rotation: when the file exceeds ``max_bytes`` the tail ``keep_lines`` rows
  are retained via atomic replace (``NamedTemporaryFile`` in the same dir +
  ``flush`` + ``fsync`` + ``os.replace``)
* malformed lines are skipped silently on read
* a ``threading.Lock`` serializes append + rotation, and reads snapshot the
  file content under the same lock
* events keep the legacy shape: ``event``, ``machine``, ``ts``, ``changes``,
  ``extra``

``hub.events`` becomes a thin compatibility facade over a default instance of
this repository; new services inject a repository built on an explicit
``Path``.
"""

import json
import os
import tempfile
import threading
from pathlib import Path

MAX_EVENT_BYTES = 1_000_000
KEEP_EVENT_LINES = 1000


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

    def append(self, event: dict) -> None:
        """Append one event to the JSONL; rotate if the file is oversized.

        Failures are intentionally NOT swallowed here so the caller (the
        publisher) can decide whether to raise or degrade gracefully.
        """
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._rotate_if_needed()

    def read_recent(self, limit: int = 50) -> list[dict]:
        """Return the most recent *limit* events, newest last."""
        if not self.path.exists():
            return []
        with self._lock:
            return self._read_lines()[-limit:]

    def read_since(self, ts: float, limit: int = 200) -> list[dict]:
        """Return events with ``ts > ts``, up to *limit*, oldest first."""
        if not self.path.exists():
            return []
        with self._lock:
            out = []
            for ev in self._read_lines():
                if len(out) >= limit:
                    break
                if float(ev.get("ts") or 0) > ts:
                    out.append(ev)
            return out

    # -- internal -----------------------------------------------------------

    def _read_lines(self) -> list[dict]:
        """Parse all lines; silently skip malformed ones. Caller holds lock."""
        try:
            text = self.path.read_text()
        except FileNotFoundError:
            return []
        out = []
        for line in text.splitlines():
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out

    def _rotate_if_needed(self) -> None:
        """Truncate an oversized JSONL to the tail ``keep_lines`` rows."""
        try:
            if not self.path.exists() or self.path.stat().st_size <= self.max_bytes:
                return
        except OSError:
            return
        try:
            lines = self.path.read_text().splitlines()
        except OSError:
            return
        keep = lines[-self.keep_lines:]
        tmp = tempfile.NamedTemporaryFile(
            mode="w", dir=self.path.parent, prefix="rotate-",
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
