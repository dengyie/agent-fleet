"""fleet_core/client_release.py — 节点客户端发布格式（hub 打包签名、节点校验共用）。

* **制品**：只含客户端闭包 :data:`CLIENT_PATHS`（不含 hub / tests / 前端 /
  运维配置），可复现 tar.gz（排序、固定 mtime/属主/权限、gzip 无时间戳），
  根目录带 :data:`RELEASE_INFO`（版本、序号、逐文件 sha256）。
* **清单**：``{schemaVersion, kind, channel, sequence, version, publishedAt,
  issuedAt, expiresAt, artifact{size, sha256}, keyId}``，Ed25519 签名覆盖
  ``MANIFEST_DOMAIN + canonical_json(manifest)``。域前缀保证清单签名与
  supervisor 命令信封（裸 canonical JSON）互不可替换，即使共用同一把 key。
* **防回滚 / 防重放**：节点只接受 ``sequence`` 严格大于已观察值的新版本，
  且 ``issuedAt <= now < expiresAt``。
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import tarfile
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from fleet_core.control import canonical_json_bytes, sign_payload, verify_payload

#: 客户端代码闭包（tools/ 与 fleet_core/ 的全部 import 依赖）。
CLIENT_PATHS = (
    "tools", "fleet_core", "connectors",
    "agent_profiles.py", "report_schema.py", "session_schema.py",
    "platform_schema.py", "requirements.txt",
)
RELEASE_INFO = "CLIENT_RELEASE.json"
MANIFEST_DOMAIN = b"agent-fleet/client-manifest/v1\n"
MANIFEST_KIND = "agent-fleet-client-release"
SCHEMA_VERSION = 1
DEFAULT_CHANNEL = "stable"
#: 制品与解包上限：远大于当前闭包（约 2MB 压缩），只防异常输入。
MAX_ARTIFACT_BYTES = 64 << 20
MAX_UNPACKED_BYTES = 256 << 20
MAX_FILES = 20000

_FIXED_MTIME = 1_700_000_000
_SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
_SKIP_SUFFIXES = (".pyc", ".pyo")
_SKIP_NAMES = {".DS_Store"}
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
CHANNEL_RE = re.compile(r"^[a-z][a-z0-9-]{0,15}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ReleaseFormatError(ValueError):
    """清单/制品不符合格式；``code`` 是稳定的诊断码。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def key_id(public_key: bytes) -> str:
    return sha256_bytes(bytes(public_key))[:16]


def public_key_of(private_key: bytes) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    return Ed25519PrivateKey.from_private_bytes(private_key).public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw)


def iter_client_files(root) -> list[str]:
    """``root`` 下属于客户端闭包的常规文件（posix 相对路径，已排序）。"""
    root = Path(root)
    out: list[str] = []
    for entry in CLIENT_PATHS:
        path = root / entry
        if path.is_file() and not path.is_symlink():
            out.append(entry)
            continue
        if not path.is_dir() or path.is_symlink():
            continue
        for item in path.rglob("*"):
            rel = item.relative_to(root)
            if any(part in _SKIP_DIRS for part in rel.parts):
                continue
            if (item.is_symlink() or not item.is_file()
                    or item.name in _SKIP_NAMES or item.name.endswith(_SKIP_SUFFIXES)):
                continue
            out.append(rel.as_posix())
    return sorted(out)


def tree_files(root) -> dict[str, str]:
    root = Path(root)
    return {rel: sha256_bytes((root / rel).read_bytes()) for rel in iter_client_files(root)}


def tree_digest(files: Mapping[str, str]) -> str:
    return sha256_bytes(canonical_json_bytes(dict(files)))


def release_info(version: str, sequence: int, channel: str,
                 files: Mapping[str, str]) -> dict[str, Any]:
    return {"schemaVersion": SCHEMA_VERSION, "version": version,
            "sequence": int(sequence), "channel": channel, "files": dict(files)}


def build_archive(root, info: Mapping[str, Any]) -> bytes:
    """可复现 tar.gz：同一份树 + 同一 info 永远得到同样的字节。"""
    root = Path(root)
    members: list[tuple[str, bytes, int]] = []
    for rel in sorted(info["files"]):
        path = root / rel
        data = path.read_bytes()
        if sha256_bytes(data) != info["files"][rel]:
            raise ReleaseFormatError("tree_changed")
        mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
        members.append((rel, data, mode))
    members.append((RELEASE_INFO, canonical_json_bytes(dict(info)), 0o644))
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for rel, data, mode in sorted(members):
            ti = tarfile.TarInfo(rel)
            ti.size, ti.mode, ti.mtime = len(data), mode, _FIXED_MTIME
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            tar.addfile(ti, io.BytesIO(data))
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0, filename="") as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


def _safe_member_name(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (not name or name.startswith("/") or "\\" in name or path.is_absolute()
            or any(part in ("", ".", "..") for part in path.parts)):
        raise ReleaseFormatError("unsafe_member")
    return path


def extract_archive(data: bytes, target) -> dict[str, Any]:
    """安全解包到空目录 ``target``，并按包内 ``RELEASE_INFO`` 逐文件校验。

    只接受常规文件；拒绝绝对路径、``..``、链接、设备文件、重复项与超限。
    返回解析后的 release info。
    """
    target = Path(target)
    target.mkdir(parents=True, exist_ok=False)
    seen: dict[str, str] = {}
    total = 0
    info_raw = None
    try:
        tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ReleaseFormatError("artifact_corrupt") from exc
    with tar:
        for count, member in enumerate(tar, start=1):
            if count > MAX_FILES:
                raise ReleaseFormatError("too_many_files")
            if not member.isreg():
                raise ReleaseFormatError("unsafe_member")
            rel = _safe_member_name(member.name)
            key = rel.as_posix()
            if key in seen:
                raise ReleaseFormatError("duplicate_member")
            total += member.size
            if total > MAX_UNPACKED_BYTES:
                raise ReleaseFormatError("artifact_too_large")
            fh = tar.extractfile(member)
            payload = fh.read() if fh is not None else b""
            if len(payload) != member.size:
                raise ReleaseFormatError("artifact_corrupt")
            if key == RELEASE_INFO:
                info_raw = payload
            dest = target.joinpath(*rel.parts)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(payload)
            dest.chmod(0o755 if member.mode & 0o111 else 0o644)
            seen[key] = sha256_bytes(payload)
    if info_raw is None:
        raise ReleaseFormatError("release_info_missing")
    info = parse_release_info(info_raw)
    files = dict(seen)
    files.pop(RELEASE_INFO)
    if files != info["files"]:
        raise ReleaseFormatError("release_files_mismatch")
    return info


def parse_release_info(raw: bytes) -> dict[str, Any]:
    try:
        info = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ReleaseFormatError("release_info_invalid") from exc
    if (not isinstance(info, dict) or info.get("schemaVersion") != SCHEMA_VERSION
            or not isinstance(info.get("version"), str)
            or not VERSION_RE.fullmatch(info["version"])
            or not _is_int(info.get("sequence"))
            or not isinstance(info.get("channel"), str)
            or not isinstance(info.get("files"), dict)
            or not all(isinstance(k, str) and isinstance(v, str) and _SHA256_RE.fullmatch(v)
                       for k, v in info["files"].items())):
        raise ReleaseFormatError("release_info_invalid")
    return info


def read_release_info(root) -> dict[str, Any] | None:
    try:
        return parse_release_info((Path(root) / RELEASE_INFO).read_bytes())
    except (OSError, ReleaseFormatError):
        return None


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------

def manifest_bytes(manifest: Mapping[str, Any]) -> bytes:
    return MANIFEST_DOMAIN + canonical_json_bytes(dict(manifest))


def sign_manifest(private_key: bytes, manifest: Mapping[str, Any]) -> str:
    return sign_payload(private_key, manifest_bytes(manifest))


def verify_manifest(public_keys: Iterable[bytes], manifest: Any, signature: Any) -> bool:
    """任一受信公钥（且 keyId 匹配）验签通过即 True；从不抛异常。"""
    if not isinstance(manifest, Mapping) or not isinstance(signature, str):
        return False
    try:
        payload = manifest_bytes(manifest)
    except (TypeError, ValueError):
        return False
    for public_key in public_keys:
        if manifest.get("keyId") == key_id(public_key) and verify_payload(
                public_key, payload, signature):
            return True
    return False


def validate_manifest(manifest: Any, *, now: float, channel: str) -> dict[str, Any]:
    """结构 + 时效校验（签名另验）。返回清单本身；不合格抛 ReleaseFormatError。"""
    if not isinstance(manifest, Mapping):
        raise ReleaseFormatError("manifest_invalid")
    artifact = manifest.get("artifact")
    if (manifest.get("schemaVersion") != SCHEMA_VERSION
            or manifest.get("kind") != MANIFEST_KIND
            or not _is_int(manifest.get("sequence")) or manifest["sequence"] < 1
            or not isinstance(manifest.get("version"), str)
            or not VERSION_RE.fullmatch(manifest["version"])
            or not isinstance(artifact, Mapping)
            or not _is_int(artifact.get("size"))
            or not 0 < artifact["size"] <= MAX_ARTIFACT_BYTES
            or not isinstance(artifact.get("sha256"), str)
            or not _SHA256_RE.fullmatch(artifact["sha256"])
            or not all(_is_int(manifest.get(k)) for k in ("publishedAt", "issuedAt", "expiresAt"))):
        raise ReleaseFormatError("manifest_invalid")
    if manifest.get("channel") != channel:
        raise ReleaseFormatError("channel_mismatch")
    # 允许 5 分钟时钟偏差；过期清单一律拒绝（防止冻结在旧版本的重放）。
    if manifest["issuedAt"] > now + 300 or now >= manifest["expiresAt"]:
        raise ReleaseFormatError("manifest_expired")
    return dict(manifest)
