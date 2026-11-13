"""Owner-scoped, single-use approval grant lifecycle."""
from __future__ import annotations

import pytest

from hub.infrastructure.approval_repository import ApprovalRepository, ApprovalRepositoryError

OWNER = "alice@example.com"
SERVICE = {"service_id": "svc-1", "version": 3, "node_id": "node-a",
           "adapter": "systemd", "target_alias": "web"}
HASH = "a" * 64


class Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def repo(tmp_path):
    clock = Clock()
    repository = ApprovalRepository(tmp_path / "fleet.db", clock=clock)
    repository.init()
    repository.init()  # idempotent
    return repository, clock


def _create(repository, **overrides):
    kwargs = dict(actor="alice", service=SERVICE, action="restart", arguments={"why": "deploy"},
                  args_hash=HASH, policy_version="p1", expires_at=1600.0)
    kwargs.update(overrides)
    return repository.create(OWNER, **kwargs)


def _code(fn, *args, **kwargs) -> str:
    with pytest.raises(ApprovalRepositoryError) as caught:
        fn(*args, **kwargs)
    return caught.value.code


def test_grant_lifecycle_approve_consume_once(repo):
    repository, _ = repo
    grant = _create(repository)
    assert grant["state"] == "pending" and grant["remaining_uses"] == 1
    assert grant["arguments"] == {"why": "deploy"}
    assert repository.decide(OWNER, grant["grant_id"], approve=True)["state"] == "approved"
    assert _code(repository.decide, OWNER, grant["grant_id"], approve=True) == "grant_not_pending"

    consuming = repository.begin_consume(OWNER, grant["grant_id"], command_id="cmd-1")
    assert consuming["state"] == "consuming" and consuming["command_id"] == "cmd-1"
    # Replays of the same command are idempotent; another command cannot reuse it.
    assert repository.begin_consume(OWNER, grant["grant_id"], command_id="cmd-1")["state"] == "consuming"
    assert _code(repository.begin_consume, OWNER, grant["grant_id"], command_id="cmd-2") == "grant_not_usable"

    done = repository.finish_consume(OWNER, grant["grant_id"], command_id="cmd-1", success=True)
    assert done["state"] == "consumed" and done["remaining_uses"] == 0
    assert repository.finish_consume(OWNER, grant["grant_id"], command_id="cmd-1", success=True)["state"] == "consumed"
    assert _code(repository.begin_consume, OWNER, grant["grant_id"], command_id="cmd-3") == "grant_not_usable"


def test_failed_consume_returns_grant_to_approved(repo):
    repository, _ = repo
    grant = _create(repository)
    repository.decide(OWNER, grant["grant_id"], approve=True)
    repository.begin_consume(OWNER, grant["grant_id"], command_id="cmd-1")
    assert _code(repository.finish_consume, OWNER, grant["grant_id"], command_id="other", success=False) == "grant_state_conflict"
    back = repository.finish_consume(OWNER, grant["grant_id"], command_id="cmd-1", success=False)
    assert back["state"] == "approved" and back["remaining_uses"] == 1
    assert _code(repository.finish_consume, OWNER, grant["grant_id"], command_id="", success=True) == "invalid_command"


def test_rejected_grant_is_not_usable(repo):
    repository, _ = repo
    grant = _create(repository)
    assert repository.decide(OWNER, grant["grant_id"], approve=False)["state"] == "rejected"
    assert _code(repository.begin_consume, OWNER, grant["grant_id"], command_id="cmd-1") == "grant_not_usable"


def test_expiry_is_persisted_on_decide_and_consume(repo):
    repository, clock = repo
    pending = _create(repository)
    approved = _create(repository)
    repository.decide(OWNER, approved["grant_id"], approve=True)
    clock.now = 1600.0
    assert _code(repository.decide, OWNER, pending["grant_id"], approve=True) == "approval_expired"
    assert repository.get(OWNER, pending["grant_id"])["state"] == "expired"
    assert _code(repository.begin_consume, OWNER, approved["grant_id"], command_id="cmd-1") == "approval_expired"
    assert repository.get(OWNER, approved["grant_id"])["state"] == "expired"


def test_grants_are_owner_scoped(repo):
    repository, _ = repo
    grant = _create(repository)
    assert repository.get("mallory@example.com", grant["grant_id"]) is None
    assert _code(repository.decide, "mallory@example.com", grant["grant_id"], approve=True) == "grant_not_found"
    assert _code(repository.begin_consume, "mallory@example.com", grant["grant_id"], command_id="c") == "grant_not_found"
    assert repository.get(OWNER, grant["grant_id"])["state"] == "pending"


def test_idempotency_key_returns_prior_grant_or_conflicts(repo):
    repository, _ = repo
    first = _create(repository, idempotency_key="req-1")
    assert _create(repository, idempotency_key="req-1")["grant_id"] == first["grant_id"]
    assert _code(_create, repository, idempotency_key="req-1", action="inspect") == "idempotency_conflict"
    assert _code(_create, repository, idempotency_key="req-1", args_hash="b" * 64) == "idempotency_conflict"
    # The key is scoped per owner.
    other = repository.create("bob@example.com", actor="bob", service=SERVICE, action="restart",
                              arguments={}, args_hash=HASH, policy_version="p1", expires_at=1600.0,
                              idempotency_key="req-1")
    assert other["grant_id"] != first["grant_id"]


@pytest.mark.parametrize(("overrides", "code"), [
    ({"actor": ""}, "invalid_actor"),
    ({"action": "delete"}, "unsupported_action"),
    ({"expires_at": 1000.0}, "invalid_expiry"),
    ({"expires_at": float("inf")}, "invalid_expiry"),
    ({"expires_at": "soon"}, "invalid_grant"),
    ({"policy_version": ""}, "invalid_expiry"),
    ({"args_hash": "A" * 64}, "invalid_grant"),
    ({"args_hash": "a" * 63}, "invalid_grant"),
    ({"service": dict(SERVICE, node_id="")}, "invalid_grant"),
    ({"service": {k: v for k, v in SERVICE.items() if k != "version"}}, "invalid_grant"),
    ({"arguments": ["not", "a", "mapping"]}, "invalid_arguments"),
    ({"arguments": {"x": object()}}, "invalid_arguments"),
    ({"arguments": {"x": "y" * 17000}}, "arguments_too_large"),
    ({"idempotency_key": ""}, "invalid_idempotency_key"),
])
def test_create_validates_input(repo, overrides, code):
    repository, _ = repo
    assert _code(_create, repository, **overrides) == code


def test_store_failure_is_reported_as_approval_store(tmp_path):
    unusable = tmp_path / "fleet.db"
    unusable.mkdir()  # sqlite cannot open a directory as a database
    repository = ApprovalRepository(unusable)
    assert _code(repository.init) == "approval_store"
    assert _code(repository.get, OWNER, "grant_x") == "approval_store"


def test_init_parent_mkdir_failure_is_reported_as_approval_store(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")  # 父目录位置是普通文件 → mkdir 抛 OSError
    repository = ApprovalRepository(blocker / "sub" / "fleet.db")
    assert _code(repository.init) == "approval_store"
