"""Offline trusted identity manifest; tokens are represented only by digests."""
from __future__ import annotations

import stat
from pathlib import Path

from hub.domain.integration_identity import (
    IntegrationIdentity,
    IntegrationIdentityError,
    parse_integration_identity,
)
from hub.domain.workflow_errors import WorkflowValidationError
from hub.domain.workflow_identity import canonical_json
from hub.domain.workflow_json import decode_snapshot

MAX_REGISTRATION_BYTES = 256 * 1024
MAX_IDENTITIES = 128


def read_integration_registration(path: Path) -> tuple[IntegrationIdentity, ...]:
    try:
        if not path.is_absolute() or not stat.S_ISREG(path.stat().st_mode):
            raise IntegrationIdentityError("invalid_integration_registration_file")
        with path.open("rb") as stream:
            value = decode_snapshot(stream.read(MAX_REGISTRATION_BYTES + 1), max_bytes=MAX_REGISTRATION_BYTES)
        if (set(value) != {"schema_version", "identities"} or type(value["schema_version"]) is not int
                or value["schema_version"] != 1 or type(value["identities"]) is not list
                or not 1 <= len(value["identities"]) <= MAX_IDENTITIES):
            raise IntegrationIdentityError("invalid_integration_registration_manifest")
        identities = tuple(parse_integration_identity(canonical_json(row)) for row in value["identities"])
    except OSError as exc:
        raise IntegrationIdentityError("integration_registration_file_unavailable") from exc
    except WorkflowValidationError as exc:
        raise IntegrationIdentityError("invalid_integration_registration_json") from exc
    if len({(item.owner_id, item.integration_id) for item in identities}) != len(identities):
        raise IntegrationIdentityError("duplicate_integration_registration")
    if len({item.token_digest for item in identities}) != len(identities):
        raise IntegrationIdentityError("integration_token_conflict")
    return identities
