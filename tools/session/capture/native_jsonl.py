"""Byte-offset native JSONL tailer with durable checkpoint (Task 7).

This adapter reads a verified native transcript ``.jsonl`` file WITHOUT any
DHCP/subprocess involvement: the caller supplies the file path (never a
command).  It tails strictly by byte offset and persists a durable
checkpoint so a restart resumes exactly where the last complete line ended.

Rules enforced here:

- Byte-offset tailing: ``offset`` is the byte position just past the last
  COMPLETE line that was fully read.  A truncated trailing line (no final
  newline) is kept for a later append to complete, never emitted, and never
  causes the offset to rewind.
- Checkpointing: ``checkpoint()`` returns the current offset; the caller may
  persist it with ``checkpoint_to(path)``.  On restart, ``tailer(path,
  checkpoint_path=...)`` restores the durable offset so already-consumed
  lines are not re-read.
- Malformed lines affect only themselves: a line that is not valid JSON, is
  not a JSON object, or exceeds ``max_line_bytes`` is skipped and its bytes
  ARE consumed (so the tail can never loop on the same garbage).  The line is
  never emitted and never leaks into diagnostics.
- The stream never treats non-JSON noise as transcript.
- The maximum bytes read per file tail is bounded by ``read_limit_bytes``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

MAX_LINE_BYTES = 262144  # 256 KiB per line; larger lines are skipped
READ_LIMIT_BYTES = 4 << 20  # bound for one tail() call (4 MiB)
_CHUNK = 65536

# Stable bounded diagnostics (never raw output / exception text).
CODE_IO_ERROR = "native_jsonl_io"


@dataclass
class JsonlTailer:
    """Byte-offset JSONL tailer with a durable checkpoint.

    ``path`` is the JSONL file to tail; ``offset`` restores a previous
    checkpoint.  ``read()`` consumes from ``offset`` to a complete line
    boundary (or the file end), returning decoded JSON objects.
    ``disconnected`` is True when the current tail is an incomplete line and a
    user/observer should expect a later completion.
    """

    path: str
    offset: int = 0
    max_line_bytes: int = MAX_LINE_BYTES
    read_limit_bytes: int = READ_LIMIT_BYTES
    disconnected: bool = False
    last_error: str | None = None

    def read(self) -> list:
        """Read and decode complete JSON object lines after ``offset``.

        Returns a list of JSON objects in file order.  Non-object JSON,
        malformed lines, and oversized lines are skipped (their bytes still
        consumed so the offset never loops); a truncated trailing line is
        kept for a later append.  Never raises: read errors surface a bounded
        ``last_error`` code and stop the read.
        """
        try:
            size = os.path.getsize(self.path)
        except OSError:
            self.last_error = CODE_IO_ERROR
            return []
        if size < self.offset:
            # The file shrank (truncation): fall back to the beginning.  This
            # is a safe, bounded rewind — some loggers truncate+reopen.
            # ``size == offset`` is a clean EOF, never a rewind.
            self.offset = 0

        out: list = []
        consumed = 0
        try:
            handle = open(self.path, "rb")
        except OSError:
            self.last_error = CODE_IO_ERROR
            return []
        try:
            handle.seek(self.offset)
            while consumed < self.read_limit_bytes:
                raw = handle.readline(_CHUNK * 4)
                if raw == b"":
                    break
                consumed += len(raw)
                stripped = raw.rstrip(b"\r\n")

                if not stripped:
                    self.offset += len(raw)
                    continue

                line_has_newline_end = raw.endswith((b"\n", b"\r"))
                oversized = len(stripped) >= self.max_line_bytes

                # A line read with a byte limit may actually be a huge line
                # split by the chunk boundary and NOT a truncated tail.  When
                # the chunk has NO newline and its stripped size is already at
                # max_line_bytes, the line is oversized/mid-line: consume the
                # REST of the oversized line so the byte offset is not left
                # mid-line, then skip it.
                if (not line_has_newline_end
                        and oversized):
                    self.last_error = "nlj_oversized_line"
                    # the FIRST chunk of the oversized line was already read;
                    # its bytes must be accounted for in the durable offset
                    self.offset += len(raw)
                    # swallow the rest of the oversized line (bounded)
                    while consumed < self.read_limit_bytes:
                        chunk = handle.readline(_CHUNK * 4)
                        if chunk == b"":
                            break
                        consumed += len(chunk)
                        self.offset += len(chunk)
                        if chunk.endswith((b"\n", b"\r")):
                            break
                    continue

                if oversized:
                    # a single oversized but complete line (was read with a
                    # newline): skip it, but still advance the offset
                    self.offset += len(raw)
                    self.last_error = "nlj_oversized_line"
                    continue

                # a truncated trailing line (no newline) is kept for later
                if not line_has_newline_end:
                    self.disconnected = True
                    break

                self.offset += len(raw)
                self.disconnected = False
                try:
                    text = stripped.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                try:
                    obj = json.loads(text)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                out.append(obj)
            return out
        finally:
            try:
                handle.close()
            except Exception:
                pass

    def checkpoint(self) -> dict:
        """Return a bounded snapshot of the durable tail offset."""
        return {"offset": self.offset}

    def checkpoint_to(self, path: str) -> dict:
        """Persist the checkpoint to ``path`` atomically (tmp + rename)."""
        import tempfile

        tmp = f"{path}.tmp-{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.checkpoint(), fh, ensure_ascii=True, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return self.checkpoint()

    @classmethod
    def from_checkpoint(cls, path: str, checkpoint_path: str | None = None,
                        **kw) -> "JsonlTailer":
        """Restore a tailer from a persisted checkpoint when one exists."""
        offset = 0
        if checkpoint_path is not None and os.path.exists(checkpoint_path):
            try:
                with open(checkpoint_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                offset = int(data.get("offset", 0))
            except (OSError, ValueError, TypeError):
                offset = 0
        return cls(path, offset=max(0, offset), **kw)


def tailer(path: str, checkpoint_path: str | None = None, **kw) -> JsonlTailer:
    """Create a JSONL tailer with an optional durable checkpoint."""
    return JsonlTailer.from_checkpoint(path, checkpoint_path=checkpoint_path,
                                       **kw)


__all__ = ["MAX_LINE_BYTES", "JsonlTailer", "tailer"]