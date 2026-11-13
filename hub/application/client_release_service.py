"""hub/application/client_release_service.py — hub 自打包、自签名的节点客户端发布。

hub 运行的代码树就是客户端的唯一来源：首次请求时从 ``source_root`` 取客户端闭包
（:data:`fleet_core.client_release.CLIENT_PATHS`）打一个可复现 tar.gz，缓存在内存。

* **序号**：持久化在 ``state_path``。闭包内容（tree digest）不变则沿用序号与版本；
  内容变化才 ``sequence + 1``，避免 hub-only 改动让全舰队无意义重启。
* **版本标签**：源树 ``RELEASE_ORIGIN`` 的 commit 前 12 位，否则 ``dev-<digest12>``；
  首次发布后始终附加 ``-s<sequence>``，避免跳过中间版本或回退源树时重名。
* **签名**：复用 supervisor Ed25519 key（清单签名带独立域前缀，见 fleet_core）。
  无 key = fail-closed，接口返回 503 ``client_release_unsigned``。
"""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fleet_core import client_release as cr
from fleet_core.locking import OwnedLock
from hub.application.errors import ApplicationError

MANIFEST_TTL_S = 24 * 3600
_REBUILD_CHECK_S = 30.0


def _origin_commit(root: Path) -> str | None:
    try:
        for line in (root / "RELEASE_ORIGIN").read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition(": ")
            if key == "commit" and cr.VERSION_RE.fullmatch(value.strip()[:12]):
                return value.strip()[:12]
    except OSError:
        pass
    return None


class ClientReleaseService:
    def __init__(self, *, source_root, state_path, signing_key: bytes | None,
                 channel: str = cr.DEFAULT_CHANNEL,
                 clock: Callable[[], float] = time.time):
        self.source_root = Path(source_root)
        self.state_path = Path(state_path)
        self.channel = channel
        self._key = signing_key
        self._public = cr.public_key_of(signing_key) if signing_key else None
        self._clock = clock
        self._mutex = threading.Lock()
        self._release: dict[str, Any] | None = None
        self._artifact: bytes | None = None
        self._checked_at = 0.0

    @property
    def signed(self) -> bool:
        return self._key is not None

    @property
    def public_key(self) -> bytes | None:
        return self._public

    # -- state -------------------------------------------------------------

    def _read_state(self) -> dict[str, Any]:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write_state(self, state: dict[str, Any]) -> None:
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        tmp.write_text(json.dumps(state, sort_keys=True, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    def _resolve(self, digest: str) -> dict[str, Any]:
        """digest → {sequence, version, publishedAt}；跨进程（多 worker）串行。"""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with OwnedLock(self.state_path.with_name(self.state_path.name + ".lock"),
                       "client-release-state").acquire(timeout=10):
            state = self._read_state()
            channels = state.setdefault("channels", {})
            entry = channels.get(self.channel)
            if isinstance(entry, dict) and entry.get("treeDigest") == digest:
                return entry
            previous = entry if isinstance(entry, dict) else {}
            sequence = int(previous.get("sequence", 0)) + 1
            version = _origin_commit(self.source_root) or f"dev-{digest[:12]}"
            if sequence > 1:
                # Every later publication needs a unique directory, including
                # a third dirty build or a return to an earlier source tree.
                version = f"{version}-s{sequence}"
            entry = {
                "treeDigest": digest,
                "sequence": sequence,
                "version": version,
                "publishedAt": int(self._clock()),
            }
            channels[self.channel] = entry
            self._write_state(state)
            return entry

    # -- build -------------------------------------------------------------

    def current(self) -> dict[str, Any]:
        """当前发布 ``{sequence, version, publishedAt, size, sha256}``（必要时重建）。"""
        with self._mutex:
            now = time.monotonic()
            if self._release is not None and now - self._checked_at < _REBUILD_CHECK_S:
                return self._release
            files = cr.tree_files(self.source_root)
            digest = cr.tree_digest(files)
            if self._release is None or self._release["treeDigest"] != digest:
                entry = self._resolve(digest)
                info = cr.release_info(entry["version"], entry["sequence"],
                                       self.channel, files)
                artifact = cr.build_archive(self.source_root, info)
                self._artifact = artifact
                self._release = {
                    "treeDigest": digest,
                    "sequence": entry["sequence"],
                    "version": entry["version"],
                    "publishedAt": entry["publishedAt"],
                    "size": len(artifact),
                    "sha256": cr.sha256_bytes(artifact),
                }
            self._checked_at = now
            return self._release

    def summary(self) -> dict[str, Any] | None:
        """机器卡片比较「是否落后」用；构建失败返回 None，不影响调用方。"""
        try:
            release = self.current()
        except (OSError, cr.ReleaseFormatError):
            return None
        return {"channel": self.channel, "sequence": release["sequence"],
                "version": release["version"]}

    def signed_manifest(self) -> dict[str, Any]:
        if self._key is None:
            raise ApplicationError("client_release_unsigned", "hub 未配置签名 key", 503)
        release = self.current()
        issued = int(self._clock())
        manifest = {
            "schemaVersion": cr.SCHEMA_VERSION,
            "kind": cr.MANIFEST_KIND,
            "channel": self.channel,
            "sequence": release["sequence"],
            "version": release["version"],
            "publishedAt": release["publishedAt"],
            "issuedAt": issued,
            "expiresAt": issued + MANIFEST_TTL_S,
            "artifact": {"size": release["size"], "sha256": release["sha256"]},
            "keyId": cr.key_id(self._public),
        }
        return {"manifest": manifest, "signature": cr.sign_manifest(self._key, manifest)}

    def artifact(self, sha256: str) -> bytes:
        release = self.current()
        if sha256 != release["sha256"] or self._artifact is None:
            raise ApplicationError("artifact_not_found", "制品不存在或已被新版本替换", 404)
        return self._artifact
