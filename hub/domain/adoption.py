"""Independent agent-adoption domain (Task 4).

Owns the adoption record ("纳管记录"): a machine + session + process that the
fleet operator has chosen to adopt for supervised capture.  This domain is
deliberately isolated from the rest of the hub: it never issues commands and
never touches the legacy observation stores.  It exists so a later
adoption-control surface can accept, revoke, and list adoptions against a
stable, validated contract.

The lifecycle is ``pending -> adopted -> revoked``:

- ``pending``  — an adoption record has been created but not yet enployed;
- ``adopted``  — the record is in force;
- ``revoked``  — the record is closed; revoked rows are retained for audit
  and never delete a session row.

Validation rules enforced here (pure Python, no persistence):

- ``adoption_id`` / ``machine_id`` are bounded opaque strings;
- ``session_id`` passes the shared ``is_valid_session_id`` boundary used by
  every other repository;
- ``pid`` / ``pgid`` must be positive integers (``pgid`` may be ``None``);
- ``status`` must be a member of ``ADOPTION_STATUSES`` and transitions only
  move along ``pending -> adopted -> revoked`` (idempotent same-status is
  tolerated so retries never corrupt a row);
- ``capture_quality`` must be a member of ``CAPTURE_QUALITIES``.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from session_schema import CAPTURE_QUALITIES, is_valid_session_id

__all__ = [
    "ADOPTION_STATUS_ORDER",
    "ADOPTION_STATUSES",
    "ADOPTION_STATUS_VALUES",
    "Adoption",
    "AdoptionStatus",
    "MAX_OPAQUE_ID",
    "allowed_old_statuses",
    "is_allowed_status_transition",
    "is_valid_adoption_status",
    "normalize_adoption_spec",
]

# -- status lifecycle ----------------------------------------------------------

class AdoptionStatus(str, Enum):
    PENDING = "pending"
    ADOPTED = "adopted"
    REVOKED = "revoked"


ADOPTION_STATUSES = frozenset(status.value for status in AdoptionStatus)
ADOPTION_STATUS_VALUES = tuple(status.value for status in AdoptionStatus)
ADOPTION_STATUS_ORDER = ("pending", "adopted", "revoked")

#: For each new status, the set of current statuses a row may transition from.
#: Idempotent same-status writes are allowed on every state; ``revoked`` is
#: terminal so a revoked row can only ever be revoked again.
_ALLOWED_OLD_STATUSES: dict[str, tuple[str, ...]] = {
    AdoptionStatus.PENDING.value: (AdoptionStatus.PENDING.value,),
    AdoptionStatus.ADOPTED.value: (
        AdoptionStatus.PENDING.value,
        AdoptionStatus.ADOPTED.value,
    ),
    AdoptionStatus.REVOKED.value: (
        AdoptionStatus.ADOPTED.value,
        AdoptionStatus.REVOKED.value,
    ),
}


def is_valid_adoption_status(value: Any) -> bool:
    """True when ``value`` is one of the three adoption statuses."""
    return isinstance(value, str) and value in ADOPTION_STATUSES


def allowed_old_statuses(new_status: str) -> tuple[str, ...]:
    """Return statuses a row may currently be in to move to ``new_status``.

    Raises :class:`ValueError` for an unknown status.
    """
    if not is_valid_adoption_status(new_status):
        raise ValueError("unknown adoption status value")
    return _ALLOWED_OLD_STATUSES[new_status]


def is_allowed_status_transition(old_status: str, new_status: str) -> bool:
    if not is_valid_adoption_status(old_status) or not is_valid_adoption_status(new_status):
        return False
    return old_status in allowed_old_statuses(new_status)


# -- opaque / bounded field limits ---------------------------------------------

MAX_OPAQUE_ID = 256
MAX_PATH_FIELD = 1024
MAX_AGENT_FAMILY = 64
MAX_ACTOR = 128

_FORBIDDEN_ID_MARKERS = ("/", "\\", "token", "key", "secret", "private",
                         "password")


def _opaque(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty opaque id")
    if len(value) > MAX_OPAQUE_ID:
        raise ValueError(f"{name} is too long")
    lowered = value.lower()
    if any(marker in lowered for marker in _FORBIDDEN_ID_MARKERS):
        raise ValueError(f"{name} must not encode a path or secret")
    return value


def _bounded_str(value: Any, name: str, limit: int, *, required: bool = True) -> str:
    if value is None:
        if required:
            raise ValueError(f"{name} is required")
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value[:limit]


def _bounded_optional_str(value: Any, name: str, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value[:limit]


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


# -- adoption record -----------------------------------------------------------

@dataclass(frozen=True)
class Adoption:
    """Frozen adoption record (one per adopted session)."""

    adoption_id: str
    machine_id: str
    session_id: str
    pid: int
    pgid: int | None
    started_at: str
    exe_path: str
    agent_family: str
    native_file_path: str | None
    status: str
    capture_quality: str
    actor: str
    created_at: str
    updated_at: str


# -- normalization -------------------------------------------------------------

def normalize_adoption_spec(spec: Adoption | Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize an adoption record for persistence.

    Accepts a frozen :class:`Adoption` or a mapping with the same keys.  Returns
    a plain dict with every permitted field, bounded, and unknown keys dropped.
    Raises :class:`ValueError` on any invalid input; error messages never embed
    the raw input, path, or exception text.
    """
    if isinstance(spec, Adoption):
        raw = dataclasses.asdict(spec)
    elif isinstance(spec, Mapping):
        raw = spec
    else:
        raise ValueError("adoption must be a Adoption record or a dict")

    adoption_id = _opaque(raw.get("adoption_id"), "adoption_id")
    machine_id = _opaque(raw.get("machine_id"), "machine_id")

    session_id = raw.get("session_id")
    if not is_valid_session_id(session_id):
        raise ValueError("invalid session id boundary")

    pid = _positive_int(raw.get("pid"), "pid")

    pgid_raw = raw.get("pgid")
    pgid = None
    if pgid_raw is not None:
        pgid = _positive_int(pgid_raw, "pgid")

    status = _bounded_str(raw.get("status"), "status", 16)
    if not is_valid_adoption_status(status):
        raise ValueError("invalid adoption status")

    quality = _bounded_str(raw.get("capture_quality"), "capture_quality", 64)
    if quality not in CAPTURE_QUALITIES:
        raise ValueError("invalid capture quality")

    agent_family = _bounded_str(raw.get("agent_family"), "agent_family",
                                MAX_AGENT_FAMILY)
    if agent_family:
        lowered = agent_family.lower()
        if any(marker in lowered for marker in _FORBIDDEN_ID_MARKERS):
            agent_family = ""

    return {
        "adoption_id": adoption_id,
        "machine_id": machine_id,
        "session_id": session_id,
        "pid": pid,
        "pgid": pgid,
        "started_at": _bounded_str(raw.get("started_at"), "started_at",
                                   MAX_PATH_FIELD),
        "exe_path": _bounded_str(raw.get("exe_path"), "exe_path",
                                 MAX_PATH_FIELD),
        "agent_family": agent_family,
        "native_file_path": _bounded_optional_str(
            raw.get("native_file_path"), "native_file_path", MAX_PATH_FIELD),
        "status": status,
        "capture_quality": quality,
        "actor": _bounded_str(raw.get("actor"), "actor", MAX_ACTOR),
        "created_at": _bounded_str(raw.get("created_at"), "created_at",
                                   MAX_PATH_FIELD),
        "updated_at": _bounded_str(raw.get("updated_at"), "updated_at",
                                   MAX_PATH_FIELD),
    }