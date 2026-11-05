"""Shared helpers for append-only JSONL files."""

import json
import os
from pathlib import Path


def repair_jsonl_tail(path: Path) -> bool:
    """Repair a torn final JSONL record left by a crashed append.

    A last line without ``\\n`` is either a complete record whose newline was
    lost (terminate it) or a partial write, possibly ending inside a multibyte
    UTF-8 sequence (truncate back to the last complete line). Without this the
    next append concatenates onto the fragment and corrupts both rows.

    Returns True when the file was modified. A missing file is a no-op.
    """
    try:
        with open(path, "r+b") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if not size:
                return False
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) == b"\n":
                return False
            handle.seek(0)
            content = handle.read()
            tail_start = content.rfind(b"\n") + 1
            tail = content[tail_start:]
            try:
                json.loads(tail.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                handle.truncate(tail_start)
            else:
                handle.seek(0, os.SEEK_END)
                handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
            return True
    except FileNotFoundError:
        return False


def decode_jsonl_object(line: bytes):
    """Decode one JSONL line into a dict, or None when malformed."""
    try:
        parsed = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


__all__ = ["decode_jsonl_object", "repair_jsonl_tail"]
