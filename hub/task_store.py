"""Compatibility facade for task/lease SQLite persistence.

The implementation lives in ``hub.infrastructure.task_repository`` and is
bound to an explicit database path. This module keeps the legacy module-level
API and patchable ``DB_PATH`` used by existing routes, tests, and CLI code.
"""

import contextlib
import sqlite3
from pathlib import Path

from hub.infrastructure.task_repository import (
    AGENT_TYPES,
    LEASE_TTL_S,
    SCHEMA,
    TASK_STATES,
    TASK_TTL_S,
    SqliteTaskRepository,
    _audit,
    _iso,
    _tx,
)

FLEET_HOME = Path(__file__).resolve().parent.parent
DB_PATH = FLEET_HOME / "state" / "fleet.db"

_default_db_path = None
_default_repository = None


def _repository():
    global _default_db_path, _default_repository
    if _default_repository is None or _default_db_path != DB_PATH:
        _default_repository = SqliteTaskRepository(DB_PATH)
        _default_db_path = DB_PATH
    return _default_repository


def _connect():
    """Legacy low-level hook; new code should use ``SqliteTaskRepository``."""
    return _repository()._connect()


def init_db():
    return _repository().init()


def create_task(*, machine, agent_type, project, instruction, requested_by,
                client_token=None, ttl_s=TASK_TTL_S, now=None, confirm=False):
    return _repository().create_task(
        machine=machine, agent_type=agent_type, project=project,
        instruction=instruction, requested_by=requested_by,
        client_token=client_token, ttl_s=ttl_s, now=now, confirm=confirm,
    )


def get_task(task_id):
    return _repository().get_task(task_id)


def list_tasks(machine=None, state=None, limit=50):
    return _repository().list_tasks(machine=machine, state=state, limit=limit)


def lease_task(*, machine, runner_id, lease_ttl_s=LEASE_TTL_S, now=None):
    return _repository().lease_task(
        machine=machine, runner_id=runner_id, lease_ttl_s=lease_ttl_s, now=now,
    )


def heartbeat(*, attempt_id, nonce, extend_s=LEASE_TTL_S, now=None):
    return _repository().heartbeat(
        attempt_id=attempt_id, nonce=nonce, extend_s=extend_s, now=now,
    )


def complete_task(*, attempt_id, nonce, exit_code, log_summary, diff_stat,
                  duration_s, now=None, files=None, diff_patch=None, test_summary=None):
    return _repository().complete_task(
        attempt_id=attempt_id, nonce=nonce, exit_code=exit_code,
        log_summary=log_summary, diff_stat=diff_stat, duration_s=duration_s,
        now=now, files=files, diff_patch=diff_patch, test_summary=test_summary,
    )


def list_result_files(task_id):
    return _repository().list_result_files(task_id)


def get_result_file(task_id, path):
    return _repository().get_result_file(task_id, path)


def audit_action(actor, action, task_id, detail=None, now=None):
    return _repository().audit_action(
        actor, action, task_id, detail=detail, now=now,
    )


def expire_leases(now=None):
    return _repository().expire_leases(now=now)


def expire_tasks(now=None):
    return _repository().expire_tasks(now=now)


def cancel_task(task_id, actor, now=None):
    return _repository().cancel_task(task_id, actor, now=now)


def retry_task(task_id, actor, now=None):
    return _repository().retry_task(task_id, actor, now=now)


def pause_task(task_id, actor, now=None):
    return _repository().pause_task(task_id, actor, now=now)


def continue_task(task_id, actor, now=None):
    return _repository().continue_task(task_id, actor, now=now)


def confirm_task(task_id, actor, now=None):
    return _repository().confirm_task(task_id, actor, now=now)


def reject_task(task_id, actor, now=None):
    return _repository().reject_task(task_id, actor, now=now)


def get_task_gate(task_id):
    return _repository().get_task_gate(task_id)
