"""Application-layer session ingress and operator query service (Task 6).

``SessionService`` composes the Task 5 repositories behind one bounded
transport-facing surface:

- ``ingest_events`` validates a bounded event batch, durably persists valid
  events (per-event classification, never a lost valid event because a sibling
  is malformed), upserts session metadata, computes a contiguous ACK prefix,
  and returns the ``accepted_through`` / ``next_cursor`` / per-event
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
import inspect
import hashlib
from collections.abc import Mapping, Sequence

from session_schema import (
    MAX_BATCH_BYTES,
    MAX_BATCH_EVENTS,
    public_session_dto,
    try_validate_event,
)

from hub.infrastructure.session_repository import SessionError
from hub.infrastructure.transcript_repository import TranscriptError
from hub.domain.interaction import normalize_answer, validate_answer_ids
from tools.session.redact import Redactor

DEFAULT_QUERY_LIMIT = 100
_MAX_QUERY_LIMIT = 1000
_MAX_SQLITE_SEQUENCE = (1 << 63) - 1


class SessionServiceError(RuntimeError):
    """Bounded session-service error: stable ``code``/``detail``/``status``.

    ``str(err)`` and ``code`` carry only a stable short token - never paths,
    exception text, raw input, or key material.
    """

    _STATUS_OVERRIDES = {
        "session_not_found": 404,
        "session_unavailable": 503,
        "session_not_managed": 409,
        "control_unavailable": 503,
        "invalid_batch": 400,
        "batch_too_many": 400,
        "batch_too_large": 400,
        "invalid_json": 400,
        "interaction_expired": 409,
        "interaction_answered": 409,
        "interaction_pending": 409,
        "interaction_unavailable": 503,
        "feature_unavailable": 503,
    }

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = str(detail)[:200] or ""
        self.status = self._STATUS_OVERRIDES.get(code, 400)
        super().__init__(code)

    def __str__(self) -> str:
        return self.code


def _json_bytes(value) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=True))
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise SessionServiceError("invalid_batch", "批次包含不可序列化内容") from exc


def _bounded_limit(value, default: int = DEFAULT_QUERY_LIMIT) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, _MAX_QUERY_LIMIT))


class SessionService:
    """Application service owning the session ingest + operator query use cases."""

    def __init__(self, session_repo, transcript_repo, event_publisher=None,
                 search_service=None) -> None:
        self.session_repo = session_repo
        self.transcript_repo = transcript_repo
        self.event_publisher = event_publisher
        self.search_service = search_service
        self.supervisor = None

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
        batch_key: tuple[str, str] | None = None
        rejected: list[dict] = []
        accepted_sequences: dict[str, set[int]] = {}
        search_events: dict[str, list[str]] = {}
        projected_events: dict[str, list[str]] = {}
        search_diagnostic = None
        stored_count = 0

        for index, raw in enumerate(events):
            if not isinstance(raw, Mapping):
                rejected.append({"index": index, "code": "invalid_event"})
                continue
            ok, clean = try_validate_event(raw)
            if not ok:
                rejected.append({"index": index, "code": clean})
                continue

            # Exact is a source capability claim, not an event-controlled
            # quality value. Until a signed source grant is wired here,
            # reject it before creating session or transcript rows.
            if clean["capture_quality"] == "exact":
                rejected.append({
                    "index": index,
                    "code": "capture_source_unverified",
                })
                continue

            session_id = clean["session_id"]
            current_key = (session_id, clean["stream_id"])
            if batch_key is None:
                batch_key = current_key
                stream_id = clean.get("stream_id")
            elif current_key != batch_key:
                rejected.append({"index": index, "code": "mixed_stream"})
                continue
            sequence = int(clean["sequence"])
            try:
                bound_session = self.session_repo.upsert_session(self._session_spec_from_event(clean))
                if clean.get("kind") == "agent_state_changed" and not bound_session.get("managed"):
                    clean["capture_quality"] = "best_effort"
                    clean["payload"].update(state="unknown", confidence="low", reason="unmanaged_source")
                self.session_repo.init_stream(session_id)
                self.session_repo.note_event(
                    session_id, clean["event_id"], sequence, clean["kind"])
            except (SessionError, ValueError, TypeError):
                # Session-binding failures are per-event; the request continues.
                rejected.append({"index": index, "code": "invalid_session_binding"})
                continue
            except (sqlite3.Error, OSError) as exc:
                # Session-store unavailability (locked / read-only / corrupt)
                # is a bounded store failure, isolated from the legacy
                # observation ingest route — never an unhandled 500.
                raise SessionServiceError(
                    "session_unavailable", "会话存储不可用，观测链路不受影响") from exc

            try:
                result = self.transcript_repo.ingest(clean)
            except Exception as exc:
                # Transcript persistence failure is a bounded session error,
                # isolated from the legacy observation ingest route.
                raise SessionServiceError(
                    "session_unavailable", "会话存储不可用，观测链路不受影响") from exc

            stored = result.status in ("accepted", "duplicate") or (
                result.status == "gap" and result.reason != "raw_quota_exceeded")
            if stored:
                stored_count += 1
                accepted_sequences.setdefault(session_id, set()).add(sequence)
                if self.search_service is not None and clean.get("kind") in {
                    "user_message", "assistant_message", "tool_call", "tool_result"
                }:
                    search_events.setdefault(session_id, []).append(clean["event_id"])
                if clean.get("kind") in {"agent_state_changed", "interaction_request",
                        "interaction_response", "session_start", "session_metadata", "session_close"}:
                    projected_events.setdefault(session_id, []).append(clean["event_id"])
                if clean.get("kind") == "session_close":
                    self._close_session_from_event(clean)
                if result.status != "duplicate":
                    self._publish_session_summary(clean)
            else:
                rejected.append({"index": index,
                                 "code": result.reason or "rejected"})

        for projected_session_id, ids in projected_events.items():
            self._project_events(projected_session_id, ids)

        if self.search_service is not None:
            for search_session_id, event_ids in search_events.items():
                outcome = self.search_service.index_accepted(
                    self.transcript_repo, self.session_repo, search_session_id, event_ids)
                if outcome and outcome.get("code"):
                    search_diagnostic = "search_unavailable"

        if batch_key is not None and accepted_sequences:
            session_id, _ = batch_key
            cursor_reader = getattr(self.session_repo, "ack_cursor", None)
            try:
                cursor = int(cursor_reader(session_id)) if callable(cursor_reader) else 0
            except (SessionError, sqlite3.Error, OSError) as exc:
                raise SessionServiceError(
                    "session_unavailable", "会话存储不可用，ACK 未提交") from exc
            except (TypeError, ValueError) as exc:
                raise SessionServiceError(
                    "session_unavailable", "会话游标无效，ACK 未提交") from exc
            contiguous_reader = getattr(self.transcript_repo,
                                        "contiguous_sequence", None)
            if callable(contiguous_reader):
                try:
                    cursor = int(contiguous_reader(session_id, cursor))
                except (TranscriptError, sqlite3.Error, OSError) as exc:
                    raise SessionServiceError(
                        "session_unavailable", "会话存储不可用，ACK 未提交") from exc
                except (TypeError, ValueError) as exc:
                    raise SessionServiceError(
                        "session_unavailable", "会话游标无效，ACK 未提交") from exc
            else:
                for sequence in sorted(accepted_sequences.get(session_id, ())):
                    if sequence == cursor + 1:
                        cursor = sequence
                    elif sequence > cursor + 1:
                        break
            accepted_through = cursor
            try:
                self.session_repo.acknowledge(session_id, accepted_through)
            except (SessionError, sqlite3.Error, OSError) as exc:
                raise SessionServiceError(
                    "session_unavailable", "会话存储不可用，ACK 未提交") from exc

        if not rejected and stored_count > 0:
            status = "accepted"
        elif stored_count > 0 or accepted_through > 0:
            status = "partial"
        else:
            status = "rejected"

        diagnostics = {}
        if search_diagnostic:
            diagnostics["search_diagnostic"] = search_diagnostic
        if getattr(self.session_repo, "management_available", True) is False:
            diagnostics["management_diagnostic"] = "feature_unavailable"
        return {
            **diagnostics,
            "ok": True,
            "status": status,
            "stream_id": stream_id or "",
            "accepted_through": accepted_through,
            "next_cursor": secrets.token_hex(16),
            "rejected": rejected,
        }

    def _apply_agent_state(self, event: Mapping) -> None:
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            return
        updater = getattr(self.session_repo, "update_agent_state", None)
        if not callable(updater):
            return
        try:
            updater(
                event["session_id"],
                payload.get("state"),
                payload.get("source"),
                payload.get("confidence"),
                int(event["sequence"]),
            )
        except (SessionError, sqlite3.Error, OSError) as exc:
            raise SessionServiceError(
                "session_unavailable", "会话状态存储不可用") from exc

    def _project_events(self, session_id: str, event_ids: list[str]) -> None:
        """Use stored content, including replay, rather than caller duplicate payloads."""
        reader = getattr(self.transcript_repo, "read_redacted_events", None)
        if not callable(reader) or getattr(self.session_repo, "management_available", True) is False:
            return
        try:
            rows = reader(session_id, event_ids)
            for event in rows:
                kind, payload = event["kind"], event.get("payload") or {}
                if kind == "agent_state_changed":
                    self._apply_agent_state(event)
                elif kind in ("session_start", "session_metadata") and "interactive_questions" in payload:
                    self.session_repo.set_capabilities(session_id,
                        ["interactive_questions"] if payload["interactive_questions"] is True else [],
                        sequence=event["sequence"])
                elif kind in ("interaction_request", "interaction_response", "session_close"):
                    clean = Redactor(passthrough=False).redact_event(event)[0]
                    self.session_repo.project_interaction(session_id, event["sequence"], kind, clean["payload"])
        except (SessionError, TranscriptError, sqlite3.Error, OSError, ValueError) as exc:
            raise SessionServiceError("session_unavailable") from exc

    def _publish_session_summary(self, event: Mapping) -> None:
        publisher = self.event_publisher
        if publisher is None:
            return
        payload = event.get("payload")
        status = "running"
        if event.get("kind") == "session_close":
            status = self._close_status(payload)
        try:
            current = self.session_repo.get_session(event["session_id"])
            if isinstance(current, Mapping) and current.get("status"):
                status = str(current["status"])
        except Exception:
            pass
        try:
            publisher.emit(
                "session_update",
                machine=event.get("machine_id"),
                changes=["session"],
                session_id=event.get("session_id"),
                sequence=int(event.get("sequence") or 0),
                cursor=int(event.get("sequence") or 0),
                kind=event.get("kind"),
                capture_quality=event.get("capture_quality"),
                status=status,
            )
        except Exception:
            # Optional observation fan-out cannot make transcript ingest fail.
            return

    @staticmethod
    def _close_status(payload) -> str:
        if isinstance(payload, Mapping):
            raw_status = str(payload.get("status") or "").lower()
            if raw_status in {"failed", "error", "cancelled", "aborted"}:
                return "failed"
            try:
                if int(payload.get("exit_code")) != 0:
                    return "failed"
            except (TypeError, ValueError):
                pass
        return "closed"

    def _close_session_from_event(self, event: Mapping) -> None:
        close = getattr(self.session_repo, "close_session", None)
        if not callable(close):
            return
        try:
            close(event["session_id"], self._close_status(event.get("payload")),
                  event["emitted_at"])
        except (SessionError, sqlite3.Error, OSError) as exc:
            raise SessionServiceError(
                "session_unavailable", "会话存储不可用，会话状态未更新") from exc

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
        sequence = row.get("sequence")
        if (not isinstance(sequence, int) or isinstance(sequence, bool)
                or not 1 <= sequence <= _MAX_SQLITE_SEQUENCE):
            sequence = 0
        candidate: dict = {
            "schema_version": 1,
            "event_id": str(row.get("event_id") or ""),
            "stream_id": row.get("stream_id"),
            "machine_id": row.get("machine_id"),
            "session_id": str(row.get("session_id") or ""),
            "sequence": sequence,
            "kind": str(row.get("kind") or ""),
            "capture_quality": str(row.get("capture_quality") or ""),
            "emitted_at": str(row.get("emitted_at") or ""),
        }
        payload = row.get("payload")
        if payload is not None:
            candidate["payload"] = payload
        ok, clean = try_validate_event(candidate)
        if ok:
            if clean["kind"] in ("interaction_request", "interaction_response"):
                clean = Redactor(passthrough=False).redact_event(clean)[0]
            if sequence > (1 << 53) - 1:
                clean["sequence"] = str(sequence)
            return clean
        return {
            "event_id": candidate["event_id"],
            "session_id": candidate["session_id"],
            "sequence": str(sequence) if sequence > (1 << 53) - 1 else sequence,
            "kind": candidate["kind"],
            "capture_quality": candidate["capture_quality"],
            "emitted_at": candidate["emitted_at"],
        }

    # -- operator query use cases ----------------------------------------------

    def list_sessions(self, machine=None,
                      limit: int = DEFAULT_QUERY_LIMIT,
                      active: bool = False) -> dict:
        """List session metadata as bounded public DTOs."""
        try:
            kwargs = {
                "machine_id": machine or None,
                "limit": _bounded_limit(limit),
            }
            if active:
                kwargs["active_only"] = True
            rows = self.session_repo.list_sessions(**kwargs)
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
        row["capabilities"] = self.session_repo.get_capabilities(session_id)
        return {"session": public_session_dto(row)}

    def answer_interaction(self, session_id: str, data: Mapping) -> dict:
        """Answer once through the signed append-user-turn channel, never PTY input."""
        session = self.get_session(session_id)["session"]
        if getattr(self.session_repo, "management_available", True) is False:
            raise SessionServiceError("feature_unavailable")
        if not session.get("managed"):
            raise SessionServiceError("session_not_managed")
        if "interactive_questions" not in session.get("capabilities", []):
            raise SessionServiceError("interaction_unavailable")
        if session.get("status") in ("closed", "failed") or session.get("current_state") in ("completed", "failed", "stopped"):
            raise SessionServiceError("interaction_expired")
        if self.supervisor is None:
            raise SessionServiceError("control_unavailable")
        enqueue = self.supervisor.enqueue
        try:
            supports_idempotency = "idempotency_key" in inspect.signature(enqueue).parameters
        except (TypeError, ValueError):
            supports_idempotency = False
        try:
            request_id, token = validate_answer_ids(data)
            command_key = "interaction:" + hashlib.sha256(
                f"{session_id}\0{request_id}\0{token}".encode("utf-8")).hexdigest()
            question = self.session_repo.get_interaction(session_id, request_id)
            if question is None:
                raise SessionServiceError("interaction_expired")
            answer, text = normalize_answer(question, data)
            receipt = self.session_repo.reserve_interaction(session_id, request_id, token, answer)
            if receipt is not None:
                return receipt
        except ValueError as exc:
            raise SessionServiceError("invalid_interaction_answer") from exc
        except SessionError as exc:
            if exc.code == "interaction_pending":
                existing = None
                find_idempotency = getattr(self.supervisor, "find_idempotency", None)
                if callable(find_idempotency):
                    try:
                        existing = find_idempotency(command_key)
                    except Exception as reconcile_exc:
                        raise SessionServiceError("interaction_unavailable") from reconcile_exc
                if existing and existing.get("command_id"):
                    try:
                        self.session_repo.finish_interaction(
                            session_id, request_id, existing["command_id"])
                    except SessionError as reconcile_exc:
                        if reconcile_exc.code != "interaction_pending":
                            raise SessionServiceError(reconcile_exc.code) from reconcile_exc
                    return {"session_id": session_id,
                            "command_id": existing["command_id"], "status": "queued"}
                if supports_idempotency and callable(find_idempotency):
                    # A crash can occur after reserving the answer but before
                    # inserting its command. The stable outbox key makes this
                    # retry safe even if another request is enqueueing it now.
                    pass
                else:
                    raise SessionServiceError(exc.code) from exc
            else:
                raise SessionServiceError(exc.code) from exc
        except (sqlite3.Error, OSError) as exc:
            raise SessionServiceError("interaction_unavailable") from exc
        try:
            enqueue_kwargs = {"payload": {"text": text}}
            if supports_idempotency:
                enqueue_kwargs["idempotency_key"] = command_key
            command = enqueue(
                session["machine_id"], session_id, session.get("attempt_id"),
                "append_user_turn", "operator_requested", **enqueue_kwargs)
        except Exception as exc:
            # Keep the durable reservation in ``dispatching``. A retry looks
            # up the stable command key first, then safely enqueues if the
            # previous attempt never committed a command.
            raise SessionServiceError("interaction_unavailable") from exc
        try:
            self.session_repo.finish_interaction(session_id, request_id, command["command_id"])
        except (sqlite3.Error, OSError, SessionError) as exc:
            try:
                persisted_command_id = self.session_repo.interaction_command(
                    session_id, request_id)
            except (sqlite3.Error, OSError, SessionError):
                persisted_command_id = None
            if persisted_command_id == command["command_id"]:
                return {"session_id": session_id,
                        "command_id": command["command_id"], "status": "queued"}
            raise SessionServiceError("interaction_pending") from exc
        return {"session_id": session_id, "command_id": command["command_id"], "status": "queued"}

    def send_message(self, session_id: str, text: str) -> dict:
        """Queue a follow-up for a managed session through the existing channel."""
        session = self.get_session(session_id)["session"]
        if not session.get("managed"):
            raise SessionServiceError("session_not_managed", "请先纳管此会话")
        if self.supervisor is None:
            raise SessionServiceError("control_unavailable", "会话控制未启用")
        command = self.supervisor.enqueue(
            session["machine_id"], session_id, session.get("attempt_id"), "append_user_turn",
            "operator_requested", payload={"text": text})
        return {"session_id": session_id, "command_id": command["command_id"], "status": "pending"}

    def session_events(self, session_id: str,
                       limit: int = DEFAULT_QUERY_LIMIT, after_sequence: int = 0) -> dict:
        """Return the bounded redacted event stream for one session."""
        try:
            rows = self.transcript_repo.read_redacted(
                session_id, limit=_bounded_limit(limit), after_sequence=after_sequence)
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
