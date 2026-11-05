"""评审修复 #1：纳管状态机在 hub 重启 / 探针拒绝 / pending 撤销下不再卡死。

- receipt 通过持久化的 supervisor 命令行解析 (session_id, action)，
  ``_tracked`` 只是缓存：重建 AdoptionService 后回执仍能推进 pending -> adopted；
- 终态拒绝（如 ``pid_not_found``）关闭 pending 行，同一进程可以再次纳管；
- 操作员可以撤销 pending 行（发一次 detach 清理）。
"""
from __future__ import annotations

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hub.application.adoption_service import AdoptionService
from hub.application.control_router import SupervisorReceiptHook
from hub.application.supervisor_service import SupervisorService
from hub.infrastructure.adoption_repository import AdoptionRepository
from hub.infrastructure.supervisor_command_repository import (
    SupervisorCommandRepository,
)
from hub.infrastructure.transcript_repository import TranscriptRepository

PID = 4242
STARTED = "2026-07-30T09:15:00Z"


class _Obs:
    def read_current(self, machine):
        return {"machine": machine, "instances": [{
            "pid": PID, "pgid": PID, "exe_path": "/usr/local/bin/codex",
            "cmdline": "codex", "agent_family": "codex",
            "native_file_path": None, "started_at": STARTED,
            "attachable": True,
        }]}


def _key() -> bytes:
    return Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption())


class _Hub:
    """One hub "process": fresh services over the SAME durable stores."""

    def __init__(self, tmp_path, key):
        inner = SupervisorService(
            signing_key=key,
            command_repository=SupervisorCommandRepository(
                tmp_path / "commands.db"))
        self.supervisor = SupervisorReceiptHook(inner, None)
        repo = AdoptionRepository(tmp_path / "adoptions.db")
        repo.init()
        transcripts = TranscriptRepository(tmp_path / "transcripts.db")
        transcripts.init()
        self.adoption = AdoptionService(_Obs(), repo, self.supervisor,
                                        transcripts)
        self.supervisor.set_adoption(self.adoption)


def test_receipt_after_hub_restart_promotes_pending(tmp_path):
    key = _key()
    first = _Hub(tmp_path, key)
    row = first.adoption.adopt("m1", PID, STARTED, "op-1")
    [envelope] = first.supervisor.poll("m1")
    assert envelope["action"] == "adopt"

    # hub restart: the in-memory ``_tracked`` cache is gone.
    second = _Hub(tmp_path, key)
    assert second.adoption._tracked == {}
    second.supervisor.receipt(envelope["command_id"], "accepted", "adopted",
                              machine="m1")
    assert second.adoption.adoption_repo.get(row.session_id).status == "adopted"


def test_rejected_adopt_closes_pending_and_allows_readopt(tmp_path):
    hub = _Hub(tmp_path, _key())
    row = hub.adoption.adopt("m1", PID, STARTED, "op-1")
    [envelope] = hub.supervisor.poll("m1")
    hub.supervisor.receipt(envelope["command_id"], "rejected",
                           "pid_not_found", machine="m1")
    assert hub.adoption.adoption_repo.get(row.session_id).status == "revoked"
    # a probe-side rejection left nothing attached: no cleanup detach.
    assert hub.supervisor.poll("m1") == []

    again = hub.adoption.adopt("m1", PID, STARTED, "op-1")
    assert again.session_id != row.session_id
    assert again.status == "pending"
    # exactly one fresh adopt for the new seat (no second bridge for the old).
    commands = hub.supervisor.poll("m1")
    assert [c["action"] for c in commands] == ["adopt"]
    assert commands[0]["target"]["session_id"] == again.session_id


def test_rejected_adopt_after_restart_also_closes_pending(tmp_path):
    key = _key()
    first = _Hub(tmp_path, key)
    row = first.adoption.adopt("m1", PID, STARTED, "op-1")
    [envelope] = first.supervisor.poll("m1")
    second = _Hub(tmp_path, key)
    second.supervisor.receipt(envelope["command_id"], "rejected",
                              "pid_not_found", machine="m1")
    assert second.adoption.adoption_repo.get(row.session_id).status == "revoked"


def test_failed_adopt_is_indeterminate_and_gets_cleanup_detach(tmp_path):
    hub = _Hub(tmp_path, _key())
    row = hub.adoption.adopt("m1", PID, STARTED, "op-1")
    [envelope] = hub.supervisor.poll("m1")
    hub.supervisor.receipt(envelope["command_id"], "failed",
                           "execution_unknown", machine="m1")
    assert hub.adoption.adoption_repo.get(row.session_id).status == "revoked"
    detach = hub.supervisor.poll("m1")
    assert [c["action"] for c in detach] == ["detach"]
    assert detach[0]["target"]["session_id"] == row.session_id


def test_rejected_retry_never_revokes_an_adopted_seat(tmp_path):
    hub = _Hub(tmp_path, _key())
    row = hub.adoption.adopt("m1", PID, STARTED, "op-1")
    [first] = hub.supervisor.poll("m1")
    hub.adoption.retry(row.session_id, "op-1")
    [second] = hub.supervisor.poll("m1")
    hub.supervisor.receipt(first["command_id"], "accepted", "adopted",
                           machine="m1")
    hub.supervisor.receipt(second["command_id"], "rejected", "pid_reused",
                           machine="m1")
    assert hub.adoption.adoption_repo.get(row.session_id).status == "adopted"


def test_operator_revoke_from_pending(tmp_path):
    hub = _Hub(tmp_path, _key())
    row = hub.adoption.adopt("m1", PID, STARTED, "op-1")
    hub.supervisor.poll("m1")
    hub.adoption.revoke(row.session_id, "op-1")
    assert hub.adoption.adoption_repo.get(row.session_id).status == "revoked"
    detach = hub.supervisor.poll("m1")
    assert [c["action"] for c in detach] == ["detach"]
    # re-adopt now possible
    again = hub.adoption.adopt("m1", PID, STARTED, "op-1")
    assert again.session_id != row.session_id


def test_non_adoption_receipt_is_untouched(tmp_path):
    hub = _Hub(tmp_path, _key())
    envelope = hub.supervisor.enqueue("m1", "sess_plain", None,
                                      "terminate_session", "op")
    hub.supervisor.poll("m1")
    result = hub.adoption.on_supervisor_receipt(
        envelope["command_id"], status="succeeded", reason="terminated")
    assert result["handled"] is False
