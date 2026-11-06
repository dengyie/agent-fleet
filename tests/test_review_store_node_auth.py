"""Node credential auth: cheap digest, legacy PBKDF2 migration, write
throttling and failure rate limiting."""

import hashlib
import sqlite3
from unittest import mock

import pytest

from hub.infrastructure import platform_db_node_auth
from hub.infrastructure.platform_db import PlatformRepository

OWNER = "owner-a@example.test"
SECRET = "s" * 40


@pytest.fixture
def repo(tmp_path):
    r = PlatformRepository(tmp_path / "platform.db")
    r.init()
    r.upsert_node(OWNER, {"node_id": "node-a", "label": "A", "capabilities": {}})
    r.provision_node_credential(OWNER, "node-a", secret=SECRET)
    return r


def _cred(repo):
    with sqlite3.connect(repo.db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM node_credentials WHERE node_id='node-a'").fetchone()


def test_authentication_does_not_run_pbkdf2(repo):
    with mock.patch.object(platform_db_node_auth.hashlib, "pbkdf2_hmac",
                           side_effect=AssertionError("slow KDF on hot path")):
        assert repo.authenticate_node_credential("node-a", SECRET)["owner_id"] == OWNER
        assert repo.authenticate_node_credential("node-a", "x" * 40) is None


def test_legacy_pbkdf2_credential_is_verified_once_then_rehashed(repo):
    with sqlite3.connect(repo.db_path) as conn:
        conn.execute("UPDATE node_credentials SET fast_digest=NULL")
    legacy = bytes(_cred(repo)["secret_digest"])
    calls = []
    real = hashlib.pbkdf2_hmac

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    with mock.patch.object(platform_db_node_auth.hashlib, "pbkdf2_hmac", side_effect=counting):
        assert repo.authenticate_node_credential("node-a", SECRET) is not None
        assert repo.authenticate_node_credential("node-a", SECRET) is not None
    assert len(calls) == 1
    row = _cred(repo)
    assert row["fast_digest"] is not None
    # 旧 PBKDF2 摘要保留：回滚到旧版本时节点仍能认证
    assert bytes(row["secret_digest"]) == legacy
    assert repo._derive_node_digest(SECRET, bytes(row["salt"])) == legacy


def test_last_used_writes_are_throttled(repo):
    clock = [1_000_000.0]
    with mock.patch.object(platform_db_node_auth.time, "time", side_effect=lambda: clock[0]):
        repo.authenticate_node_credential("node-a", SECRET)
        first = _cred(repo)["last_used_at"]
        clock[0] += 30
        repo.authenticate_node_credential("node-a", SECRET)
        assert _cred(repo)["last_used_at"] == first
        clock[0] += 31
        repo.authenticate_node_credential("node-a", SECRET)
        assert _cred(repo)["last_used_at"] == clock[0]
        # 节点被标记非 online 时立即写回
        with sqlite3.connect(repo.db_path) as conn:
            conn.execute("UPDATE nodes SET status='degraded'")
        clock[0] += 1
        repo.authenticate_node_credential("node-a", SECRET)
        assert _cred(repo)["last_used_at"] == clock[0]
        with sqlite3.connect(repo.db_path) as conn:
            assert conn.execute("SELECT status FROM nodes").fetchone()[0] == "online"


def test_failed_attempts_are_rate_limited_per_node_and_ip(repo):
    mono = [500.0]
    with mock.patch.object(platform_db_node_auth.time, "monotonic", side_effect=lambda: mono[0]):
        for _ in range(platform_db_node_auth.NODE_AUTH_MAX_FAILURES):
            assert repo.authenticate_node_credential("node-a", "bad" * 12, remote_addr="203.0.113.9") is None
        # 达到上限后即便密钥正确也拒绝，且不触库
        with mock.patch.object(repo, "_connect", side_effect=AssertionError("db hit")):
            assert repo.authenticate_node_credential("node-a", SECRET) is None
        mono[0] += platform_db_node_auth.NODE_AUTH_FAILURE_WINDOW_S + 1
        assert repo.authenticate_node_credential("node-a", SECRET) is not None
        # IP 维度：同一来源对其他 node 也被限流
        for _ in range(platform_db_node_auth.NODE_AUTH_MAX_FAILURES):
            repo.authenticate_node_credential("node-z", "bad" * 12, remote_addr="198.51.100.7")
        with mock.patch.object(repo, "_connect", side_effect=AssertionError("db hit")):
            assert repo.authenticate_node_credential("node-a", SECRET, remote_addr="198.51.100.7") is None
        assert repo.authenticate_node_credential("node-a", SECRET, remote_addr="192.0.2.1") is not None


def test_limiter_memory_is_bounded(repo):
    with mock.patch.object(platform_db_node_auth, "NODE_AUTH_LIMITER_MAX_KEYS", 8):
        for i in range(50):
            repo._record_auth_failure([("node", f"n{i}")], 1.0)
        assert len(repo._auth_failures) <= 8
