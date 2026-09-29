"""Durable bridge from platform Runs to the legacy coding-task service.

The legacy task database and platform database are intentionally separate. A
platform row therefore acts as an outbox and relation: a retry always derives
the same client token, asks the old TaskService to create/query that task, and
then projects only bounded result metadata back into private Run events.
"""
from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping
from typing import Any, Callable

from hub.application.task_service import ApplicationError
from hub.domain.task import DomainError, validate_task_input
from platform_schema import PlatformValidationError, validate_id, validate_owner_id
from session_schema import is_valid_session_id


MAX_SUMMARY = 2000
MAX_FILES = 50
MAX_FILE_PATH = 256
MAX_ERROR = 120
RETRYABLE_ERRORS = frozenset({"tasks_unavailable", "platform_store"})
NONTERMINAL_TASK_STATES = frozenset({"queued", "leased", "running", "paused"})


def legacy_client_token(owner_id: str, run_id: str) -> str:
    """Return a stable, bounded token that is unique across owner/run pairs."""
    owner = validate_owner_id(owner_id)
    run = validate_id(run_id, "run_id")
    digest = hashlib.sha256((owner + "\0" + run).encode("utf-8")).hexdigest()
    return "legacy-run-" + digest


class LegacyTaskBridge:
    """Create and reconcile one legacy Task for an explicit platform Run."""

    def __init__(self, platform_repository, task_service, task_repository, *, clock: Callable[[], float] = time.time, worker_id: str = "legacy-bridge"):
        self.repository = platform_repository
        self.tasks = task_service
        self.task_repository = task_repository
        self.clock = clock
        self.worker_id = str(worker_id)[:128] or "legacy-bridge"

    @staticmethod
    def _translate(exc: Exception) -> ApplicationError:
        code = getattr(exc, "code", "platform_store")
        status = {
            "run_not_found": 404,
            "legacy_task_not_found": 404,
            "legacy_task_conflict": 409,
            "legacy_task_lease_mismatch": 409,
            "invalid_legacy_task": 400,
            "invalid_instruction": 400,
            "invalid_machine": 400,
            "invalid_project": 400,
            "invalid_agent_type": 400,
            "invalid_confirm": 400,
            "message_too_large": 413,
        }.get(code, 503)
        detail = {
            "run_not_found": "运行不存在",
            "legacy_task_not_found": "旧任务桥接不存在",
            "legacy_task_conflict": "运行已有不同的旧任务桥接",
            "legacy_task_lease_mismatch": "旧任务桥接租约已失效",
            "invalid_instruction": "旧任务说明长度须为 1-2000 字符",
            "message_too_large": "旧任务说明超出限制",
            "tasks_unavailable": "旧任务存储不可用",
            "platform_store": "平台存储不可用",
        }.get(code, "旧任务桥接失败")
        return ApplicationError(code, detail, status)

    @staticmethod
    def _public(row: Mapping[str, Any]) -> dict[str, Any]:
        projection = row.get("projection") or {}
        if not isinstance(projection, Mapping):
            projection = {}
        return {
            "run_id": row.get("run_id"),
            "state": row.get("state"),
            "task_id": row.get("task_id"),
            "attempt_id": row.get("attempt_id"),
            "session_id": row.get("session_id") if is_valid_session_id(row.get("session_id")) else None,
            "task_state": row.get("task_state"),
            "bridge_attempt": int(row.get("bridge_attempt") or 0),
            "error_code": row.get("error_code"),
            "projection": dict(projection),
            "updated_at": row.get("updated_at"),
        }

    @staticmethod
    def _normalize_request(request: Mapping[str, Any]) -> dict[str, Any]:
        try:
            normalized = validate_task_input(request)
        except DomainError as exc:
            raise ApplicationError(exc.code, exc.detail, 413 if exc.code == "invalid_instruction" else 400) from None
        # The platform row owns the stable token; a caller cannot replace it.
        return {
            "machine": normalized["machine"],
            "agent_type": normalized["agent_type"],
            "project": normalized["project"],
            "instruction": normalized["instruction"],
            "confirm": bool(normalized.get("confirm")),
        }

    def enqueue(self, owner_id: str, run_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        run_id = validate_id(run_id, "run_id")
        normalized = self._normalize_request(request)
        try:
            row = self.repository.enqueue_legacy_task(
                owner_id, run_id, normalized, now=float(self.clock()),
            )
            return self._public(row)
        except ApplicationError:
            raise
        except Exception as exc:
            raise self._translate(exc) from None

    def get(self, owner_id: str, run_id: str, *, refresh: bool = True) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        run_id = validate_id(run_id, "run_id")
        try:
            row = self.repository.get_legacy_task_link(owner_id, run_id)
            if row is None:
                raise ApplicationError("legacy_task_not_found", "旧任务桥接不存在", 404)
            if refresh and row["state"] in {"pending", "linked"}:
                self.process_once(owner_id, run_id=run_id)
                row = self.repository.get_legacy_task_link(owner_id, run_id) or row
            return self._public(row)
        except ApplicationError:
            raise
        except Exception as exc:
            raise self._translate(exc) from None

    def process_once(self, owner_id: str, *, run_id: str | None = None) -> dict[str, Any] | None:
        owner_id = validate_owner_id(owner_id)
        try:
            claim = self.repository.claim_legacy_task(
                owner_id, worker_id=self.worker_id, now=float(self.clock()), lease_s=60.0, run_id=run_id,
            )
            if claim is None:
                return None
            return self._process_claim(claim)
        except Exception as exc:
            if isinstance(exc, ApplicationError):
                raise
            raise self._translate(exc) from None

    def _process_claim(self, claim: Mapping[str, Any]) -> dict[str, Any]:
        owner_id = claim["owner_id"]
        run_id = claim["run_id"]
        lease_attempt = int(claim["bridge_attempt"])
        try:
            task_id = claim.get("task_id")
            if not task_id:
                request = dict(claim.get("request") or {})
                request["client_token"] = legacy_client_token(owner_id, run_id)
                created = self.tasks.create(request, owner_id)
                task_id = (created.get("task") or {}).get("task_id")
                if not isinstance(task_id, str) or not task_id:
                    raise ApplicationError("tasks_unavailable", "旧任务创建结果缺少 task_id", 503)
            projection = self._project_task(task_id)
            saved, _changed = self.repository.apply_legacy_task_projection(
                owner_id, run_id, projection, now=float(self.clock()),
                worker_id=self.worker_id, lease_attempt=lease_attempt,
            )
            return self._public(saved)
        except ApplicationError as exc:
            if exc.code in RETRYABLE_ERRORS:
                return self._release_for_retry(claim, exc.code)
            projection = self._failure_projection(claim.get("task_id"), exc.code)
            saved, _changed = self.repository.apply_legacy_task_projection(
                owner_id, run_id, projection, now=float(self.clock()),
                worker_id=self.worker_id, lease_attempt=lease_attempt,
            )
            return self._public(saved)
        except (DomainError, PlatformValidationError) as exc:
            projection = self._failure_projection(claim.get("task_id"), getattr(exc, "code", "invalid_legacy_task"))
            saved, _changed = self.repository.apply_legacy_task_projection(
                owner_id, run_id, projection, now=float(self.clock()),
                worker_id=self.worker_id, lease_attempt=lease_attempt,
            )
            return self._public(saved)
        except Exception:
            return self._release_for_retry(claim, "tasks_unavailable")

    def _release_for_retry(self, claim: Mapping[str, Any], error_code: str) -> dict[str, Any]:
        saved = self.repository.release_legacy_task_claim(
            claim["owner_id"], claim["run_id"], worker_id=self.worker_id,
            lease_attempt=int(claim["bridge_attempt"]), now=float(self.clock()),
            error_code=error_code,
        )
        return self._public(saved)

    def _project_task(self, task_id: str) -> dict[str, Any]:
        detail = self.tasks.get(task_id)
        task = detail.get("task") if isinstance(detail, Mapping) else None
        if not isinstance(task, Mapping):
            raise ApplicationError("tasks_unavailable", "旧任务详情不可用", 503)
        raw = self.task_repository.get_task(task_id)
        if raw is None:
            raise ApplicationError("tasks_unavailable", "旧任务详情不可用", 503)
        state = str(task.get("state") or "")
        bridge_state = {
            "succeeded": "succeeded", "failed": "failed", "expired": "failed",
            "cancelled": "cancelled",
        }.get(state, "linked")
        result = task.get("result") if isinstance(task.get("result"), Mapping) else {}
        summary = str(result.get("log_summary") or "")[:MAX_SUMMARY]
        diff_patch_bytes = result.get("diff_patch_bytes")
        try:
            diff_patch_bytes = max(0, min(102400, int(diff_patch_bytes or 0)))
        except (TypeError, ValueError):
            diff_patch_bytes = 0
        diff = {
            "stat": str(result.get("diff_stat") or "")[:5120],
            "has_patch": bool(result.get("has_diff_patch") or result.get("diff_patch")),
            "patch_bytes": diff_patch_bytes,
        }
        tests = result.get("test_summary") if isinstance(result.get("test_summary"), Mapping) else {}
        allowed_tests = {"framework", "passed", "failed", "skipped", "errors", "duration_s", "failed_names"}
        test_projection = {}
        for key in allowed_tests:
            if key not in tests:
                continue
            value = tests[key]
            if key == "failed_names" and isinstance(value, list):
                test_projection[key] = [str(item)[:200] for item in value[:20]]
            elif key in {"framework"}:
                test_projection[key] = str(value)[:32]
            else:
                test_projection[key] = value
        files = []
        try:
            file_rows = self.tasks.list_files(task_id).get("files", [])
        except ApplicationError as exc:
            if exc.code in RETRYABLE_ERRORS:
                raise
            file_rows = []
        for item in file_rows[:MAX_FILES] if isinstance(file_rows, list) else []:
            if not isinstance(item, Mapping):
                continue
            path = str(item.get("path") or "")[:MAX_FILE_PATH]
            if not path:
                continue
            try:
                file_bytes = max(0, min(1024 * 1024 * 1024, int(item.get("bytes") or 0)))
            except (TypeError, ValueError):
                file_bytes = 0
            files.append({
                "path": path,
                "bytes": file_bytes,
                "truncated": bool(item.get("truncated")),
                "redacted": bool(item.get("redacted")),
            })
        session_id = task.get("session_id")
        if not is_valid_session_id(session_id):
            session_id = None
        return {
            "task_id": task_id,
            "attempt_id": raw.get("attempt_id"),
            "session_id": session_id,
            "task_state": state,
            "bridge_state": bridge_state,
            "summary": summary,
            "diff": diff,
            "tests": test_projection,
            "files": files,
        }

    @staticmethod
    def _failure_projection(task_id: str | None, error_code: str) -> dict[str, Any]:
        return {
            "task_id": task_id,
            "attempt_id": None,
            "session_id": None,
            "task_state": "failed",
            "bridge_state": "failed",
            "error_code": str(error_code)[:MAX_ERROR],
            "summary": "旧任务桥接失败",
            "diff": {}, "tests": {}, "files": [],
        }


__all__ = ["LegacyTaskBridge", "legacy_client_token"]
