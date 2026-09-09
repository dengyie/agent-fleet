"""Probe definitions for Hermes (observation-only best-effort).

Per the frozen design, Hermes has no verified non-interactive spawn entry, no
structured transcript API, and no stable two-way session event protocol.  Every
:func:`match` output bit is False: metadata/help text alone never produces a
spawn/resume/structured claim.  :func:`event_kind_map` is empty because no
structured interface was observed.
"""

import re
from typing import Pattern


def match(text: str) -> dict[str, bool]:
    """Hermes is observation-only: every capability bit is always False.

    ``text`` is intentionally ignored; help/version strings are never evidence
    for Hermes in this phase.
    """
    del text  # unused by contract: metadata is not evidence
    return {
        "spawn": False,
        "resume": False,
        "native_transcript": False,
        "structured_stream": False,
        "hooks": False,
        "pty": False,
    }


def help_flags() -> dict[str, str]:
    """Every key maps to ``None``: nothing may be claimed for Hermes."""
    return {
        "spawn": None,
        "resume": None,
        "native_transcript": None,
        "structured_stream": None,
        "hooks": None,
        "pty": None,
    }


def event_kind_map() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """No structured interface is known for Hermes; nothing to claim."""
    return ()


def version_patterns() -> tuple[Pattern, ...]:
    """Ordered regexes to extract the gateway version for display only."""
    return (
        re.compile(r"Hermes\s+(?:Agent\s+)?v?([0-9.]+)"),
        re.compile(r"^\s*v?([0-9]+\.[0-9]+(?:\.[0-9]+)?)"),
    )