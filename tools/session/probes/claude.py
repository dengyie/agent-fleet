"""Probe definitions for the Claude Code CLI.

Capability claims derive *only* from observed ``--version`` / ``--help``
output.  :func:`match` applies a family-specific full-token grammar so a help
line such as ``--output-format=summary`` or ``stream-jsonish`` never sets a
capability, while genuine ``--print`` / ``-p``, ``--resume``, ``--hooks``, and
``--output-format=stream-json`` do.  ``--resume`` requires a successful
explicit resume probe executed by the probe runner; a bare help-text mention
is never a claim by itself.

Value/punctuation extensions never match: ``--output-format=stream-json-v1``,
``stream-json.foo``, ``stream-json_extra`` and ``stream-jsonish`` all stay
false, because the exact ``stream-json`` token must be followed by end-of-text
or a strict delimiter.
"""

import re
from typing import Pattern

# Strict delimiter that must follow a matched flag/value token: end-of-text,
# whitespace, or a small set of non-identifier punctuation. ``-``, ``.``, ``_``
# and ``:`` are deliberately absent so extensions never match the base token:
# ``stream-json-v1``, ``stream-json.foo`` and ``stream-json_extra`` can never
# claim the exact ``stream-json`` capability.
_TOKEN_DELIM = r"(?=$|[\s,;)\]])"


def _flag_present(text: str, flag: str) -> bool:
    """True when ``flag`` appears as a whole flag token, not a substring.

    ``--print`` matches ``--print`` and ``--print=`` but never ``--printf``;
    ``-p`` matches the short flag only at a token boundary.  A trailing comma
    is a normal help-listing separator (``-p, --print``), not a token
    extension.
    """
    text = text or ""
    if flag in ("-p", "-r"):
        return re.search(
            r"(?:^|\s)(?:%s)(?=$|[\s,])" % re.escape(flag), text,
        ) is not None
    return re.search(
        r"(?:^|[\s(=])%s(?=$|[\s=,\])])" % re.escape(flag), text,
    ) is not None


def _pair_present(text: str, name: str, value: str) -> bool:
    """True when ``name=value`` or ``name value`` with an *exact* value token.

    ``--output-format=stream-json`` and ``--output-format stream-json`` match;
    extensions such as ``stream-json-v1``, ``stream-json.x`` and
    ``stream-json_extra`` never do because the token must be followed by end-of-
    string or a strict delimiter.
    """
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


def match(text: str) -> dict[str, bool]:
    """Return the capability bits observed in a decoded ``--help`` text."""
    spawn = _flag_present(text, "--print") or _flag_present(text, "-p")
    resume = _flag_present(text, "--resume") or _flag_present(text, "-r")
    stream = _pair_present(text, "--output-format", "stream-json")
    return {
        "spawn": spawn,
        "resume": resume,
        "native_transcript": stream,
        "structured_stream": stream,
        "hooks": _flag_present(text, "--hooks"),
        "pty": False,
    }


def help_flags() -> dict[str, str]:
    """Documentation tokens matched by :func:`match` (kept for compatibility)."""
    return {
        "spawn": "--print",
        "resume": "--resume",
        "native_transcript": "--output-format=stream-json",
        "structured_stream": "--output-format=stream-json",
        "hooks": "--hooks",
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
        ("step_start", ("structured",)),
        ("step_end", ("structured",)),
        ("session_close", ("structured",)),
        ("policy_signal", ("best_effort",)),
    )


def version_patterns() -> tuple[Pattern, ...]:
    """Ordered regexes used to extract a stable ``X.Y.Z`` version."""
    return tuple(
        re.compile(p)
        for p in (
            r"Claude Code version\s+(\S+)",
            r"Claude Code\s+(\S+)",
            r"^\s*(\d+\.\d+\.\d+(?:[\w.\-]*))\b",
        )
    )