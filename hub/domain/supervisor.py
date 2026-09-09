"""hub/domain/supervisor.py — supervisor identity domain values (Task 9).

The Supervisor credential is machine-bound: ``X-Supervisor-Credential:
<machine>:<secret>``.  Parsing/validation here is pure, so the HTTP adapter and
the Agent-side client agree on the exact wire format.  Bound to ``MACHINE_RE``
from the project convention (a strict, safe hostname shape).
"""
from __future__ import annotations

import re
from typing import Any

#: Wire header carrying the supervisor credential.
SUPERVISOR_CREDENTIAL_HEADER = "X-Supervisor-Credential"
#: Environment variable holding the JSON mapping of machine -> supervisor secret.
SUPERVISOR_CREDENTIAL_ENV = "AGENT_FLEET_SUPERVISOR_CREDENTIALS"
#: Environment variable / credentials file for the Hub's Ed25519 signing private
#: key (base64 of 32 bytes).  Only the private key's public half verifies on the
#: Agent; issuance is disabled when absent.
SUPERVISOR_SIGNING_KEY_ENV = "AGENT_FLEET_SUPERVISOR_SIGNING_KEY"

_MACHINE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def parse_credential(header: Any) -> tuple[str, str] | None:
    """Parse ``<machine>:<secret>``; return ``(machine, secret)`` or None.

    Bounded: machine must be a plain safe hostname; a header with more than one
    separator is rejected so a secret containing ``:`` cannot be abused.
    """
    if not isinstance(header, str) or not header:
        return None
    machine, sep, secret = header.partition(":")
    if not sep:
        return None
    if not machine or not secret:
        return None
    if ":" in secret:
        return None
    if not _MACHINE_RE.fullmatch(machine):
        return None
    return machine, secret


def validate_machine(machine: Any) -> str | None:
    """Return the validated machine id or None (never raises)."""
    if not isinstance(machine, str) or not _MACHINE_RE.fullmatch(machine):
        return None
    return machine


__all__ = [
    "SUPERVISOR_CREDENTIAL_ENV",
    "SUPERVISOR_CREDENTIAL_HEADER",
    "SUPERVISOR_SIGNING_KEY_ENV",
    "parse_credential",
    "validate_machine",
]