"""Application-layer session ingress and operator query service (Task 6).

``SessionService`` composes the Task 5 repositories behind one bounded
transport-facing surface:

- ``ingest_events`` validates a bounded event batch, durably persists valid
  events (per-event classification, never a lost valid event because a sibling
  is malformed), upserts session metadata, advances per-session stream
  cursors, and returns the ``accepted_through`` / ``next_cursor`` / per-event
  rejects the Session Bridge uploader consumes;
- ``list_sessions`` / ``get_session`` / ``session_events`` /
  ``policy_signals`` form the operator query surface against the same
  repositories;
- a transcript repository failure is isolated as a bounded
  ``session_unavailable`` error - it can never break the legacy observation
  ingest route.

Constructor-injected repositories keep this module free of Flask, request
state, credentials, and module-level path lookups.  The public DTOs are the
shared ``public_session_dto`` / event allowlists, so nothing raw ever leaves
the service.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Mapping, Sequence

from session_schema import (
    MAX_BATCH_BYTES,
    MAX_BATCH_EVENTS,
    public_session_dto,
    try_validate_event,
)

from hub.infrastructure.session_repository import SessionError
from hub.infrastructure.transcript_repository import TranscriptError

DEFAULT_QUERY_LIMIT = 100
_MAX_QUERY_LIMIT = 1000


class SessionServiceError(RuntimeError):
    """Bounded session-service error: stable ``code``/``detail``/``status``.

    ``str(err)`` and ``code`` carry only a stable short token - never paths,
    exception text, raw input, or key material.
    """

    _STATUS_OVERRIDES = {
        "session_not_found": 404,
        "session_unavailable": 503,
        "invalid_batch": 400,
        "batch_too_many": 400,
        "batch_too_large": 400,
        "invalid_json": 400,
    }

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = str(detail)[:200] or ""
        self.status = self._STATUS_OVERRIDES.get(code, 400)
        super().__init__(code)

    def __str__(self) -> str:
        return self.code


def _json_bytes(value) -> int:
    return len(json.dumps(value, ensure_ascii=True))


def _bounded_limit(value, default: int = DEFAULT_QUERY_LIMIT) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, _MAX_QUERY_LIMIT))


class SessionService:
    """Application service owning the session ingest + operator query use cases."""

    def __init__(self, session_repo, transcript_repo) -> None:
        self.session_repo = session_repo
        self.transcript_repo = transcript_repo

    # -- write use case -----------------------------------------------------

    def ingest_events(self, events) -> dict:
        """Validate, isolate, persist, and ack one bounded event batch.

        Malformed events are rejected per-event (bounded ``{"index", "code"}``)
        and never drop legitimate events around them.  Returns the
        ``accepted_through`` / ``next_cursor`` / ``rejected`` shape the
        uploader consumes.  Raises :class:`SessionServiceError` for
        whole-request bounds or an isolated transcript-store failure.
        """
        if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
            raise SessionServiceError("invalid_batch", "批次必须是事件列表")
        if len(events) > MAX_BATCH_EVENTS:
            raise SessionServiceError(
                "batch_too_many", f"批次超过 {MAX_BATCH_EVENTS} 条上限")
        if _json_bytes(events) > MAX_BATCH_BYTES:
            raise SessionServiceError("batch_too_large", "批次超过字节上限")

        accepted_through = 0
        stream_id: str | None = None
        rejected: list[dict] = []
        acks: dict[str, int] = {}
        _reject_occurred = False

        for index, raw in enumerate(events):
            if not isinstance(raw, Mapping):
                rejected.append({"index": index, "code": "invalid_event"})
                continue
            ok, clean = try_validate_event(raw)
            if not ok:
                rejected.append({"index": index, "code": clean})
                continue

            session_id = clean["session_id"]
            sequence = int(clean["sequence"])
            try:
                self.session_repo.upsert_session(self._session_spec_from_event(clean))
                self.session_repo.init_stream(session_id)
                self.session_repo.note_event(
                    session_id, clean["event_id"], sequence, clean["kind"])
            except (SessionError, ValueError, TypeError):
                # Session-binding failures are per-event; the request continues.
                rejected.append({"index": index, "code": "invalid_session_binding"})
                continue
            except sqlite3.Error as exc:
                # Session-store unavailability (locked / read-only / corrupt)
                # is a bounded store failure, isolated from the legacy
                # observation ingest route — never an unhandled 500.
                raise SessionServiceError(
                    "session_unavailable", "会话存储不可用，观测链路不受影响") from exc

            if stream_id is None:
                stream_id = clean.get("stream_id")

            try:
                result = self.transcript_repo.ingest(clean)
            except Exception:
                # Transcript persistence failure is a bounded session error,
                # isolated from the legacy observation ingest route.
                raise SessionServiceError(
                    "session_unavailable", "会话存储不可用，观测链路不受影响") from None

            if result.status in ("accepted", "duplicate"):
                if not _reject_occurred:
                    accepted_through = max(accepted_through, sequence)
                    acks[session_id] = max(acks.get(session_id, 0), sequence)
            elif result.status == "gap" and result.reason != "raw_quota_exceeded":
                # A sequence-gap is stored (redacted row is always written; the
                # raw stream is best-effort) and is therefore acknowledged.
                if not _reject_occurred:
                    accepted_through = max(accepted_through, sequence)
                    acks[session_id] = max(acks.get(session_id, 0), sequence)
            else:
                # ``gap`` + ``raw_quota_exceeded`` means the quota cap would be
                # exceeded so the event is NOT stored at all — it is never
                # acknowledged.  Stop advancing ``accepted_through`` so the
                # uploader acknowledges only the contiguous prefix, preserving
                # the un-stored event (and any later events) for retry.
                _reject_occurred = True
                rejected.append({"index": index,
                                 "code": result.reason or "rejected"})

        for session_id, seq in acks.items():
            try:
                self.session_repo.acknowledge(session_id, seq)
            except (SessionError, sqlite3.Error):
                pass

        if not rejected and accepted_through > 0:
            status = "accepted"
        elif accepted_through > 0:
            status = "partial"
        else:
            status = "rejected"

        return {
            "ok": True,
            "status": status,
            "stream_id": stream_id or "",
            "accepted_through": accepted_through,
            "next_cursor": secrets.token_hex(16),
            "rejected": rejected,
        }

    @staticmethod
    def _session_spec_from_event(event: dict) -> dict:
        """Derive a SessionRepository spec from a validated session event.

        A managed event carries its own ``process_group_id``; a best-effort
        event without one degrades to an unmanaged row.
        """
        spec: dict = {
            "session_id": event["session_id"],
            "machine_id": event["machine_id"],
            "capture_quality": event.get("capture_quality", "best_effort"),
        }
        group = event.get("process_group_id")
        attempt = event.get("attempt_id")
        if group:
            spec["process_group_id"] = group
            spec["managed"] = True
        else:
            spec["managed"] = False
        if attempt:
            spec["attempt_id"] = attempt
        payload = event.get("payload")
        family = payload.get("agent_family") if isinstance(payload, Mapping) else None
        if isinstance(family, str) and family:
            spec["agent_family"] = family[:64]
        return spec

    # -- bounded DTO helpers ---------------------------------------------------

    @staticmethod
    def _public_event_row(row: Mapping) -> dict:
        """Build a bounded public event DTO from a redacted repo row.

        The row is short-circuited through the shared validator so the public
        surface is exactly the session event allowlist; a corrupt row degrades
        to a bounded skeleton that never leaks a payload.
        """
        candidate: dict = {
            "schema_version": 1,
            "event_id": str(row.get("event_id") or ""),
            "stream_id": row.get("stream_id"),
            "machine_id": row.get("machine_id"),
            "session_id": str(row.get("session_id") or ""),
            "sequence": int(row.get("sequence") or 0),
            "kind": str(row.get("kind") or ""),
            "capture_quality": str(row.get("capture_quality") or ""),
            "emitted_at": str(row.get("emitted_at") or ""),
        }
        payload = row.get("payload")
        if payload is not None:
            candidate["payload"] = payload
        ok, clean = try_validate_event(candidate)
        if ok:
            return clean
        return {
            "event_id": candidate["event_id"],
            "session_id": candidate["session_id"],
            "sequence": candidate["sequence"],
            "kind": candidate["kind"],
            "capture_quality": candidate["capture_quality"],
            "emitted_at": candidate["emitted_at"],
        }

    # -- operator query use cases ----------------------------------------------

    def list_sessions(self, machine=None,
                      limit: int = DEFAULT_QUERY_LIMIT) -> dict:
        """List session metadata as bounded public DTOs."""
        try:
            rows = self.session_repo.list_sessions(
                machine_id=machine or None, limit=_bounded_limit(limit))
        except SessionError as exc:
            raise SessionServiceError(exc.code) from None
        except Exception:
            raise SessionServiceError("session_unavailable") from None
        return {
            "sessions": [
                public_session_dto(r) for r in rows if isinstance(r, Mapping)
            ]
        }

    def get_session(self, session_id: str) -> dict:
        """Return a single bounded session DTO or ``session_not_found``."""
        try:
            row = self.session_repo.get_session(session_id)
        except SessionError as exc:
            raise SessionServiceError(exc.code) from None
        except Exception:
            raise SessionServiceError("session_unavailable") from None
        if not row:
            raise SessionServiceError("session_not_found")
        return {"session": public_session_dto(row)}

    def session_events(self, session_id: str,
                       limit: int = DEFAULT_QUERY_LIMIT) -> dict:
        """Return the bounded redacted event stream for one session."""
        try:
            rows = self.transcript_repo.read_redacted(
                session_id, limit=_bounded_limit(limit))
        except TranscriptError as exc:
            raise SessionServiceError(exc.code) from None
        except Exception:
            raise SessionServiceError("session_unavailable") from None
        return {"events": [self._public_event_row(r) for r in rows]}

    def policy_signals(self, session_id: str) -> dict:
        """Return the bounded policy-signal index for one session."""
        try:
            rows = self.transcript_repo.read_policy_signals(session_id)
        except TranscriptError as exc:
            raise SessionServiceError(exc.code) from None
        except Exception:
            raise SessionServiceError("session_unavailable") from None
        bounded = []
        for r in rows:
            if not isinstance(r, Mapping):
                continue
            bounded.append({
                "session_id": str(r.get("session_id") or ""),
                "emitted_at": str(r.get("emitted_at") or ""),
                "severity": str(r.get("severity") or "")[:64],
                "reason": str(r.get("reason") or "")[:200],
            })
        return {"signals": bounded}


__all__ = [
    "DEFAULT_QUERY_LIMIT",
    "SessionService",
    "SessionServiceError",
]