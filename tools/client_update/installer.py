"""tools/client_update/installer.py — 事务化的客户端更新器。

布局（``client_home``）::

    releases/<version>/        解包后的不可变代码树（含 CLIENT_RELEASE.json）
    current -> releases/<v>    服务入口经此链接启动；切换 = 原子 rename
    previous -> releases/<v>   上一个版本，回滚目标
    staging/<txid>/            解包中的临时目录
    transactions/<txid>.json   事务日志（phase 推进前先落盘，崩溃后 recover 重放）
    state/manifest-observation.json   已接受的最大 sequence（防回滚）与失败记录
    state/runtime/<role>.json  进程启动回执（runtime.announce）
    state/drain/<role>.json    让旧版本进程自愿退出的请求
    state/updater.lock         同一时刻只有一个 updater

状态机：``fetched → staged → switched → draining → committed``；
失败分支：切换前 ``aborted``，切换后 ``rolled_back``。
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fleet_core import client_release as cr
from fleet_core.identity import ProcessIdentity
from fleet_core.locking import OwnedLock
from fleet_core.process import local_process
from tools.client_update import runtime
from tools.client_update import services as svc

KEEP_RELEASES = 3
DEFAULT_GRACE_S = 90.0
_POLL_S = 1.0
_SMOKE_IMPORTS = ("fleet_core.client_release", "fleet_core.locking", "tools.runner_config",
                  "tools.transport", "tools.client_update.installer")
TERMINAL = ("committed", "aborted", "rolled_back")


class UpdateError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass
class Layout:
    home: Path

    def __post_init__(self):
        self.home = Path(self.home)

    releases = property(lambda self: self.home / "releases")
    current = property(lambda self: self.home / "current")
    previous = property(lambda self: self.home / "previous")
    staging = property(lambda self: self.home / "staging")
    transactions = property(lambda self: self.home / "transactions")
    state = property(lambda self: self.home / "state")
    observation = property(lambda self: self.home / "state" / "manifest-observation.json")
    lock = property(lambda self: self.home / "state" / "updater.lock")

    def ensure(self) -> None:
        for path in (self.releases, self.staging, self.transactions, self.state):
            path.mkdir(parents=True, exist_ok=True)

    def link_version(self, link: Path) -> str | None:
        try:
            target = os.readlink(link)
        except OSError:
            return None
        return Path(target).name

    def set_link(self, link: Path, version: str | None) -> None:
        if version is None:
            try:
                link.unlink()
            except FileNotFoundError:
                pass
            return
        tmp = link.with_name(f".{link.name}.{secrets.token_hex(4)}")
        os.symlink(os.path.join("releases", version), tmp)
        os.replace(tmp, link)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(value, fh, sort_keys=True, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


@dataclass
class Transaction:
    path: Path
    data: dict[str, Any]

    @property
    def phase(self) -> str:
        return self.data["phase"]

    def advance(self, phase: str, **extra) -> None:
        self.data.update(extra)
        self.data["phase"] = phase
        self.data.setdefault("history", []).append([phase, int(time.time())])
        _write_json(self.path, self.data)


@dataclass
class Installer:
    layout: Layout
    public_keys: list[bytes]
    fetcher: Any = None
    services: list[svc.Service] = field(default_factory=list)
    run: Callable = svc._default_run
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    grace_s: float = DEFAULT_GRACE_S
    channel: str = cr.DEFAULT_CHANNEL
    python: str = sys.executable
    log: Callable[[str], None] = lambda msg: None

    # -- observation ----------------------------------------------------

    def observation(self) -> dict[str, Any]:
        return runtime.read_json(self.layout.observation) or {"sequence": 0}

    def _observe(self, **fields) -> None:
        value = self.observation()
        value.update(fields)
        _write_json(self.layout.observation, value)

    # -- status ---------------------------------------------------------

    def status(self) -> dict[str, Any]:
        lay = self.layout
        current = lay.link_version(lay.current)
        info = cr.read_release_info(lay.current) if current else None
        receipts = {}
        for role in runtime.ROLES:
            receipt = runtime.read_json(runtime.receipt_path(role, lay.home))
            if receipt:
                ident = ProcessIdentity.from_dict(receipt)
                receipt["alive"] = ident.alive() if ident else None
                receipts[role] = receipt
        pending = [t.data for t in self._pending()]
        return {
            "home": str(lay.home),
            "current": current,
            "sequence": info["sequence"] if info else None,
            "previous": lay.link_version(lay.previous),
            "releases": sorted(p.name for p in lay.releases.iterdir()) if lay.releases.is_dir() else [],
            "observation": self.observation(),
            "services": [dict(s.to_dict(), managed=s.uses(lay.current)) for s in self.services],
            "runtime": receipts,
            "pendingTransactions": pending,
        }

    # -- manifest -------------------------------------------------------

    def check(self) -> dict[str, Any]:
        """拉取并校验清单；返回 ``{manifest, action}``，action ∈ install/up_to_date/skip_failed。"""
        if not self.public_keys:
            raise UpdateError("no_trusted_key", "runner.yaml 未配置 supervisor public_key")
        signed = self.fetcher.manifest(self.channel)
        manifest, signature = signed.get("manifest"), signed.get("signature")
        if not cr.verify_manifest(self.public_keys, manifest, signature):
            raise UpdateError("bad_signature")
        try:
            manifest = cr.validate_manifest(manifest, now=self.clock(), channel=self.channel)
        except cr.ReleaseFormatError as exc:
            raise UpdateError(exc.code) from None
        observed = self.observation()
        if manifest["sequence"] < int(observed.get("sequence") or 0):
            raise UpdateError("sequence_rollback",
                              f"{manifest['sequence']} < {observed.get('sequence')}")
        if self.layout.link_version(self.layout.current) == manifest["version"]:
            action = "up_to_date"
        elif observed.get("failedSequence") == manifest["sequence"]:
            action = "skip_failed"
        else:
            action = "install"
        return {"manifest": manifest, "action": action}

    # -- update ---------------------------------------------------------

    def update(self, *, force: bool = False, dry_run: bool = False) -> dict[str, Any]:
        self.layout.ensure()
        with OwnedLock(self.layout.lock, "client-updater"):
            recovered = self._recover_locked()
            checked = self.check()
            manifest, action = checked["manifest"], checked["action"]
            if action == "skip_failed" and force:
                action = "install"
            if action != "install" or dry_run:
                return {"action": action, "dryRun": dry_run, "recovered": recovered,
                        "version": manifest["version"], "sequence": manifest["sequence"]}
            result = self._install(manifest)
            result["recovered"] = recovered
            return result

    def _new_tx(self, manifest: dict[str, Any]) -> Transaction:
        txid = time.strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)
        tx = Transaction(self.layout.transactions / f"{txid}.json", {
            "txid": txid, "phase": "new",
            "fromVersion": self.layout.link_version(self.layout.current),
            "previousVersion": self.layout.link_version(self.layout.previous),
            "toVersion": manifest["version"], "sequence": manifest["sequence"],
            "artifact": manifest["artifact"], "startedAt": int(self.clock()),
            "updater": ProcessIdentity.current().to_dict(),
        })
        return tx

    def _install(self, manifest: dict[str, Any]) -> dict[str, Any]:
        lay = self.layout
        tx = self._new_tx(manifest)
        version = manifest["version"]
        data = self.fetcher.artifact(manifest["artifact"]["sha256"], manifest["artifact"]["size"])
        if (len(data) != manifest["artifact"]["size"]
                or cr.sha256_bytes(data) != manifest["artifact"]["sha256"]):
            raise UpdateError("artifact_mismatch")
        tx.advance("fetched")
        staged = lay.staging / tx.data["txid"]
        try:
            try:
                info = cr.extract_archive(data, staged)
            except cr.ReleaseFormatError as exc:
                raise UpdateError(exc.code) from None
            if (info["version"], info["sequence"], info["channel"]) != (
                    version, manifest["sequence"], self.channel):
                raise UpdateError("release_info_mismatch")
            self._smoke(staged)
        except BaseException:
            shutil.rmtree(staged, ignore_errors=True)
            tx.advance("aborted", error="staging_failed")
            self._observe(failedSequence=manifest["sequence"])
            raise
        dest = lay.releases / version
        if dest.exists():
            # 同版本目录已存在（上次中断/回滚留下）：用新校验过的树替换
            if lay.link_version(lay.current) == version:
                raise UpdateError("current_in_use")
            shutil.rmtree(dest)
        os.replace(staged, dest)
        tx.advance("staged")
        return self._switch_and_verify(tx, version)

    def _smoke(self, root: Path) -> None:
        """新树能编译、关键模块能 import（隔离环境，不用当前进程的 sys.path）。"""
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import importlib; "
                f"[importlib.import_module(m) for m in {list(_SMOKE_IMPORTS)!r}]")
        for cmd in ([self.python, "-m", "compileall", "-q", str(root)],
                    [self.python, "-c", code, str(root)]):
            result = local_process.run(cmd, capture_output=True, text=True, timeout=180,
                                    check=False, cwd=str(root))
            if result.returncode != 0:
                tail = (result.stderr or result.stdout or "").strip().splitlines()[-1:]
                raise UpdateError("smoke_failed", tail[0][:200] if tail else "")

    def _managed_services(self) -> list[svc.Service]:
        return [s for s in self.services if s.uses(self.layout.current)]

    def _switch_and_verify(self, tx: Transaction, version: str) -> dict[str, Any]:
        lay = self.layout
        before = {role: runtime.read_json(runtime.receipt_path(role, lay.home))
                  for role in runtime.ROLES}
        if tx.data["fromVersion"]:
            lay.set_link(lay.previous, tx.data["fromVersion"])
        lay.set_link(lay.current, version)
        tx.advance("switched")
        self._observe(sequence=tx.data["sequence"], version=version,
                      acceptedAt=int(self.clock()), failedSequence=None)
        managed = self._managed_services()
        outcome = self._drain(tx, version, managed, before)
        if outcome["failed"]:
            self.log(f"新版本未能接管 {outcome['failed']}，回滚")
            self._rollback_tx(tx, reason="verify_failed:" + ",".join(outcome["failed"]))
            raise UpdateError("verify_failed", ",".join(outcome["failed"]))
        tx.advance("committed", verified=outcome["verified"], draining=outcome["draining"],
                   unmanaged=[s.role for s in self.services if s not in managed])
        self._gc()
        return {"action": "installed", "version": version, "sequence": tx.data["sequence"],
                "txid": tx.data["txid"], **outcome,
                "unmanaged": tx.data["unmanaged"]}

    def _drain(self, tx: Transaction, version: str, managed: list[svc.Service],
               before: dict[str, Any]) -> dict[str, list[str]]:
        lay = self.layout
        for service in managed:
            _write_json(runtime.drain_path(service.role, lay.home),
                        {"txid": tx.data["txid"], "targetVersion": version,
                         "requestedAt": int(self.clock())})
        tx.advance("draining", roles=[s.role for s in managed])
        for service in managed:
            svc.kickstart(service, self.run)
        pending = {s.role: s for s in managed}
        verified: list[str] = []
        deadline = self.clock() + self.grace_s
        kicked_at = self.clock()
        while pending:
            for role in list(pending):
                receipt = runtime.read_json(runtime.receipt_path(role, lay.home)) or {}
                ident = ProcessIdentity.from_dict(receipt)
                if receipt.get("version") == version and ident and ident.alive() is not False:
                    verified.append(role)
                    del pending[role]
            if not pending or self.clock() >= deadline:
                break
            # 常驻进程在两轮之间退出；KeepAlive 节流后 kickstart 一次确保被拉起
            if self.clock() - kicked_at >= 15:
                for service in pending.values():
                    svc.kickstart(service, self.run)
                kicked_at = self.clock()
            self.sleep(_POLL_S)
        failed, draining = [], []
        for role in pending:
            old = ProcessIdentity.from_dict(before.get(role) or {})
            if old is not None and old.alive():
                draining.append(role)  # 旧进程还在跑长任务，drain 文件保留，稍后自行退出
            else:
                failed.append(role)
        return {"verified": verified, "draining": draining, "failed": failed}

    # -- rollback / recover ------------------------------------------------

    def _rollback_tx(self, tx: Transaction, *, reason: str) -> None:
        lay = self.layout
        target = tx.data.get("fromVersion")
        if target and (lay.releases / target).is_dir():
            lay.set_link(lay.current, target)
            lay.set_link(lay.previous, tx.data.get("previousVersion"))
            for service in self._managed_services():
                _write_json(runtime.drain_path(service.role, lay.home),
                            {"txid": tx.data["txid"], "targetVersion": target,
                             "requestedAt": int(self.clock()), "rollback": True})
                svc.kickstart(service, self.run)
        else:
            lay.set_link(lay.current, None)
        self._observe(failedSequence=tx.data["sequence"])
        tx.advance("rolled_back", reason=reason, restored=target)

    def rollback(self) -> dict[str, Any]:
        """手动回滚到 ``previous``（current/previous 互换）。"""
        lay = self.layout
        with OwnedLock(lay.lock, "client-updater"):
            prev, cur = lay.link_version(lay.previous), lay.link_version(lay.current)
            if not prev or not (lay.releases / prev).is_dir():
                raise UpdateError("no_previous")
            tx = Transaction(lay.transactions / f"rollback-{secrets.token_hex(4)}.json", {
                "txid": f"rollback-{prev}", "phase": "new", "fromVersion": cur,
                "toVersion": prev, "sequence": None, "kind": "manual-rollback"})
            lay.set_link(lay.current, prev)
            lay.set_link(lay.previous, cur)
            tx.advance("switched")
            for service in self._managed_services():
                _write_json(runtime.drain_path(service.role, lay.home),
                            {"txid": tx.data["txid"], "targetVersion": prev,
                             "requestedAt": int(self.clock()), "rollback": True})
                svc.kickstart(service, self.run)
            tx.advance("committed")
            return {"action": "rolled_back", "current": prev, "previous": cur}

    def _pending(self) -> list[Transaction]:
        out = []
        if not self.layout.transactions.is_dir():
            return out
        for path in sorted(self.layout.transactions.glob("*.json")):
            data = runtime.read_json(path)
            if data and data.get("phase") not in TERMINAL:
                out.append(Transaction(path, data))
        return out

    def recover(self) -> list[dict[str, Any]]:
        self.layout.ensure()
        with OwnedLock(self.layout.lock, "client-updater"):
            return self._recover_locked()

    def _recover_locked(self) -> list[dict[str, Any]]:
        """重放中断的事务：切换前 → 清理并 aborted；切换后 → 回滚到 fromVersion。

        切换后中断无法确认新版本是否被验证过，保守回滚；下次 update 会重试
        （failedSequence 记录让自动更新不在同一版本上反复打转，``--force`` 可越过）。
        """
        done = []
        for tx in self._pending():
            phase = tx.phase
            shutil.rmtree(self.layout.staging / tx.data["txid"], ignore_errors=True)
            if phase in ("new", "fetched", "staged"):
                tx.advance("aborted", error=f"interrupted_at_{phase}")
            else:
                self._rollback_tx(tx, reason=f"interrupted_at_{phase}")
            done.append({"txid": tx.data["txid"], "from": phase, "to": tx.phase})
        return done

    # -- gc -------------------------------------------------------------

    def _gc(self) -> list[str]:
        lay = self.layout
        keep = {v for v in (lay.link_version(lay.current), lay.link_version(lay.previous)) if v}
        extras = sorted((p for p in lay.releases.iterdir() if p.is_dir() and p.name not in keep),
                        key=lambda p: p.stat().st_mtime, reverse=True)
        removed = []
        for path in extras[max(0, KEEP_RELEASES - len(keep)):]:
            shutil.rmtree(path, ignore_errors=True)
            removed.append(path.name)
        for path in lay.staging.iterdir() if lay.staging.is_dir() else ():
            shutil.rmtree(path, ignore_errors=True)
        return removed


class HubFetcher:
    """经 runner 凭据从 hub 拉清单与制品。"""

    def __init__(self, cfg, transport=None):
        from tools.transport import Transport

        self.cfg = cfg
        self.transport = transport or Transport(timeout=30, attempts=2)

    def _headers(self) -> dict[str, str]:
        return {"X-Runner-Credential": f"{self.cfg.machine}:{self.cfg.credential}",
                **runtime.client_headers("updater")}

    def manifest(self, channel: str) -> dict[str, Any]:
        status, body = self.transport.get_json(
            f"{self.cfg.hub.rstrip('/')}/api/client/releases/{channel}/manifest",
            self._headers(), timeout=30)
        if status != 200 or not body.get("ok"):
            raise UpdateError("manifest_unavailable", f"{status} {str(body.get('error') or '')[:80]}")
        return body

    def artifact(self, sha256: str, size: int) -> bytes:
        status, body = self.transport.get_bytes(
            f"{self.cfg.hub.rstrip('/')}/api/client/releases/artifacts/{sha256}",
            self._headers(), timeout=120, max_bytes=min(int(size), cr.MAX_ARTIFACT_BYTES))
        if status != 200 or not isinstance(body, bytes):
            raise UpdateError("artifact_unavailable", str(status))
        return body


def trusted_keys(cfg) -> list[bytes]:
    key = getattr(cfg, "supervisor_public_key", None)
    return [key] if key else []
