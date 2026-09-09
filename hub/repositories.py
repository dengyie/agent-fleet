"""Protocols for the hub observation domain.

Repository implementations are injected with an explicit ``Path`` and must not
depend on Flask, request globals, or any network behavior. The hub only
processes pushed observations; repositories never connect to agents or execute
remote commands.
"""

import re
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

# Machine-name safety contract shared with the auth layer
# (hub/auth.MACHINE_RE). Rejected names never escape ``state_dir``.
MACHINE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def validate_machine_name(machine: str) -> str:
    """Return the machine name if safe, else raise ``ValueError``.

    Mirrors the auth machine-name contract so repository paths can never
    traverse out of the state directory.
    """
    if not isinstance(machine, str) or not MACHINE_NAME_RE.fullmatch(machine):
        raise ValueError(f"unsafe machine name: {machine!r}")
    return machine


@runtime_checkable
class TaskRepository(Protocol):
    """Persistence contract for queued tasks and fenced runner attempts."""

    def init(self) -> None: ...
    def create_task(self, **kwargs: Any) -> tuple[dict, bool]: ...
    def get_task(self, task_id: str) -> dict | None: ...
    def list_tasks(self, machine=None, state=None, limit: int = 50) -> list[dict]: ...
    def lease_task(self, **kwargs: Any) -> dict | None: ...
    def heartbeat(self, **kwargs: Any) -> dict | None: ...
    def complete_task(self, **kwargs: Any) -> dict | None: ...
    def list_result_files(self, task_id: str) -> list[dict]: ...
    def get_result_file(self, task_id: str, path: str) -> dict | None: ...
    def audit_action(self, actor: str, action: str, task_id: str,
                     detail=None, now=None) -> None: ...
    def expire_leases(self, now=None) -> list[str]: ...
    def expire_tasks(self, now=None) -> list[str]: ...
    def cancel_task(self, task_id: str, actor: str, now=None) -> tuple[dict | None, bool]: ...
    def retry_task(self, task_id: str, actor: str, now=None) -> tuple[dict | None, bool]: ...


@runtime_checkable
class ObservationRepository(Protocol):
    """Persistent observation store for per-machine push snapshots.

    Wire payloads are preserved verbatim (plus the legacy ``_ts`` stamp).
    """

    def save_snapshot(self, machine: str, snapshot: dict) -> None: ...

    def read_current(self, machine: str) -> dict | None: ...

    def read_history(self, machine: str, limit: int = 50) -> list[dict]: ...

    def machines(self) -> set[str]: ...
