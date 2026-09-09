"""Probe definitions for the pi CLI.

Capability claims derive *only* from observed ``--version`` / ``--help``
output, matched at strict token boundaries (same grammar discipline as
codex/claude).  From pi 0.84.4's help text:

- ``-p`` / ``--print``  → non-interactive spawn;
- ``--mode`` with the exact value ``json`` → structured stream;
- ``--session`` / ``--session-id`` → deterministic native session file
  selection (native transcript); ``--session-dir``/``--fork`` never match;
- ``--resume``/``-r`` is an INTERACTIVE session picker — a help-text mention
  never claims resume, and pi is not in ``_RESUME_VERIFIABLE_FAMILIES``, so
  ``resume`` stays False in this phase;
- ``--no-session`` is deliberately not a capability token.

``thinking`` blocks in pi transcripts are internal surfaces (dropped by the
bridge mapper), matching the claude/codex convention.
"""

import re
from typing import Pattern

# Strict delimiter closing a matched flag/value token: end-of-text, whitespace,
# or a small set of non-identifier punctuation. ``-``, ``.``, ``_`` and ``:``
# are deliberately absent so extensions never match the base token.
_TOKEN_DELIM = r"(?=$|[\s,;)\]])"


def _flag_present(text: str, flag: str) -> bool:
    """True when the flag appears as a whole token (not a prefix)."""
    text = text or ""
    return re.search(
        r"(?:^|[\s(=])%s%s" % (re.escape(flag), _TOKEN_DELIM), text,
    ) is not None


def _pair_present(text: str, name: str, value: str) -> bool:
    """True when ``name=value`` or ``name value`` with an *exact* value token."""
    text = text or ""
    if re.search(
        r"%s\s*=\s*%s%s" % (re.escape(name), re.escape(value), _TOKEN_DELIM),
        text,
    ):
        return True
    return re.search(
        r"%s\s+%s%s" % (re.escape(name), re.escape(value), _TOKEN_DELIM),
        text,
    ) is not None


def _mode_json_present(text: str) -> bool:
    """True when ``json`` is observed as a ``--mode`` value.

    pi's help lists mode values in the flag's own line description
    (``--mode <mode>   Output mode: text (default), json, or rpc``) rather
    than as ``--mode json``, so the grammar accepts both the direct
    ``--mode=json`` / ``--mode json`` forms and an exact ``json`` value token
    on the ``--mode`` flag's line.  ``jsonl``/``jsonrpc`` never match
    (strict delimiter).
    """
    text = text or ""
    if _pair_present(text, "--mode", "json"):
        return True
    for line in text.splitlines():
        if "--mode" not in line:
            continue
        if re.search(r"(?:^|[\s(=:,])json%s" % _TOKEN_DELIM, line):
            return True
    return False


def _short_flag_present(text: str, flag: str) -> bool:
    """True when a short flag appears at a token boundary (``-p`` vs ``-pt``)."""
    text = text or ""
    return re.search(
        r"(?:^|\s)%s(?=$|[\s,])" % re.escape(flag), text,
    ) is not None


def match(text: str) -> dict[str, bool]:
    """Return the capability bits observed in a decoded ``--help`` text."""
    text = text or ""
    spawn = _flag_present(text, "--print") or _short_flag_present(text, "-p")
    json_mode = _mode_json_present(text)
    native = (_flag_present(text, "--session")
              or _flag_present(text, "--session-id"))
    return {
        "spawn": spawn,
        # ``--resume``/``-r`` is an interactive picker; deterministic session
        # selection goes through ``--session-id``.  Resume stays unclaimed.
        "resume": False,
        "native_transcript": native,
        "structured_stream": json_mode,
        "hooks": False,
        "pty": False,
    }


def event_kind_map() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Ordered (kind, qualities) tuples for a verified structured interface."""
    return (
        ("session_start", ("structured",)),
        ("user_message", ("structured",)),
        ("assistant_message", ("structured",)),
        ("tool_call", ("structured",)),
        ("tool_result", ("structured",)),
        ("process_spawn", ("structured",)),
        ("process_exit", ("structured",)),
        ("session_close", ("structured",)),
    )


def version_patterns() -> tuple[Pattern, ...]:
    """Ordered regexes to extract a stable version from ``--version`` output."""
    return (
        re.compile(r"^\s*(\d+(?:\.\d+)+)\s*$"),
        re.compile(r"pi\s+(\d+(?:\.\d+)+)"),
        re.compile(r"^\s*(\d+(?:\.\d+)+)"),
    )
