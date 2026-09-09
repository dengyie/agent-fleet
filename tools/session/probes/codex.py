"""Probe definitions for the Codex CLI.

Codex's ``exec`` (non-interactive spawn), ``--json`` (structured event
stream), ``--rollout`` (native transcript) and ``--resume`` capabilities are
claimed only when the observed help text matches the family grammar at a token
boundary.  ``execute``, ``--json-lines`` and ``--rollout-foo`` never match;
a bare ``exec`` subcommand is required for spawn.  ``--resume`` additionally
requires a successful explicit resume probe executed by the probe runner.

Codex is one of the two families allowed to advertise structured/native
capabilities after the probe observes them.
"""

import re
from typing import Pattern

# Strict delimiter closing a matched flag/value token: end-of-text, whitespace,
# or a small set of non-identifier punctuation. ``-``, ``.``, ``_`` and ``:``
# are deliberately absent so extensions never match the base token:
# ``--json-lines``/``--json-x`` never claim ``--json``, ``--rollout-foo`` never
# claims ``--rollout`` and ``exec.go`` never claims ``exec``.
_TOKEN_DELIM = r"(?=$|[\s,;)\]])"


def _flag_present(text: str, flag: str) -> bool:
    """True when the long flag appears as a whole token (not a prefix)."""
    text = text or ""
    return re.search(
        r"(?:^|[\s(=])%s%s" % (re.escape(flag), _TOKEN_DELIM), text,
    ) is not None


def _word_present(text: str, word: str) -> bool:
    """True when the bare word appears at a token boundary (e.g. ``exec``)."""
    text = text or ""
    return re.search(
        r"(?:^|[\s(])%s%s" % (re.escape(word), _TOKEN_DELIM), text,
    ) is not None


def match(text: str) -> dict[str, bool]:
    """Return the capability bits observed in a decoded ``--help`` text."""
    text = text or ""
    spawn = _word_present(text, "exec")
    resume = _flag_present(text, "--resume")
    rollout = _flag_present(text, "--rollout")
    json_flag = _flag_present(text, "--json")
    # ``--json`` must not be confused with ``exec --json``; the structured
    # stream is claimed only when the help text exposes ``--json`` directly.
    return {
        "spawn": spawn,
        "resume": resume,
        "native_transcript": rollout,
        "structured_stream": json_flag,
        "hooks": False,
        "pty": False,
    }


def help_flags() -> dict[str, str]:
    """Documentation tokens matched by :func:`match` (kept for compatibility)."""
    return {
        "spawn": "exec",
        "resume": "--resume",
        "native_transcript": "--rollout",
        "structured_stream": "--json",
        "hooks": None,
        "pty": None,
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
        re.compile(r"codex\-cli\s+(\d+(?:\.\d+)+)"),
        re.compile(r"codex\s+(\d+(?:\.\d+)+)"),
        re.compile(r"^\s*(\d+(?:\.\d+)+)"),
    )