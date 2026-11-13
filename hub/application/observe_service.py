"""Application-layer observation use cases.

``ObserveService`` owns the push-observation business logic that previously
lived in the HTTP blueprint and the ``scan`` facade:

- machine validation and payload sanitization
- old/current diff detection
- snapshot persistence and the ``state_changed`` event
- public status / machine-detail / event-summary read models
- stale ingest reconciliation (mark expired push reports offline)

Constructor-injected repository, event publisher, and host configuration keep
this module free of Flask, request state, module-level path lookups, and any
network/subprocess behavior.  The hub only processes pushed observations.
"""

import re
import threading
import time
from pathlib import Path

from hub.application.event_emit import safe_emit
from hub.domain.machine import public_machine_detail, public_machine_summary
from hub.repositories import MACHINE_NAME_RE
from report_schema import (
    sanitize_agents,
    sanitize_client,
    sanitize_instances,
    sanitize_local_profiles,
    sanitize_system,
)

DEFAULT_STALE_AFTER_S = 300
_HISTORY_LIMIT = 720
_REPORT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

#: 快照里记录上报认证方式的字段（公共摘要/详情白名单不含它，不会外泄）。
INGEST_AUTH_FIELD = "ingest_auth"
INGEST_AUTH_MACHINE = "machine_credential"
INGEST_AUTH_SHARED = "shared_token"

_DIFF_AGENT_FIELDS = (
    "installed", "gateway_state", "deployed", "error",
    "active_agents", "platforms", "pid", "process_count",
    "active_count", "session_count",
)


def normalize_agents(agents):
    """Reduce an agent payload to the fields compared during change detection."""
    out = {}
    for agent_type, state in (agents or {}).items():
        if not isinstance(state, dict):
            out[agent_type] = state
            continue
        normalized = {}
        for key in _DIFF_AGENT_FIELDS:
            if key in state:
                normalized[key] = state[key]
        if isinstance(state.get("sessions"), list):
            normalized["session_count"] = len(state["sessions"])
        out[agent_type] = normalized
    return out


def diff_snapshots(old, new):
    """Return the field-level change list between two push snapshots.

    Mirrors the legacy ``scan._diff`` contract so the service and its callers
    keep identical change semantics.
    """
    if not old:
        return ["initial"]
    changes = []
    if normalize_agents(new.get("agents")) != normalize_agents(old.get("agents")):
        changes.append("agents")
    for key in ("reachable", "remote_error"):
        if new.get(key) != old.get(key):
            changes.append(key)
    return changes


class ObserveError(Exception):
    """Bounded application error with a public, bounded ``code``.

    Codes match the canonical application error sets:
    ``invalid_json`` / ``invalid_machine`` / ``machine_offline`` /
    ``not_found``. The HTTP adapter maps ``not_found`` to 404 and all other
    validation/application errors to 400; the message text is never exposed.
    """

    _STATUS_OVERRIDES = {"not_found": 404, "machine_mismatch": 403,
                         "machine_bound": 403}

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = str(detail)[:200] or ""
        self.status = self._STATUS_OVERRIDES.get(code, 400)
        super().__init__(self.code)


class HostConfig:
    """Dedicated host display metadata and stale TTLs.

    Replaces ad-hoc ``hosts.yaml`` parsing inside web/scan callers. This is a
    plain config object: it only converts file rows into the values
    :class:`ObserveService` needs (names, display rows, stale TTLs).
    """

    def __init__(self, hosts, *, default_stale_after_s=DEFAULT_STALE_AFTER_S) -> None:
        self._default_stale_after_s = int(default_stale_after_s)
        self._hosts = []
        for host in hosts or []:
            if not isinstance(host, dict) or "name" not in host:
                continue
            self._hosts.append({
                "name": str(host["name"]),
                "desc": str(host.get("desc", ""))[:200],
                "transport": str(host.get("transport", "ingest")),
                "stale_after_s": int(host.get("stale_after_s", self._default_stale_after_s)),
            })

    @classmethod
    def load(cls, path=None, *, default_stale_after_s=DEFAULT_STALE_AFTER_S):
        """Load host metadata from a YAML hosts file (missing file -> empty)."""
        try:
            import yaml
            if path is None or not Path(path).exists():
                data = {}
            else:
                data = yaml.safe_load(Path(path).read_text()) or {}
        except (OSError, ImportError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        return cls(data.get("hosts") or [], default_stale_after_s=default_stale_after_s)

    def names(self) -> list[str]:
        return [h["name"] for h in self._hosts]

    def hosts(self) -> list[dict]:
        """Public display rows (name/desc/transport) for status summaries."""
        return [{"name": h["name"], "desc": h["desc"], "transport": h["transport"]}
                for h in self._hosts]

    def stale_after_s(self, name: str) -> int:
        for h in self._hosts:
            if h["name"] == name:
                return h["stale_after_s"]
        return self._default_stale_after_s


class ObserveService:
    """Application service for push observation use cases."""

    def __init__(self, observation_repo, event_publisher, host_config, *, clock=time.time) -> None:
        self.repo = observation_repo
        self.publisher = event_publisher
        self.hosts = host_config
        self._clock = clock
        # 每台机器一把锁：ingest 与 reconcile 的 read→check→save 在同一临界区，
        # 避免 reconcile 用旧快照覆盖刚到达的 ingest（把在线机器标成离线）。
        # 进程内锁之外，临界区还进入仓库的 ``machine_transaction``（跨进程
        # 文件锁），``hub/scan.py`` 等独立进程同样无法插入 read 与 save 之间。
        self._locks_guard = threading.Lock()
        self._machine_locks: dict[str, threading.Lock] = {}

    def _machine_lock(self, machine: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._machine_locks.get(machine)
            if lock is None:
                lock = self._machine_locks[machine] = threading.Lock()
            return lock

    def is_credential_bound(self, machine) -> bool:
        """该机器最近一次 ingest 是否由每机凭据认证（共享 token 不得覆盖）。"""
        if not isinstance(machine, str) or not MACHINE_NAME_RE.fullmatch(machine):
            return False
        current = self.repo.read_current(machine)
        return bool(current) and current.get(INGEST_AUTH_FIELD) == INGEST_AUTH_MACHINE

    # -- write use case ----------------------------------------------------

    def ingest(self, payload: dict, *, bound_machine: str | None = None) -> dict:
        """Validate, sanitize, diff, persist, and publish one push report.

        Returns the legacy ``{"ok": True, "changes": [...]}`` result; raises
        :class:`ObserveError` for invalid machine names or malformed payloads.

        机器绑定规则：
        - ``bound_machine`` 非空（每机凭据认证）：payload ``machine`` 必须相同，
          否则 ``machine_mismatch``（403）；快照记 ``ingest_auth=machine_credential``；
        - ``bound_machine`` 为空（共享 token，兼容现有探针）：快照记
          ``ingest_auth=shared_token``；若该机器当前快照已由每机凭据写入则拒绝
          ``machine_bound``（403），共享 token 不能冒充/覆盖已绑定机器。
        """
        if not isinstance(payload, dict):
            raise ObserveError("invalid_json")
        machine = payload.get("machine")
        if not machine:
            raise ObserveError("invalid_machine", "machine 字段缺失")
        if not isinstance(machine, str) or not MACHINE_NAME_RE.fullmatch(machine):
            raise ObserveError("invalid_machine", "machine 名不合法")
        if bound_machine is not None and machine != bound_machine:
            raise ObserveError("machine_mismatch", "payload machine 与凭据机器不一致")
        report_id = payload.get("report_id")
        if "report_id" in payload and (
                not isinstance(report_id, str) or not _REPORT_ID_RE.fullmatch(report_id)):
            raise ObserveError("invalid_report_id", "report_id 不合法")

        remote_error = payload.get("error")
        if isinstance(remote_error, str):
            remote_error = remote_error[:200]
        elif remote_error is not None:
            remote_error = str(remote_error)[:200]
        snapshot = {
            "machine": machine,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._clock())),
            "source": "ingest",
            "agents": sanitize_agents(payload.get("agent") or payload.get("agents") or {}),
            "system": sanitize_system(payload.get("system", {})),
            "instances": sanitize_instances(payload.get("instances")),
            "local_profiles": sanitize_local_profiles(payload.get("local_profiles")),
            "client": sanitize_client(payload.get("client")),
            "reachable": True,
            "remote_error": remote_error,
            INGEST_AUTH_FIELD: (INGEST_AUTH_MACHINE if bound_machine is not None
                                else INGEST_AUTH_SHARED),
        }
        with self._machine_lock(machine), self.repo.machine_transaction(machine):
            old = self.repo.read_current(machine)
            if (bound_machine is None and old
                    and old.get(INGEST_AUTH_FIELD) == INGEST_AUTH_MACHINE):
                raise ObserveError("machine_bound", "该机器已绑定每机凭据")
            changes = diff_snapshots(old, snapshot)
            if report_id is None:
                self.repo.save_snapshot(machine, snapshot)
            else:
                saved = self.repo.save_snapshot_once(machine, snapshot, report_id)
                if not saved:
                    return {"ok": True, "changes": [], "duplicate": True}
        if changes:
            safe_emit(self.publisher, "state_changed", machine=machine,
                      changes=changes, snapshot=snapshot)
        return {"ok": True, "changes": changes}

    # -- read use cases ----------------------------------------------------

    def status(self) -> dict:
        """Public machine summary rows plus server-side ``updated_at`` stamp."""
        return {
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._clock())),
            "machines": self._build_summary(),
            "features": {
                "append_user_turn": False,
                "apply_local_profile": False,
            },
        }

    def machine_detail(self, name: str) -> dict:
        """Public single-machine detail with the bounded history timeline."""
        if not isinstance(name, str) or not MACHINE_NAME_RE.fullmatch(name):
            raise ObserveError("invalid_machine", "machine 名不合法")
        current = self.repo.read_current(name)
        if not current:
            raise ObserveError("not_found")
        return {"ok": True, **public_machine_detail(
            current, self.repo.read_history(name, limit=_HISTORY_LIMIT)
        )}

    def events(self, limit: int = 50) -> list[dict]:
        """Recent public event summaries without snapshot bodies."""
        items = []
        for e in self.publisher.read_recent(limit):
            items.append({
                "event": e.get("event"),
                "machine": e.get("machine"),
                "ts": e.get("ts"),
                "event_seq": e.get("event_seq"),
                "changes": e.get("changes", []),
            })
        return items

    # -- stale reconciliation ----------------------------------------------

    def reconcile(self, machine=None, *, now=None) -> list[dict]:
        """Mark expired push snapshots offline without executing any process.

        ``machine`` restricts reconciliation to one machine (None = all).
        """
        if now is None:
            now = self._clock()
        results = []
        machines = [machine] if machine else sorted(self.repo.machines())
        for name in machines:
            ttl = self.hosts.stale_after_s(name)
            # read→判定→写 与 ingest 共用每机锁与仓库跨进程事务：判定所用的
            # ``_ts`` 就是被覆盖的那份快照，期间到达的 ingest（含其他进程）
            # 要么先落盘（此处读到新 _ts 跳过），要么等本次写完再覆盖成 reachable。
            with self._machine_lock(name), self.repo.machine_transaction(name):
                old = self.repo.read_current(name)
                if not old or old.get("source") != "ingest":
                    continue
                if not old.get("reachable", True):
                    continue
                last_seen = float(old.get("_ts", 0))
                if now - last_seen <= ttl:
                    continue
                snapshot = dict(old)
                snapshot.update({
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                    "reachable": False,
                    "remote_error": f"ingest 超过 {ttl}s 未上报",
                    "source": "ingest",
                })
                self.repo.save_snapshot(name, snapshot)
            changes = diff_snapshots(old, snapshot)
            if changes:
                safe_emit(self.publisher, "state_changed", machine=name,
                          changes=changes, snapshot=snapshot)
            results.append({"machine": name, "changed": changes, "stale": True})
        return results

    # -- internal helpers --------------------------------------------------

    def _build_summary(self) -> list[dict]:
        known = set(self.hosts.names())
        hosts = self.hosts.hosts()
        try:
            for name in sorted(self.repo.machines()):
                if name in known:
                    continue
                current = self.repo.read_current(name)
                if current and current.get("source") == "ingest" and current.get("machine"):
                    hosts.append({"name": name, "desc": "自报告 (ingest)", "transport": "ingest"})
        except Exception:
            pass
        rows = []
        for host in hosts:
            rows.append(public_machine_summary(self.repo.read_current(host["name"]), host))
        return rows
