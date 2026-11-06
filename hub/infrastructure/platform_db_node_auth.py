"""Node credentials, heartbeats and the in-memory failed-auth limiter."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
from collections import deque
from typing import Any

from hub.infrastructure.platform_db_base import PlatformRepositoryError
from platform_schema import PlatformValidationError, validate_id, validate_owner_id

# Node credential auth runs on every node request: keep it cheap and bounded.
NODE_LAST_USED_WRITE_INTERVAL_S = 60.0
NODE_AUTH_FAILURE_WINDOW_S = 60.0
NODE_AUTH_MAX_FAILURES = 20
NODE_AUTH_LIMITER_MAX_KEYS = 4096


class PlatformNodeAuthMixin:
    @staticmethod
    def _derive_node_digest(secret: str, salt: bytes) -> bytes:
        """Legacy PBKDF2 digest (``secret_digest``); kept so a rollback to an
        older release can still authenticate nodes provisioned by this one."""
        return hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, 120_000)

    @staticmethod
    def _fast_node_digest(secret: str, salt: bytes) -> bytes:
        """Per-request digest (``fast_digest``). Node secrets are >=32 random
        characters, so a slow KDF adds no brute-force margin but costs ~100ms
        CPU on every authenticated node request."""
        return hashlib.sha256(b"fleet-node-credential:v2:" + salt + secret.encode("utf-8")).digest()

    def _auth_keys(self, node_id: str, remote_addr: str | None) -> list[tuple[str, str]]:
        keys = [("node", node_id)]
        if isinstance(remote_addr, str) and remote_addr:
            keys.append(("ip", remote_addr[:64]))
        return keys

    def _auth_rate_limited(self, keys, now: float) -> bool:
        cutoff = now - NODE_AUTH_FAILURE_WINDOW_S
        with self._auth_failures_lock:
            for key in keys:
                bucket = self._auth_failures.get(key)
                if bucket is None:
                    continue
                while bucket and bucket[0] <= cutoff:
                    bucket.popleft()
                if not bucket:
                    del self._auth_failures[key]
                elif len(bucket) >= NODE_AUTH_MAX_FAILURES:
                    return True
        return False

    def _record_auth_failure(self, keys, now: float) -> None:
        cutoff = now - NODE_AUTH_FAILURE_WINDOW_S
        with self._auth_failures_lock:
            if len(self._auth_failures) >= NODE_AUTH_LIMITER_MAX_KEYS:
                # Bound memory: drop expired buckets, then the oldest ones.
                for key in [k for k, b in self._auth_failures.items() if not b or b[-1] <= cutoff]:
                    del self._auth_failures[key]
                while len(self._auth_failures) >= NODE_AUTH_LIMITER_MAX_KEYS:
                    del self._auth_failures[next(iter(self._auth_failures))]
            for key in keys:
                bucket = self._auth_failures.setdefault(key, deque(maxlen=NODE_AUTH_MAX_FAILURES))
                bucket.append(now)

    def provision_node_credential(self, owner_id: str, node_id: str, *, secret: str | None = None) -> dict[str, Any]:
        """Rotate a node credential and return the plaintext exactly once."""
        owner_id = validate_owner_id(owner_id)
        node_id = validate_id(node_id, "node_id")
        if secret is None:
            secret = secrets.token_urlsafe(32)
        if not isinstance(secret, str) or not (32 <= len(secret) <= 256) or any(c in secret for c in (":", "\r", "\n")):
            raise PlatformRepositoryError("invalid_node_credential")
        conn = None
        try:
            conn = self._connect()
            row = conn.execute("SELECT enabled FROM nodes WHERE owner_id=? AND node_id=?", (owner_id, node_id)).fetchone()
            if not row or not bool(row["enabled"]):
                raise PlatformRepositoryError("reference_forbidden")
            salt = secrets.token_bytes(16)
            digest = self._derive_node_digest(secret, salt)
            fast = self._fast_node_digest(secret, salt)
            now = time.time()
            conn.execute("INSERT INTO node_credentials(owner_id,node_id,salt,secret_digest,fast_digest,enabled,issued_at,last_used_at) VALUES(?,?,?,?,?,1,?,NULL) ON CONFLICT(owner_id,node_id) DO UPDATE SET salt=excluded.salt,secret_digest=excluded.secret_digest,fast_digest=excluded.fast_digest,enabled=1,issued_at=excluded.issued_at,last_used_at=NULL", (owner_id, node_id, salt, digest, fast, now))
            return {"owner_id": owner_id, "node_id": node_id, "credential": f"{node_id}:{secret}", "issued_at": now}
        except PlatformRepositoryError:
            raise
        except (sqlite3.Error, OSError):
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def authenticate_node_credential(self, node_id: str, secret: str, *,
                                     remote_addr: str | None = None) -> dict[str, Any] | None:
        """Verify ``node_id:secret``.

        * digest: sha256 ``fast_digest`` + ``hmac.compare_digest``; a legacy
          row (PBKDF2 only) is verified once and then gets its fast digest
          (the PBKDF2 digest stays for rollback compatibility);
        * ``last_used_at`` / ``last_seen_at`` are written at most once per
          ``NODE_LAST_USED_WRITE_INTERVAL_S`` (or when the node was not
          online), not on every request;
        * failures are rate limited in memory per node id (and per
          ``remote_addr`` when the caller supplies it).
        """
        node_id = validate_id(node_id, "node_id")
        if not isinstance(secret, str) or not secret or len(secret) > 256:
            return None
        keys = self._auth_keys(node_id, remote_addr)
        if self._auth_rate_limited(keys, time.monotonic()):
            return None
        conn = None
        try:
            conn = self._connect()
            rows = conn.execute("SELECT c.owner_id,c.node_id,c.salt,c.secret_digest,c.fast_digest,c.last_used_at,n.status AS node_status,n.enabled AS node_enabled,c.enabled AS credential_enabled FROM node_credentials c JOIN nodes n ON n.owner_id=c.owner_id AND n.node_id=c.node_id WHERE c.node_id=?", (node_id,)).fetchall()
            match = None
            upgrade = None
            for row in rows:
                if not bool(row["node_enabled"]) or not bool(row["credential_enabled"]):
                    continue
                salt = bytes(row["salt"])
                fast = self._fast_node_digest(secret, salt)
                if row["fast_digest"] is not None:
                    ok = hmac.compare_digest(fast, bytes(row["fast_digest"]))
                else:
                    ok = hmac.compare_digest(
                        self._derive_node_digest(secret, salt), bytes(row["secret_digest"]))
                    if ok:
                        upgrade = fast
                if ok:
                    if match is not None:
                        return None
                    match = row
            if match is None:
                self._record_auth_failure(keys, time.monotonic())
                return None
            now = time.time()
            owner_id = match["owner_id"]
            if upgrade is not None:
                conn.execute("UPDATE node_credentials SET fast_digest=? WHERE owner_id=? AND node_id=? AND fast_digest IS NULL AND secret_digest=?", (upgrade, owner_id, node_id, match["secret_digest"]))
            last_used = match["last_used_at"]
            if (last_used is None or match["node_status"] != "online"
                    or now - float(last_used) >= NODE_LAST_USED_WRITE_INTERVAL_S
                    or now < float(last_used)):
                conn.execute("UPDATE node_credentials SET last_used_at=? WHERE owner_id=? AND node_id=?", (now, owner_id, node_id))
                conn.execute("UPDATE nodes SET last_seen_at=?,status='online' WHERE owner_id=? AND node_id=?", (now, owner_id, node_id))
            return {"owner_id": owner_id, "node_id": node_id, "last_seen_at": now}
        except (PlatformValidationError, sqlite3.Error):
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def record_node_heartbeat(self, owner_id: str, node_id: str, *, status: str = "online") -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        node_id = validate_id(node_id, "node_id")
        if status not in {"online", "degraded"}:
            raise PlatformRepositoryError("invalid_node_status")
        conn = None
        try:
            conn = self._connect()
            now = time.time()
            cur = conn.execute("UPDATE nodes SET last_seen_at=?,status=? WHERE owner_id=? AND node_id=? AND enabled=1", (now, status, owner_id, node_id))
            if cur.rowcount != 1:
                raise PlatformRepositoryError("node_not_found")
            row = conn.execute("SELECT * FROM nodes WHERE owner_id=? AND node_id=?", (owner_id, node_id)).fetchone()
            return self._row_node(row)
        except PlatformRepositoryError:
            raise
        except (sqlite3.Error, OSError):
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()
