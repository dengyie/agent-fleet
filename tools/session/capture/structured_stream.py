"""Structured-event stream parser for verified CLI streams (Task 7).

A CLI that advertises ``structured_stream`` in its capability manifest emits a
byte stream of JSON objects (e.g. Claude Code ``--output-format=stream-json``
or Codex ``--json``).  This adapter parses that stream and refuses to treat
non-JSON bytes as transcript.

Rules enforced here:

- Every input line is validated with :func:`json.loads`; malformed / non-JSON
  lines are REJECTED and never emitted (never silently treated as text).
- A line exceeding ``max_chars`` is rejected rather than truncated into a
  transcript row.
- Returned values are JSON objects (``dict``); arrays, scalars and malformed
  JSON are skipped.
- The parser is deterministic and synchronous (no subprocess).  The caller
  injects an iterable of lines.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any

MAX_LINE_CHARS = 262144  # 256 KiB per line; larger lines rejected


def iter_json(lines: Iterable[str | bytes], *,
              max_chars: int = MAX_LINE_CHARS) -> Iterator[dict]:
    """Yield JSON OBJECTS from ``lines``, rejecting all noise.

    Each input is decoded (bytes UTF-8), stripped of a trailing newline,
    parsed as JSON, and yielded only when it is a ``dict``.  Malformed JSON,
    non-dict JSON, and oversized lines never appear in the output stream.
    """
    for line in lines:
        text = line
        if isinstance(text, (bytes, bytearray)):
            try:
                text = bytes(text).decode("utf-8")
            except UnicodeDecodeError:
                continue
        if not isinstance(text, str):
            continue
        text = text.rstrip("\r\n")
        if not text:
            continue
        if len(text) > max_chars:
            continue
        try:
            obj = json.loads(text)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        yield obj


__all__ = ["MAX_LINE_CHARS", "iter_json"]