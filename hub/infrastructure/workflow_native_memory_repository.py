"""Original Native Memory Run closure and one current authorization snapshot."""
from __future__ import annotations

from collections.abc import Mapping
import sqlite3
from typing import Any, Callable, Iterator

from hub.application.workflow_text_inputs import WorkflowTextInputs
from hub.application.workflow_code_inputs import WorkflowCodeInputs
from hub.application.workflow_media_inputs import WorkflowMediaInputs
from hub.domain.workflow_contract import parse_resolved
from hub.domain.workflow_contract_types import CodeRevisionReference, ResolvedContract
from hub.domain.workflow_errors import WorkflowPersistenceError, WorkflowValidationError
from hub.domain.workflow_identity import canonical_digest, canonical_json
from hub.domain.workflow_json import decode_snapshot
from hub.domain.workflow_native_memory import (
    NATIVE_CODE_MODE_MARKER, NATIVE_MEDIA_MODE_MARKER, NATIVE_MEMORY_MARKER, NATIVE_TEXT_MODE_MARKER, NativeWorkflowMemory, WorkflowNativeMemoryError, parse_native_memory,
)
from .workflow_memory_repository import WorkflowMemoryRepository
from .workflow_summary_repository import WorkflowSummaryRepository, validate_summary_input_closure
from .workflow_repository import WorkflowRepository, _time
from .workflow_schema import WorkflowSchemaError, inspect_schema


_SELECT = (
    "SELECT d.rowid AS dispatch_rowid,d.workflow_id,d.step_key,d.generation,"
    "d.token,d.owner_id,d.workspace_id,d.target_key,d.payload_hash,d.stage,d.binding_kind,d.binding_id,"
    "d.binding_run_id,s.kind AS step_kind,s.is_current,s.state AS step_state,s.active_deadline,"
    "CASE WHEN length(s.resolved_snapshot)<=65536 THEN s.resolved_snapshot END AS resolved_snapshot,"
    "r.run_id,r.owner_id AS run_owner,r.conversation_id,r.trigger_message_id,r.state AS run_state,"
    "r.lease_id,r.lease_owner,r.lease_expires_at,r.attempt,r.cancel_requested AS run_cancel,"
    "CASE WHEN length(CAST(r.config_snapshot AS BLOB))<=65536 THEN r.config_snapshot END AS config_snapshot,"
    "m.message_id,m.owner_id AS message_owner,m.conversation_id AS message_conversation,m.client_token,m.role,"
    "CASE WHEN length(CAST(m.content AS BLOB))<=32768 THEN m.content END AS content,"
    "c.workspace_id AS conversation_workspace,w.enabled AS workspace_enabled,"
    "f.cancel_requested,f.pause_requested,meta.admission_mode,meta.minimum_reader_version,"
    "b.workflow_id AS budget_workflow,b.child_request_id "
    "FROM workflow_dispatches d JOIN workflow_steps s USING(owner_id,workspace_id,workflow_id,step_key,generation) "
    "JOIN workflow_runs f USING(owner_id,workspace_id,workflow_id) CROSS JOIN workflow_schema_meta meta "
    "LEFT JOIN runs r ON r.run_id='wf-run-'||d.token "
    "LEFT JOIN messages m ON m.message_id=r.trigger_message_id "
    "LEFT JOIN conversations c ON c.owner_id=r.owner_id AND c.conversation_id=r.conversation_id "
    "LEFT JOIN workspaces w ON w.owner_id=d.owner_id AND w.workspace_id=d.workspace_id "
    "LEFT JOIN workflow_budget_reservations b USING(owner_id,workflow_id,step_key,generation) "
)


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    names = tuple(column[0] for column in cursor.description)
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _row(connection: sqlite3.Connection, token: str) -> dict[str, Any] | None:
    rows = _rows(connection.execute(_SELECT + "WHERE d.token=? AND meta.singleton=1 LIMIT 1", (token,)))
    return rows[0] if rows else None


def _text_reference_closure(connection: sqlite3.Connection,
                            saved: list[tuple[dict[str, Any], NativeWorkflowMemory | None]]) -> None:
    """Match original input refs with one indexed query per bounded page."""
    selected = [(row, snapshot) for row, snapshot in saved if snapshot is not None and (snapshot.texts or snapshot.codes or snapshot.media or snapshot.audio or snapshot.video)]
    if not selected:
        return
    identities = [tuple(row[key] for key in ("owner_id", "workspace_id", "workflow_id", "step_key", "generation"))
                  for row, _snapshot in selected]
    expected = {identity + ("input:" + text.ref_key, text.reference.artifact_id, text.reference.sha256, text.reference.size)
                for identity, (_row_value, snapshot) in zip(identities, selected) for text in snapshot.texts}
    expected.update(identity + ("input:" + code.ref_key, code.reference.source_bundle.artifact_id,
                code.reference.source_bundle.sha256, code.reference.source_bundle.size)
                for identity, (_row_value, snapshot) in zip(identities, selected) for code in snapshot.codes)
    expected.update(identity + ("input:" + media.ref_key, media.reference.artifact_id,
                media.reference.sha256, media.reference.size)
                for identity, (_row_value, snapshot) in zip(identities, selected) for media in snapshot.media)
    expected.update(identity + ("input:" + audio.ref_key, audio.reference.artifact_id,
                audio.reference.sha256, audio.reference.size)
                for identity, (_row_value, snapshot) in zip(identities, selected) for audio in snapshot.audio)
    expected.update(identity + ("input:" + video.ref_key, video.reference.artifact_id,
                video.reference.sha256, video.reference.size)
                for identity, (_row_value, snapshot) in zip(identities, selected) for video in snapshot.video)
    placeholders = ",".join("(?,?,?,?,?)" for _identity in identities)
    query = (
        "WITH selected(owner_id,workspace_id,workflow_id,step_key,generation) AS (VALUES " + placeholders + ") "
        "SELECT r.owner_id,r.workspace_id,r.workflow_id,r.step_key,r.generation,"
        "r.output_key,r.artifact_id,r.sha256,r.size FROM selected s JOIN workflow_artifact_refs r "
        "ON r.owner_id=s.owner_id AND r.workflow_id=s.workflow_id AND r.step_key=s.step_key AND r.generation=s.generation "
        "WHERE r.output_key LIKE 'input:%' LIMIT ?"
    )
    parameters = tuple(value for identity in identities for value in identity) + (len(expected) + 1,)
    actual = {tuple(row) for row in connection.execute(query, parameters).fetchall()}
    if actual != expected:
        raise WorkflowValidationError("native_memory_reference_mismatch")


def _summary_reference_closure(connection: sqlite3.Connection,
                               saved: list[tuple[dict[str, Any], NativeWorkflowMemory | None]]) -> None:
    validate_summary_input_closure(connection, [(parse_resolved(bytes(row["resolved_snapshot"])), snapshot.summaries)
        for row, snapshot in saved if snapshot is not None and snapshot.summaries])


def native_memory_history(connection: sqlite3.Connection) -> Iterator[list[dict[str, Any]]]:
    """One indexed join per bounded page, including signed SQLite rowids."""
    cursor: int | None = None
    while True:
        predicate = "" if cursor is None else "WHERE d.rowid>? "
        rows = _rows(connection.execute(_SELECT + predicate + "ORDER BY d.rowid LIMIT 128",
                                       () if cursor is None else (cursor,)))
        saved = [(row, _saved(row)) for row in rows]
        _text_reference_closure(connection, saved)
        _summary_reference_closure(connection, saved)
        yield rows
        if len(rows) < 128:
            return
        cursor = rows[-1]["dispatch_rowid"]


def saved_native_memory(connection: sqlite3.Connection, token: str, *,
                        resolved: ResolvedContract | None = None) -> NativeWorkflowMemory | None:
    """Validate saved facts without consulting mutable Memory rows."""
    row = _row(connection, token)
    if row is None:
        raise WorkflowValidationError("native_memory_binding_missing")
    snapshot = _saved(row, resolved)
    _text_reference_closure(connection, [(row, snapshot)])
    _summary_reference_closure(connection, [(row, snapshot)])
    return snapshot


def _saved(row: dict[str, Any], expected: ResolvedContract | None = None) -> NativeWorkflowMemory | None:
    if row["step_kind"] == "native_run" and row["binding_run_id"] is not None and row["run_id"] is None:
        raise WorkflowValidationError("native_memory_execution_missing")
    if row["resolved_snapshot"] is None:
        if row["run_id"] is None:
            return None
        raise WorkflowValidationError("invalid_native_memory_run")
    resolved = parse_resolved(bytes(row["resolved_snapshot"]))
    if expected is not None and resolved != expected:
        raise WorkflowValidationError("native_memory_mismatch")
    if row["run_id"] is None:
        return None
    if type(row["config_snapshot"]) is not str:
        raise WorkflowValidationError("invalid_native_memory_run")
    config = decode_snapshot(row["config_snapshot"].encode("utf-8"))
    text_required = NATIVE_TEXT_MODE_MARKER in config
    code_required = NATIVE_CODE_MODE_MARKER in config
    media_required = NATIVE_MEDIA_MODE_MARKER in config
    if text_required:
        mode = config[NATIVE_TEXT_MODE_MARKER]
        if (type(mode) is not dict or set(mode) != {"revision"} or type(mode["revision"]) is not int
                or mode["revision"] != 1 or not resolved.contract.input_refs):
            raise WorkflowValidationError("invalid_native_text_mode")
    if code_required:
        mode = config[NATIVE_CODE_MODE_MARKER]
        if (type(mode) is not dict or set(mode) != {"revision"} or type(mode["revision"]) is not int
                or mode["revision"] != 1 or not any(
                    type(item.reference) is CodeRevisionReference for item in resolved.contract.input_refs)):
            raise WorkflowValidationError("invalid_native_code_mode")
    if media_required:
        mode = config[NATIVE_MEDIA_MODE_MARKER]
        provider = config.get("provider_snapshot")
        capabilities = provider.get("capabilities") if type(provider) is dict else None
        if (type(mode) is not dict or set(mode) != {"revision"} or type(mode["revision"]) is not int
                or mode["revision"] != 1 or not resolved.contract.input_refs or type(capabilities) is not dict
                or not any(capabilities.get(name) is True for name in ("image_input", "audio_input", "video_input"))):
            raise WorkflowValidationError("invalid_native_media_mode")
    if row["step_kind"] != "native_run" or not (resolved.contract.context_refs or text_required or code_required or media_required):
        if NATIVE_MEMORY_MARKER in config or text_required or code_required or media_required:
            raise WorkflowValidationError("unexpected_native_memory")
        return None
    if canonical_json(config).decode("utf-8") != row["config_snapshot"]:
        raise WorkflowValidationError("invalid_native_memory_run")
    snapshot = parse_native_memory(config.get(NATIVE_MEMORY_MARKER), resolved)
    required_reader = 15 if snapshot.uses_video else (14 if snapshot.uses_audio else (13 if media_required else (12 if code_required else (9 if text_required else 7))))
    if type(row["minimum_reader_version"]) is not int or row["minimum_reader_version"] < required_reader:
        raise WorkflowValidationError("invalid_native_memory_reader")
    provider_snapshot = config.get("provider_snapshot")
    capabilities = provider_snapshot.get("capabilities") if type(provider_snapshot) is dict else None
    if type(capabilities) is not dict:
        raise WorkflowValidationError("invalid_native_media_mode")
    if ((snapshot.uses_media and capabilities.get("image_input") is not True)
            or (snapshot.uses_audio and capabilities.get("audio_input") is not True)
            or (snapshot.uses_video and capabilities.get("video_input") is not True)):
        raise WorkflowValidationError("invalid_native_media_mode")
    if snapshot.summaries and row["minimum_reader_version"] < 11:
        raise WorkflowValidationError("invalid_native_memory_reader")
    if snapshot.codes and row["minimum_reader_version"] < 12:
        raise WorkflowValidationError("invalid_native_memory_reader")
    if bool(snapshot.texts) != text_required:
        raise WorkflowValidationError("invalid_native_text_mode")
    if bool(snapshot.codes) != code_required:
        raise WorkflowValidationError("invalid_native_code_mode")
    if (snapshot.uses_media or snapshot.uses_audio or snapshot.uses_video) != media_required:
        raise WorkflowValidationError("invalid_native_media_mode")
    if (resolved.owner_id, resolved.workspace_id, resolved.digest) != (row["owner_id"], row["workspace_id"], row["payload_hash"]):
        raise WorkflowValidationError("native_memory_mismatch")
    identity = (row["run_owner"], row["run_id"], row["trigger_message_id"], row["binding_run_id"],
                row["message_owner"], row["message_conversation"], row["conversation_workspace"], row["client_token"], row["role"], row["content"])
    if identity != (row["owner_id"], "wf-run-" + row["token"], "wf-msg-" + row["token"], "wf-run-" + row["token"],
            row["owner_id"], row["conversation_id"], row["workspace_id"], row["token"], "user", resolved.snapshot.decode("utf-8")):
        raise WorkflowValidationError("native_memory_mismatch")
    if (config.get("owner_id"), config.get("workspace_id")) != (row["owner_id"], row["workspace_id"]):
        raise WorkflowValidationError("native_memory_mismatch")
    if (config.get("execution_node_id") not in (None, "", "local", "hub", "host")
            or {"memory_context", "workflow_test", "workflow_source", "workflow_review", "workflow_capability"} & config.keys()
            or resolved.contract.capability_requirements):
        raise WorkflowValidationError("unsupported_native_memory_target")
    marker = config.pop("workflow_dispatch", None)
    adapter_ref = decode_snapshot(resolved.snapshot)["adapter_ref"]
    digest = canonical_digest({"target_key": row["target_key"], "adapter_ref": adapter_ref,
        "conversation_id": row["conversation_id"], "config": config, "legacy_request": None})
    if marker != {"token": row["token"], "payload_hash": resolved.digest, "config_digest": digest}:
        raise WorkflowValidationError("native_memory_configuration_mismatch")
    if row["stage"] == "bound" and (row["binding_kind"], row["binding_id"]) != ("run", row["run_id"]):
        raise WorkflowValidationError("native_memory_mismatch")
    return snapshot


class WorkflowNativeMemoryRepository:
    def __init__(self, workflows: WorkflowRepository, *, text_inputs: WorkflowTextInputs | None = None,
                 code_inputs: WorkflowCodeInputs | None = None, media_inputs: WorkflowMediaInputs | None = None,
                 clock: Callable[[], float] | None = None) -> None:
        self.workflows = workflows
        self.text_inputs = text_inputs
        self.code_inputs = code_inputs
        self.media_inputs = media_inputs
        self.clock = clock

    def prepare(self, claim: dict[str, Any], *, now: float) -> NativeWorkflowMemory | None:
        try:
            snapshot, resolved = self._prepare(claim, now=_time(now))
            if snapshot is None or not (snapshot.texts or snapshot.codes or snapshot.media or snapshot.audio or snapshot.video):
                return snapshot
            if self.clock is None or resolved is None:
                code = "native_video_guard_unavailable" if snapshot.video else ("native_audio_guard_unavailable" if snapshot.audio else ("native_media_guard_unavailable" if snapshot.media else ("native_code_guard_unavailable" if snapshot.codes else "native_text_guard_unavailable")))
                raise WorkflowValidationError(code)
            if snapshot.texts:
                if self.text_inputs is None:
                    raise WorkflowValidationError("native_text_guard_unavailable")
                if snapshot.codes:
                    self.text_inputs.authorize(resolved, snapshot.texts, references=tuple(
                        named for named in resolved.contract.input_refs
                        if type(named.reference) is not CodeRevisionReference))
                else:
                    self.text_inputs.authorize(resolved, snapshot.texts)
            if snapshot.codes:
                if self.code_inputs is None:
                    raise WorkflowValidationError("native_code_guard_unavailable")
                self.code_inputs.authorize(resolved, snapshot.codes)
            if snapshot.media:
                if self.media_inputs is None:
                    raise WorkflowValidationError("native_media_guard_unavailable")
                self.media_inputs.authorize(resolved, snapshot.media)
            if snapshot.audio:
                if self.media_inputs is None:
                    raise WorkflowValidationError("native_audio_guard_unavailable")
                capabilities = (claim.get("config_snapshot", {}).get("provider_snapshot", {}) or {}).get("capabilities", {})
                self.media_inputs.authorize(resolved, snapshot.audio, capabilities=capabilities)
            if snapshot.video:
                if self.media_inputs is None:
                    raise WorkflowValidationError("native_video_guard_unavailable")
                capabilities = (claim.get("config_snapshot", {}).get("provider_snapshot", {}) or {}).get("capabilities", {})
                self.media_inputs.authorize(resolved, snapshot.video, capabilities=capabilities)
            current, current_resolved = self._prepare(claim, now=_time(self.clock()))
            if current != snapshot or current_resolved != resolved:
                raise WorkflowValidationError("native_memory_mismatch")
            return current
        except WorkflowNativeMemoryError:
            raise
        except (WorkflowValidationError, WorkflowPersistenceError, WorkflowSchemaError, sqlite3.Error, KeyError, TypeError, ValueError) as exc:
            code = getattr(exc, "code", "native_memory_unavailable")
            raise WorkflowNativeMemoryError(code) from exc

    def load_media(self, claim: dict[str, Any], snapshot: NativeWorkflowMemory, *,
                   now: float) -> tuple[dict[str, Any], ...]:
        """Read outside SQL, then recheck the frozen input and current execution."""
        try:
            current, resolved = self._prepare(claim, now=_time(now))
            if current != snapshot or not snapshot.uses_media or resolved is None:
                raise WorkflowValidationError("invalid_native_media_run")
            trigger = claim.get("message")
            if (type(trigger) is not dict or
                    (trigger.get("message_id"), trigger.get("role"), trigger.get("content")) !=
                    (claim.get("trigger_message_id"), "user", resolved.snapshot.decode("utf-8"))):
                raise WorkflowValidationError("native_memory_mismatch")
            if self.media_inputs is None or self.clock is None:
                raise WorkflowValidationError("native_media_guard_unavailable")
            capabilities = (claim.get("config_snapshot", {}).get("provider_snapshot", {}) or {}).get("capabilities", {})
            parts = self.media_inputs.load(resolved, snapshot.media, capabilities=capabilities)
            self.media_inputs.authorize(resolved, snapshot.media, capabilities=capabilities)
            current, current_resolved = self._prepare(claim, now=_time(self.clock()))
            if current != snapshot or current_resolved != resolved:
                raise WorkflowValidationError("native_memory_mismatch")
            return parts
        except WorkflowNativeMemoryError:
            raise
        except (WorkflowValidationError, WorkflowPersistenceError, WorkflowSchemaError, sqlite3.Error, KeyError, TypeError, ValueError, OSError) as exc:
            code = getattr(exc, "code", "native_memory_unavailable")
            raise WorkflowNativeMemoryError(code) from exc

    def load_audio(self, claim: dict[str, Any], snapshot: NativeWorkflowMemory, *,
                   now: float) -> tuple[dict[str, Any], ...]:
        """Read bounded audio outside SQL and recheck its immutable binding afterward."""
        try:
            current, resolved = self._prepare(claim, now=_time(now))
            if current != snapshot or not snapshot.uses_audio or resolved is None:
                raise WorkflowValidationError("invalid_native_audio_run")
            trigger = claim.get("message")
            if (type(trigger) is not dict or
                    (trigger.get("message_id"), trigger.get("role"), trigger.get("content")) !=
                    (claim.get("trigger_message_id"), "user", resolved.snapshot.decode("utf-8"))):
                raise WorkflowValidationError("native_memory_mismatch")
            if self.media_inputs is None or self.clock is None:
                raise WorkflowValidationError("native_audio_guard_unavailable")
            capabilities = (claim.get("config_snapshot", {}).get("provider_snapshot", {}) or {}).get("capabilities", {})
            parts = self.media_inputs.load_audio(resolved, snapshot.audio, capabilities=capabilities)
            self.media_inputs.authorize(resolved, snapshot.audio, capabilities=capabilities)
            current, current_resolved = self._prepare(claim, now=_time(self.clock()))
            if current != snapshot or current_resolved != resolved:
                raise WorkflowValidationError("native_memory_mismatch")
            return parts
        except WorkflowNativeMemoryError:
            raise
        except (WorkflowValidationError, WorkflowPersistenceError, WorkflowSchemaError, sqlite3.Error,
                KeyError, TypeError, ValueError, OSError) as exc:
            code = getattr(exc, "code", "native_memory_unavailable")
            raise WorkflowNativeMemoryError(code) from exc

    def load_video(self, claim: dict[str, Any], snapshot: NativeWorkflowMemory, *,
                   now: float) -> tuple[dict[str, Any], ...]:
        """Read bounded video outside SQL and recheck its immutable binding afterward."""
        try:
            current, resolved = self._prepare(claim, now=_time(now))
            if current != snapshot or not snapshot.uses_video or resolved is None:
                raise WorkflowValidationError("invalid_native_video_run")
            trigger = claim.get("message")
            if (type(trigger) is not dict or
                    (trigger.get("message_id"), trigger.get("role"), trigger.get("content")) !=
                    (claim.get("trigger_message_id"), "user", resolved.snapshot.decode("utf-8"))):
                raise WorkflowValidationError("native_memory_mismatch")
            if self.media_inputs is None or self.clock is None:
                raise WorkflowValidationError("native_video_guard_unavailable")
            capabilities = (claim.get("config_snapshot", {}).get("provider_snapshot", {}) or {}).get("capabilities", {})
            parts = self.media_inputs.load_video(resolved, snapshot.video, capabilities=capabilities)
            self.media_inputs.authorize(resolved, snapshot.video, capabilities=capabilities)
            current, current_resolved = self._prepare(claim, now=_time(self.clock()))
            if current != snapshot or current_resolved != resolved:
                raise WorkflowValidationError("native_memory_mismatch")
            return parts
        except WorkflowNativeMemoryError:
            raise
        except (WorkflowValidationError, WorkflowPersistenceError, WorkflowSchemaError, sqlite3.Error,
                KeyError, TypeError, ValueError, OSError) as exc:
            code = getattr(exc, "code", "native_memory_unavailable")
            raise WorkflowNativeMemoryError(code) from exc

    def _prepare(self, claim: dict[str, Any], *, now: float) -> tuple[NativeWorkflowMemory | None, ResolvedContract | None]:
        config = claim.get("config_snapshot")
        if not isinstance(config, Mapping):
            raise WorkflowValidationError("invalid_native_memory_run")
        marker = config.get("workflow_dispatch")
        if marker is None and not {NATIVE_MEMORY_MARKER, NATIVE_TEXT_MODE_MARKER, NATIVE_CODE_MODE_MARKER, NATIVE_MEDIA_MODE_MARKER} & config.keys():
            if str(claim.get("run_id", "")).startswith("wf-run-"):
                raise WorkflowValidationError("invalid_native_memory_run")
            return None, None
        if type(marker) is not dict or type(marker.get("token")) is not str:
            raise WorkflowValidationError("invalid_native_memory_run")
        with self.workflows.transaction(write=False) as connection:
            inspect_schema(connection)
            row = _row(connection, marker["token"])
            if row is None:
                raise WorkflowValidationError("native_memory_binding_missing")
            snapshot = _saved(row)
            _text_reference_closure(connection, [(row, snapshot)])
            _summary_reference_closure(connection, [(row, snapshot)])
            if snapshot is None:
                return None, None
            if (claim.get("owner_id"), claim.get("run_id"), claim.get("lease_id"), claim.get("lease_owner"), claim.get("attempt")) != (
                    row["owner_id"], row["run_id"], row["lease_id"], row["lease_owner"], row["attempt"]):
                raise WorkflowValidationError("native_memory_lease_mismatch")
            if config != decode_snapshot(row["config_snapshot"].encode("utf-8")):
                raise WorkflowValidationError("native_memory_configuration_mismatch")
            if row["stage"] == "sending" and row["binding_kind"] is None and row["binding_id"] is None:
                raise WorkflowNativeMemoryError("binding_not_ready")
            if (row["stage"] != "bound" or row["is_current"] != 1 or row["step_state"] not in ("running", "waiting_external")
                    or row["run_state"] != "running" or not row["lease_id"] or row["lease_expires_at"] is None
                    or row["lease_expires_at"] <= now or row["active_deadline"] is None or row["active_deadline"] <= now
                    or row["workspace_enabled"] != 1 or row["run_cancel"] or row["cancel_requested"]
                    or row["pause_requested"] or row["admission_mode"] != "open"):
                raise WorkflowValidationError("native_memory_execution_forbidden")
            resolved = parse_resolved(bytes(row["resolved_snapshot"]))
            current = WorkflowMemoryRepository(self.workflows).read_selected(row["owner_id"],
                tuple(named.reference for named in resolved.contract.context_refs if named.reference.kind == "memory"), connection=connection)
            if current != tuple(item for _key, item in snapshot.inputs):
                raise WorkflowValidationError("memory_selection_stale")
            selected = WorkflowSummaryRepository(self.workflows).read_selected(row["owner_id"], row["workspace_id"],
                tuple(named.reference for named in resolved.contract.context_refs if named.reference.kind == "summary"), connection=connection)
            if selected != tuple(item for _key, item in snapshot.summaries):
                raise WorkflowValidationError("summary_selection_stale")
            return snapshot, resolved


def validate_native_memory_runs(connection: sqlite3.Connection) -> None:
    """Bounded historical closure; never require current Memory values."""
    metadata = inspect_schema(connection)
    if metadata is None:
        return
    for _page in native_memory_history(connection):
        # Validation is performed while reading each page; keep no history cache.
        continue
