"""tools/supervisor/supervisor.py — 受管 CLI 进程生命周期（Task 8）。

进程组操作被收敛在一个小的 ``GroupOps`` 接口之后。Linux 使用真实
``os.killpg`` + cgroup v2 检测；macOS 也使用 ``os.killpg``（无 cgroup）。

抽象约束（与全局枢纽约束一致）：

- 永不执行任意远程 shell / ``sh -c`` / tmux 命令；
- 永不注入任意 stdin；
- 只有固定枚举的 action（``CONTROL_ACTIONS``）；
- 没有归属证明的既有进程永不被自动 adoption；
- 通过 ``escaped`` 检测逃逸进程并降级 terminate 结果。
- 输出读取是 deadline-safe 的（``select`` + 有界行缓冲），不会在读管道上
  阻塞越过超时。

模块布局：进程组后端与有界输出缓冲在 ``group_ops``；私有句柄与跨实例
句柄绑定在 ``handles``（``HandlesMixin``）；原生会话续接在 ``native_resume``
（``NativeResumeMixin``）。这些名字仍全部从本模块 re-export。

公开输出（``status`` / manifest）永远不包含 pid、cwd、argv、env、command、
path 或 token/secret；``process_group_id`` 是 opaque ``grp_<hex>``，绝不会
是裸 leader PID。
"""
from __future__ import annotations

import os
import platform as _platform_mod
# tests patch ``subprocess.Popen/run`` through this module's attribute.
import subprocess  # noqa: F401
import time
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from report_schema import INSTANCE_FAMILIES
from fleet_core.process.process_group import process_start_identity

from tools.supervisor.model import (
    GROUP_CAPABILITY_CGROUP,
    PAUSED,
    QUARANTINED,
    RUNNING,
    TERMINATED,
    TERMINAL_STATES,
    ManagedSessionManifest,
    new_opaque_id,
    validate_opaque,
)
# Split-out modules.  Every name stays importable from this module (tests,
# hub and tools import / patch ``tools.supervisor.supervisor.<name>``).
from tools.supervisor.group_ops import (  # noqa: F401
    _ABUF_MAX,
    _LINE_MAX,
    _SIGCONT,
    _SIGKILL,
    _SIGSTOP,
    _SIGTERM,
    GroupError,
    GroupOps,
    LinuxGroupOps,
    MacGroupOps,
    POSIXGroupOps,
    WindowsGroupOps,
    _bounded_spawn_env,
    _buffer_chunk,
    _finish_line,
    _handle_pid,
    _pending_lines,
    validate_command,
)
from tools.supervisor.handles import (  # noqa: F401
    HandlesMixin,
    UnknownSessionError,
    _AttachedHandle,
    _Bound,
    _ManagedEntry,
    _OwnerHandle,
    _stamp_private_identity,
)
from tools.supervisor.native_resume import (  # noqa: F401
    _NATIVE_RESUME_FAMILIES,
    _RESUME_FORBIDDEN_FLAGS,
    _RESUME_VERIFIABLE_FAMILIES,
    _SESSION_PATH_RESUME_FAMILIES,
    NativeResumeMixin,
    _live_process_cwd,
    _native_resume_argv,
    _native_resume_identity,
    _resume_token_from_native_path,
)

# --------------------------------------------------------------------------- #
# Fixed control surface (the ONLY actions a Supervisor knows).
# --------------------------------------------------------------------------- #

CONTROL_ACTIONS = frozenset({
    "pause_session",
    "resume_session",
    "terminate_session",
    "quarantine_session",
    "cancel_attempt",
    "adopt",
    "detach",
    "append_user_turn",
    "apply_local_profile",
})

# Bounded outcome codes for every control action.  Every persisted
# ``manifest.reason`` — success and failure paths alike — must be a member of
# this fixed enum; writes are routed through ``Supervisor._set_reason`` so the
# invariant holds by construction.
_CONTROL_OUTCOMES = frozenset({
    "paused", "pause", "quarantined", "quarantine", "running",
    "resumed", "recovered",
    "pause_failed", "quarantine_failed", "resume_failed",
    "terminated", "terminated_forced", "escape_unverified",
    "group_remaining", "completed",
    "already_finished", "no_live_process", "control_failed",
    "appended", "applied", "unknown_profile", "family_mismatch",
    "resume_unverified", "unsupported_action",
    # 跨实例控制时无法证明进程身份/存活：既不是成功也不是 already_finished。
    "liveness_unknown",
})

# Post-exit tail-flush bounds: per-pump step and the whole-flush wall budget.
_FLUSH_STEP = 0.2
_FLUSH_BUDGET = 2.0


# --------------------------------------------------------------------------- #
# Supervisor
# --------------------------------------------------------------------------- #

@dataclass
class ManagedRunResult:
    """Bounded result of a managed execution (no raw exceptions/paths)."""

    exit_code: int
    log_tail: str = ""
    timed_out: bool = False
    aborted: bool = False
    outcome: str = ""  # bounded terminate/control outcome code


class Supervisor(HandlesMixin, NativeResumeMixin):
    """Local authority over managed CLI process groups."""

    MANIFEST_SUFFIX = ".json"

    def __init__(
        self,
        manifest_dir,
        machine_id: str,
        ops: GroupOps | None = None,
    ):
        self._machine_id = validate_opaque(machine_id, "machine_id")
        self._manifest_dir = Path(manifest_dir)
        self._manifest_dir.mkdir(parents=True, exist_ok=True)
        if ops is None:
            system = _platform_mod.system().lower()
            if system == "linux":
                ops = LinuxGroupOps()
            elif system == "windows":
                ops = WindowsGroupOps()
            else:
                ops = MacGroupOps()
        self._ops = ops
        self._entries: dict[str, _ManagedEntry] = {}
        #: Bounded in-memory mark of REVOKED adoptions (never persisted, never
        #: public; keeps ``detach`` / ``all_status`` / ``get`` semantics
        #: byte-identical).  Keyed by session id -> fixed code
        #: ``"adoption_revoked"``.  A session the probe already detached is
        #: answered with that bounded code by ``validate_attached_identity``
        #: instead of the generic unknown_session — the operator revoked this
        #: seat; the guard refuses BEFORE any signal.
        self._adoption_revoked: dict[str, str] = {}

    # ---- launch ----------------------------------------------------------- #

    def launch(
        self,
        *,
        agent: str,
        command: Sequence[str],
        cwd: str,
        env_allowlist: Mapping[str, str],
        session_id: str | None = None,
        attempt_id: str | None = None,
        capability_manifest: Mapping[str, Any] | None = None,
    ) -> ManagedSessionManifest:
        """Launch an allowlisted CLI as a fresh managed process group.

        The manifest's ``process_group_id`` is the opaque ``grp_<hex>`` here —
        the raw OS leader PID stays on the private handle and is never written
        to the durable manifest nor to any public status row.
        """
        if not validate_command(command):
            raise ValueError("command must be a non-empty argv list")

        session_id = validate_opaque(
            session_id if session_id is not None else new_opaque_id("sess"),
            "session_id")
        attempt_id = (validate_opaque(attempt_id, "attempt_id")
                      if attempt_id is not None else None)

        group_caps, _ver = self._ops.capability()
        group_id = new_opaque_id("grp")
        cap_manifest = dict(capability_manifest or {})
        now = _rfc3339()
        manifest = ManagedSessionManifest(
            session_id=session_id,
            machine_id=self._machine_id,
            process_group_id=group_id,
            agent=str(agent)[:32] or "unknown",
            attempt_id=attempt_id,
            state=RUNNING,
            group_capability=group_caps,
            platform=self._ops._platform,
            capability_manifest=cap_manifest,
            created_at=now,
            updated_at=now,
        )
        handle = self._ops.create(command, cwd, env_allowlist)
        entry = _ManagedEntry(
            manifest, handle,
            cwd=str(cwd) if cwd else None,
            env=dict(env_allowlist) if isinstance(env_allowlist, Mapping) else {},
            exe_path=str(command[0]) if command else None,
        )
        _stamp_private_identity(handle, entry)
        self._entries[session_id] = entry
        # process_group_id stays the opaque id; the real pid is never copied.
        # cwd/env stay on the private entry so they survive handle release.
        self._persist(manifest)
        self._write_owner(session_id, handle)
        self._maybe_prune()
        return manifest

    # ---- adoption (attach an existing local process) --------------------- #

    def attach_to_existing(
        self,
        pid: int,
        started_at: str,
        exe_path: str,
        native_file_path: str | None,
        agent_family: str,
        *,
        session_id: str = "",
        probe=None,
    ) -> str:
        """Bind an EXISTING local process as a managed entry (纳管).

        Fail closed at the first non-success gate, in this exact order:

        1. ``agent_family`` must be a member of the fixed allowlist
           (``report_schema.INSTANCE_FAMILIES``) — the probe is never called;
        2. a non-None ``native_file_path`` must exist and be read-able
           (``os.access`` R_OK; content is never opened/read);
        3. the CURRENT identity of ``pid`` is re-read through ``probe`` and
           compared exactly: missing/unknown → ``"pid_reused"``, a different
           ``started_at`` → ``"pid_reused"``, a different ``exe_path`` →
           ``"exe_changed"``, a reader exception → ``"no_permission"``;
        4. if ``session_id`` is already bound, the attachment is idempotent
           only for the SAME pid, otherwise the bounded ``"pid_reused"`` is
           returned and nothing is overwritten.

        Returns exactly one of:
        ``"adopted" | "pid_reused" | "exe_changed" | "no_permission" |
        "unsupported_family" | "native_file_unreadable"``.

        On success the raw ``(pid, pgid, started_at, exe_path)`` live ONLY on
        the private handle; the durable manifest and public status keep the
        bounded field set with an opaque ``grp_<hex>``.
        """
        # gate 1 — family allowlist (never before calling the reader).
        if agent_family not in INSTANCE_FAMILIES:
            return "unsupported_family"
        # gate 2 — native transcript path (presence + R_OK only; never open).
        if native_file_path is not None and not _native_readable(
                native_file_path):
            return "native_file_unreadable"
        try:
            pid_int = int(pid)
        except (TypeError, ValueError):
            return "pid_reused"
        # gate 3 — re-read the CURRENT identity; unverified ⇒ fail closed.
        reader = probe if probe is not None else self._default_attach_probe
        try:
            ident = reader(pid_int)
        except Exception:
            return "no_permission"
        if ident is None:
            return "pid_reused"
        try:
            current_started, current_exe = ident
        except Exception:
            return "no_permission"
        if str(current_started) != str(started_at or ""):
            return "pid_reused"
        if str(current_exe) != str(exe_path or ""):
            return "exe_changed"
        # gate 4 — already bound: idempotent only for the same pid.
        bound = self._entries.get(session_id)
        if bound is not None:
            if _handle_pid(bound.handle) == pid_int:
                return "adopted"
            if bound.handle is not None or not session_id.startswith("adopt_"):
                return "pid_reused"
        sid = validate_opaque(
            session_id if session_id else new_opaque_id("adopt"),
            "session_id")
        try:
            handle = self._ops.attach(pid_int, started_at, exe_path)
        except GroupError as exc:
            # Preserve the backend's bounded distinction between a vanished
            # PID and process metadata that could not be read.
            return exc.code if exc.code in ("pid_reused", "no_permission") else "pid_reused"
        except Exception:
            # process died (or the read of pgid failed) between the identity
            # re-read and the bind — autocount that as a reused/gone pid.
            return "pid_reused"
        # A prior detach left an adoption-revocation tombstone for this
        # session id; a fresh successful attach proves a CURRENT identity, so
        # the mark is cleared — the guard must judge the LIVE binding, never a
        # stale tombstone.  (The tombstone is internal-only; nothing public
        # changes.)
        self._adoption_revoked.pop(sid, None)
        group_caps, _ver = self._ops.capability()
        now = _rfc3339()
        manifest = ManagedSessionManifest(
            session_id=sid,
            machine_id=self._machine_id,
            process_group_id=new_opaque_id("grp"),
            agent=str(agent_family)[:32],
            attempt_id=None,
            state=RUNNING,
            group_capability=group_caps,
            platform=self._ops._platform,
            capability_manifest={},
            created_at=now,
            updated_at=now,
        )
        if bound is not None:
            # A fresh signed Hub command and the live identity check above
            # authorize reattachment. Keep the durable session/group identity.
            manifest = bound.manifest
        # remember the SAME identity reader used for this attach so the guard's
        # follow-up re-check sees identical live readings; probe=None replays
        # the default reader (`self._default_attach_probe`).
        # Native path/token/exe stay on the private entry (never the manifest)
        # so a later native resume does not have to re-read the live process.
        family = str(agent_family)[:32]
        token = _resume_token_from_native_path(native_file_path)
        if (family in _SESSION_PATH_RESUME_FAMILIES
                and isinstance(native_file_path, str) and native_file_path):
            # pi --resume is an interactive picker. Deterministic follow-up
            # uses --session <path>.
            token = native_file_path
        live_cwd = _live_process_cwd(pid_int)
        live_env = _bounded_spawn_env({})
        entry = _ManagedEntry(
            manifest, handle,
            probe if probe is not None else None,
            cwd=live_cwd,
            env=live_env,
            native_file_path=native_file_path,
            resume_token=token,
            exe_path=str(exe_path) if exe_path else None,
        )
        _stamp_private_identity(handle, entry)
        if (isinstance(native_file_path, str) and native_file_path
                and family in _NATIVE_RESUME_FAMILIES):
            manifest.capability_manifest = {"resume": True}
        self._entries[sid] = entry
        self._persist(manifest)
        return "adopted"

    @staticmethod
    def _default_attach_probe(pid: int):
        """Live-process identity re-read used by ``attach_to_existing``.

        Reuses ``tools.probe.discovery.enumerate_process_rows()`` (imported
        lazily to avoid an import cycle): one row per pid carries the exact
        ISO8601 ``started_at`` and canonical ``exe_path`` the discovery
        payloads use.  Returns ``(started_at, exe_path)`` for the pid or
        ``None`` when the pid has no row; raises if the process table itself
        could not be produced (so the caller fails closed with
        ``"no_permission"`` rather than guessing the pid vanished).
        """
        from tools.probe import discovery

        rows = discovery.enumerate_process_rows()
        for row in rows:
            if int(row.pid) == int(pid):
                return (row.started_at, row.exe_path)
        if getattr(discovery, "_last_enumerate_failed", False):
            raise RuntimeError("attach identity table unavailable")
        return None

    def detach(self, session_id: str) -> None:
        """Drop a managed entry WITHOUT touching the process (idempotent).

        Only ``ops.close`` (metadata/desc close) runs — never a killpg,
        terminate, receive, pause or resume.  The entry is popped from
        ``_entries`` and the durable ``<session_id>.json`` manifest is
        removed so a stale revivable adoption cannot survive a detach.  An
        unknown ``session_id`` is a no-op (no exception).
        """
        entry = self._entries.get(session_id)
        if entry is None:
            return
        with entry.control_lock:
            if self._entries.get(session_id) is not entry:
                return
            entry.control_epoch += 1
            self._entries.pop(session_id, None)
            self._detach_entry(session_id, entry)

    def _detach_entry(self, session_id: str, entry: _ManagedEntry) -> None:
        if entry is not None and isinstance(entry.handle, _AttachedHandle):
            # an ATTACHED (adopted) seat was dropped.  The bounded in-memory
            # tombstone lets a subsequent revoke-side control answer
            # ``adoption_revoked`` instead of the generic unknown_session —
            # and the guard refuses it BEFORE any signal can be attempted.
            self._adoption_revoked[session_id] = "adoption_revoked"
        if entry is None:
            return
        if entry.handle is not None:
            try:
                self._ops.close(entry.handle)
            except Exception:
                pass
        try:
            (self._manifest_dir
             / f"{session_id}{self.MANIFEST_SUFFIX}").unlink()
        except OSError:
            pass

    def release_adoption_binding(self, session_id: str) -> None:
        """Undo a failed capture reconnect while keeping its durable identity."""
        entry = self._entries.get(session_id)
        if entry is None:
            return
        with entry.control_lock:
            handle, entry.handle = entry.handle, None
            if handle is not None:
                try:
                    self._ops.close(handle)
                except Exception:
                    pass

    # ---- adoption identity guard (Task 9) -------------------------------- #

    def validate_attached_identity(self, session_id: str) -> str:
        """Re-verify an ADOPTED session before any control signal.

        Returns exactly one of the fixed bounded codes::

            "adopted"          — the session is NOT adopted (launched path
                                 passes untouched), or its live identity still
                                 matches the private handle;
            "adoption_revoked" — this probe already released the adoption
                     (operator revoke is wired through ``detach``);
            "pid_reused"       — the pid slot was reused (different
                     ``started_at``) or the pid vanished;
            "exe_changed"      — the binary swapped under the pid;
            "no_permission"    — the identity reader itself failed.

        Raises :class:`UnknownSessionError` for a session that was never seen
        (not a tombstone, no entry).  Live identity is read ONLY while the
        entry is still bound (supervised); the raw ``(pid, started_at,
        exe_path)`` never leaves the private handle / entry.
        """
        if session_id in self._adoption_revoked:
            return "adoption_revoked"
        entry = self._entries.get(session_id)
        if entry is None:
            raise UnknownSessionError(session_id)
        if entry.handle is None or not isinstance(entry.handle, _AttachedHandle):
            # NOT an adopted session — the LAUNCHED path passes untouched
            # (the "launched sessions run unchanged" invariant).
            return "adopted"
        if isinstance(entry.handle, _OwnerHandle):
            # 跨实例句柄：信号前再确认 pid 未被复用。
            current = process_start_identity(entry.handle.pid)
            if current is None or current != entry.handle.start_identity:
                return "liveness_unknown"
            return "adopted"
        # live re-read through the SAME reader used at attach (mirror gate 3
        # in attach_to_existing): fail closed on every unverifiable branch.
        reader = entry.probe if entry.probe is not None else self._default_attach_probe
        try:
            ident = reader(entry.handle.pid)
        except Exception:
            return "no_permission"
        if ident is None:
            return "pid_reused"
        try:
            current_started, current_exe = ident
        except Exception:
            return "no_permission"
        if str(current_started) != str(entry.handle.started_at or ""):
            return "pid_reused"
        if str(current_exe) != str(entry.handle.exe_path or ""):
            return "exe_changed"
        return "adopted"

    def _guard_adopted(self, session_id: str) -> str:
        """Return "" to proceed, else a bounded refusal code.

        On any non-``adopted`` code the private entry is detached (idempotent,
        exactly like a revocation; NEVER a signal).  A mismatch leaves nothing
        behind, so ``terminate`` is never reached for a revoked / mismatched
        adoption.
        """
        code = self.validate_attached_identity(session_id)
        if code != "adopted":
            entry = self._entries.get(session_id)
            if entry is not None and isinstance(entry.handle, _OwnerHandle):
                # 跨实例句柄失效：只丢弃句柄，manifest 归属仍属 launch 实例。
                entry.handle = None
                return code
            self.detach(session_id)   # idempotent; NEVER a signal
            return code
        return ""

    # ---- lookup ------------------------------------------------------------ #

    def get(self, session_id: str) -> ManagedSessionManifest:
        entry = self._entries.get(session_id)
        if entry is None:
            entry = self._refresh_from_disk(session_id)
        if entry is None:
            raise UnknownSessionError(session_id)
        return entry.manifest

    # ---- controls ---------------------------------------------------------- #

    @staticmethod
    def _signal_ok(handle, ops, sig) -> bool:
        """Return True iff the whole group was signalled (bounded)."""
        try:
            return bool(ops.group_terminate(handle, sig))
        except Exception:
            return False

    @staticmethod
    def _kill_ok(handle, ops, sig) -> bool:
        """Return True iff the forced group-kill operation was accepted."""
        try:
            return bool(ops.group_kill(handle, sig))
        except Exception:
            return False

    def pause_session(self, session_id: str) -> str:
        entry = self._entries.get(session_id)
        if entry is None:
            raise UnknownSessionError(session_id)
        with entry.control_lock:
            if self._entries.get(session_id) is not entry:
                raise UnknownSessionError(session_id)
            entry.control_epoch += 1
            return self._pause_session_locked(session_id, entry)

    def _pause_session_locked(self, session_id: str,
                              entry: _ManagedEntry) -> str:
        """Pause an owned live process group (SIGSTOP)."""
        m = entry.manifest
        if m.state in TERMINAL_STATES:
            return m.state
        handle, code = self._live_handle(session_id, entry)
        if handle is None:
            return code if code == "liveness_unknown" else (
                m.state if m.state in TERMINAL_STATES else "no_live_process")
        if not self._group_alive_checked(handle):
            return "no_live_process"
        gate = self._guard_adopted(session_id)
        if gate:
            return gate
        if not self._signal_ok(handle, self._ops, _SIGSTOP):
            return self._fail_operation(m, "pause_failed")
        m.state = PAUSED
        self._set_reason(m, "pause")
        return PAUSED

    def resume_session(self, session_id: str) -> str:
        entry = self._entries.get(session_id)
        if entry is None:
            raise UnknownSessionError(session_id)
        with entry.control_lock:
            if self._entries.get(session_id) is not entry:
                raise UnknownSessionError(session_id)
            entry.control_epoch += 1
            return self._resume_session_locked(session_id, entry)

    def _resume_session_locked(self, session_id: str,
                               entry: _ManagedEntry) -> str:
        """Resume a stopped process group (SIGCONT thaw)."""
        m = entry.manifest
        if m.state in TERMINAL_STATES:
            return m.state
        handle, code = self._live_handle(session_id, entry)
        if handle is None:
            return code if code == "liveness_unknown" else (
                m.state if m.state in TERMINAL_STATES else "no_live_process")
        if not self._group_alive_checked(handle):
            return "no_live_process"
        gate = self._guard_adopted(session_id)
        if gate:
            return gate
        if not self._signal_ok(handle, self._ops, _SIGCONT):
            return self._fail_operation(m, "resume_failed")
        m.state = RUNNING
        self._set_reason(m, "resumed")
        return RUNNING

    def quarantine_session(self, session_id: str) -> str:
        entry = self._entries.get(session_id)
        if entry is None:
            raise UnknownSessionError(session_id)
        with entry.control_lock:
            if self._entries.get(session_id) is not entry:
                raise UnknownSessionError(session_id)
            entry.control_epoch += 1
            return self._quarantine_session_locked(session_id, entry)

    def _quarantine_session_locked(self, session_id: str,
                                   entry: _ManagedEntry) -> str:
        """Quarantine a session: SIGSTOP the group before freezing state.

        A failed stop is surfaced as ``quarantine_failed`` (bounded), never
        silently reported as success.
        """
        m = entry.manifest
        if m.state in TERMINAL_STATES:
            return m.state
        handle, code = self._live_handle(session_id, entry)
        if handle is None:
            return code if code == "liveness_unknown" else (
                m.state if m.state in TERMINAL_STATES else "no_live_process")
        if not self._group_alive_checked(handle):
            return "no_live_process"
        gate = self._guard_adopted(session_id)
        if gate:
            return gate
        if not self._signal_ok(handle, self._ops, _SIGSTOP):
            return self._fail_operation(m, "quarantine_failed")
        m.state = QUARANTINED
        self._set_reason(m, "quarantine")
        return QUARANTINED

    def _group_alive_checked(self, handle) -> bool:
        try:
            return bool(self._ops.group_alive(handle))
        except Exception:
            return True  # can't verify — assume alive so control may apply

    def _fail_operation(self, m, code: str) -> str:
        if code not in _CONTROL_OUTCOMES:
            code = "control_failed"
        self._set_reason(m, code)
        return code

    def _set_reason(self, m, reason: str) -> None:
        """Single enum-validating reason writer for every transition.

        Both success and failure paths persist ``manifest.reason`` through this
        setter, so the bounded-outcome invariant holds by construction: a
        reason that is not in ``_CONTROL_OUTCOMES`` is downgraded to
        ``control_failed`` (or ``terminated`` for the terminal marker).
        """
        if reason not in _CONTROL_OUTCOMES:
            reason = ("terminated" if m.state == TERMINATED
                      else "control_failed")
        m.reason = reason
        m.updated_at = _rfc3339()
        self._persist(m)

    def cancel_attempt(self, session_id: str) -> str:
        return self.terminate_session(session_id, grace_s=0.1)

    def append_user_turn(self, session_id: str, text: str) -> str:
        entry = self._entries.get(session_id)
        if entry is None:
            raise UnknownSessionError(session_id)
        with entry.control_lock:
            if self._entries.get(session_id) is not entry:
                raise UnknownSessionError(session_id)
            if entry.resume_pending is not None:
                return "unsupported_action"
            epoch = entry.control_epoch
            outcome = self._prepare_user_turn(session_id, text, entry)
            reservation = object() if not isinstance(outcome, str) else None
            if reservation is not None:
                entry.resume_pending = reservation
        if isinstance(outcome, str):
            return outcome
        m, old, argv, cwd, env = outcome
        try:
            handle = self._ops.create(argv, cwd, env)
        except Exception:
            with entry.control_lock:
                if entry.resume_pending is reservation:
                    entry.resume_pending = None
                if entry.control_epoch != epoch or m.state in TERMINAL_STATES:
                    return "terminated" if m.state in TERMINAL_STATES else "unsupported_action"
                return self._fail_operation(m, "control_failed")
        with entry.control_lock:
            if entry.resume_pending is reservation:
                entry.resume_pending = None
            if (self._entries.get(session_id) is not entry
                    or entry.control_epoch != epoch
                    or (m.state in TERMINAL_STATES
                        and m.reason != "completed")
                    or m.state == QUARANTINED):
                try:
                    self._ops.group_kill(handle, _SIGKILL)
                except Exception:
                    pass
                try:
                    group_gone = self._wait_gone(handle, timeout=1.0)
                except Exception:
                    group_gone = False
                try:
                    reaped = bool(self._ops.group_reap(handle, timeout=1.0))
                except Exception:
                    reaped = False
                try:
                    self._ops.close(handle)
                except Exception:
                    pass
                if not group_gone or not reaped:
                    self._set_reason(m, "escape_unverified")
                    return "escape_unverified"
                if m.state in TERMINAL_STATES:
                    return m.state
                return "unsupported_action"
            if old is not None and entry.handle is old:
                try:
                    self._ops.close(old)
                except Exception:
                    pass
            entry.handle = handle
            _stamp_private_identity(handle, entry)
            m.state = RUNNING
            self._set_reason(m, "appended")
            self._write_owner(session_id, handle)
            return "appended"

    def apply_local_profile(self, session_id: str, profile_id: str) -> str:
        """Flip the node-local cc-switch current pointer.  Never pushes keys."""
        m = self.get(session_id)
        if m.state in TERMINAL_STATES:
            return m.state
        gate = self._guard_adopted(session_id)
        if gate:
            return gate
        from tools.local_profile import apply_local_profile as _apply
        outcome = _apply(profile_id, family=str(getattr(m, "agent", "") or ""))
        if outcome == "applied":
            self._set_reason(m, "applied")
            return "applied"
        if outcome in ("unknown_profile", "family_mismatch", "unsupported_action"):
            return outcome
        return self._fail_operation(m, "control_failed")

    def terminate_session(self, session_id: str, grace_s: float = 5.0) -> str:
        entry = self._entries.get(session_id)
        if entry is None:
            raise UnknownSessionError(session_id)
        with entry.control_lock:
            if self._entries.get(session_id) is not entry:
                raise UnknownSessionError(session_id)
            entry.control_epoch += 1
            return self._terminate_session_locked(session_id, grace_s, entry)

    def _terminate_if_current(self, session_id: str, handle,
                              grace_s: float) -> bool:
        entry = self._entries.get(session_id)
        if entry is None:
            return False
        with entry.control_lock:
            if (self._entries.get(session_id) is not entry
                    or entry.handle is not handle):
                return False
            entry.control_epoch += 1
            self._terminate_session_locked(session_id, grace_s, entry)
            return True

    def _terminate_session_locked(self, session_id: str, grace_s: float,
                                  entry: _ManagedEntry) -> str:
        """Stateful terminate: graceful SIGTERM -> bounded grace -> SIGKILL."""
        m = entry.manifest
        if m.state in TERMINAL_STATES:
            return m.state
        handle, code = self._live_handle(session_id, entry)
        if handle is None:
            if code == "liveness_unknown":
                # 无法证明进程已结束：不标记终态，交由 hub 记 control_failed。
                return code
            if m.state in TERMINAL_STATES:
                return m.state
            self._mark_terminated(m, "no_live_process")
            return "no_live_process"
        if not self._group_alive_checked(handle):
            self._mark_terminated(m, "already_finished")
            self._ops.close(handle)
            self._release_handle(session_id)
            return "already_finished"

        gate = self._guard_adopted(session_id)
        if gate:
            # terminate is NEVER accepted for a pending/revoked/mismatched
            # adoption: the entry was detached (no signal, idempotent) and the
            # bounded code is returned — no SIGTERM, no SIGKILL.
            return gate

        # escaped-child pre-check
        try:
            escaped = bool(self._ops.escaped(handle))
        except Exception:
            escaped = False

        if escaped:
            # a child slipped out of the group namespace: group-wide kill cannot
            # be guaranteed, so report the degraded outcome honestly.
            self._signal_ok(handle, self._ops, _SIGTERM)
            self._kill_ok(handle, self._ops, _SIGKILL)
            if self._wait_gone(handle, 2.0):
                self._mark_terminated(m, "escape_unverified")
                self._ops.close(handle)
                self._release_handle(session_id)
            else:
                # Retain the private handle while the tree remains observable;
                # cleanup is not complete until liveness is verified.
                self._set_reason(m, "escape_unverified")
            return "escape_unverified"

        # graceful
        self._signal_ok(handle, self._ops, _SIGTERM)
        if self._wait_gone(handle, max(0.0, float(grace_s))):
            self._mark_terminated(m, "terminated")
            self._ops.close(handle)
            self._release_handle(session_id)
            return "terminated"

        # forced
        self._kill_ok(handle, self._ops, _SIGKILL)
        if self._wait_gone(handle, 2.0):
            self._mark_terminated(m, "terminated_forced")
            self._ops.close(handle)
            self._release_handle(session_id)
            return "terminated_forced"
        # A successful taskkill invocation is only a delivery acknowledgement;
        # retain the handle until the process tree is proven gone.
        self._set_reason(m, "escape_unverified")
        return "escape_unverified"

    def pump_output(self, session_id: str) -> None:
        """Drain resumed children on control-client ticks to avoid full pipes."""
        m = self.get(session_id)
        handle = self._handle_of(session_id)
        if handle is None or getattr(handle, "proc", None) is None:
            return
        rc = self._ops.proc_poll(handle)
        if rc is not None:
            self._finish_completed(m, handle, session_id, rc, lambda line: None)
        else:
            self._ops.pump(handle, 0.1, lambda line: None)

    def wait_session(
        self,
        session_id: str,
        timeout_s: float = 1800.0,
        on_line=None,
        should_abort=None,
    ) -> ManagedRunResult:
        """Wait for an already-launched managed session to finish.

        The read loop is deadline-safe: it never blocks past ``timeout_s``,
        even for a child that produces no output.  A silent child is torn down
        (graceful then forced) and reported ``timed_out`` with ``exit_code
        =124``; if ``should_abort`` flips the group is torn down and reported
        ``aborted`` (130).  Lines are captured through ``on_line`` until then.
        """
        m = self.get(session_id)
        handle = self._handle_of(session_id)
        if handle is None:
            return ManagedRunResult(exit_code=125, outcome="no_live_process")
        consumer = on_line or (lambda _s: None)
        def on_line(line):
            # Codex reports the native identity before any model output. Keep
            # it with the private entry so a finished session can resume.
            if m.agent == "codex":
                try:
                    record = json.loads(line)
                    token = record.get("thread_id") if record.get("type") == "thread.started" else None
                    if isinstance(token, str) and token:
                        entry = self._entries.get(session_id)
                        if entry is not None and entry.handle is handle:
                            entry.resume_token = token
                except (ValueError, TypeError, AttributeError):
                    pass
            consumer(line)
        should_abort = should_abort or (lambda: False)
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        aborted = False

        while True:
            entry = self._entries.get(session_id)
            if entry is None:
                return ManagedRunResult(exit_code=125, outcome="superseded")
            with entry.control_lock:
                if (self._entries.get(session_id) is not entry
                        or entry.handle is not handle):
                    return ManagedRunResult(exit_code=125,
                                            outcome="superseded")
            # 1. completed process?
            try:
                rc = self._ops.proc_poll(handle)
            except Exception:
                rc = None
            if rc is not None:
                return self._finish_completed(
                    m, handle, session_id, rc, on_line)

            # 2. abort / deadline checks, then one progress pump.
            if should_abort():
                aborted = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                pr = self._ops.pump(handle, min(remaining, 0.2),
                                    on_line, should_abort)
            except Exception:
                pr = "timeout"
            if pr == "abort" or should_abort():
                aborted = True
                break
            # else: re-poll + re-check deadline at the top.

        if aborted:
            if not self._terminate_if_current(
                    session_id, handle, grace_s=1.0):
                return ManagedRunResult(exit_code=125,
                                        outcome="superseded")
            return ManagedRunResult(exit_code=130, aborted=True,
                                    outcome="aborted")
        # timed_out
        if not self._terminate_if_current(
                session_id, handle,
                grace_s=min(2.0, max(0.1, timeout_s / 20))):
            return ManagedRunResult(exit_code=125, outcome="superseded")
        return ManagedRunResult(exit_code=124, timed_out=True,
                                outcome="timed_out")

    def _finish_completed(self, m, handle, session_id, rc, on_line):
        entry = self._entries.get(session_id)
        if entry is None:
            return ManagedRunResult(exit_code=int(rc or 0),
                                    outcome="superseded")
        with entry.control_lock:
            if (self._entries.get(session_id) is not entry
                    or entry.handle is not handle):
                return ManagedRunResult(exit_code=int(rc or 0),
                                        outcome="superseded")
            return self._finish_completed_locked(
                m, handle, session_id, rc, on_line)

    def _finish_completed_locked(self, m, handle, session_id, rc, on_line):
        """Bounded completion: drain until EOF, reap, kill stragglers.

        The leader (``m.state`` the supervisor knows) has exited, but members of
        the same group may still run (e.g. a leader that forked a child and
        returned).  We keep pumping until EOF or ``_FLUSH_BUDGET`` total seconds
        of wall time, then guarantee the whole group is gone: a SIGKILL for any
        member that keeps the stdout pipe open past the budget, reaped by a
        second bounded wait.  A degraded completion is reported honestly
        (``group_remaining``, exit 124) instead of a plain success.
        """
        flush_start = time.monotonic()
        eof = False
        try:
            while time.monotonic() - flush_start < _FLUSH_BUDGET:
                pr = self._ops.pump(handle, _FLUSH_STEP, on_line)
                if pr == "eof":
                    eof = True
                    break
                if pr == "abort":
                    break
        except Exception:
            pass

        if eof:
            # Pipe closed = every writer in the group is gone.  Reap for real,
            # bounded, so no zombie remains after the supervisor closed the pipe.
            try:
                self._ops.group_reap(handle, timeout=1.0)
            except Exception:
                pass
            self._ops.close(handle)
            self._release_handle(session_id)
            self._mark_terminated(m, "completed")
            return ManagedRunResult(exit_code=int(rc or 0),
                                    outcome="finished")

        # A member kept the stdout pipe open past the flush budget.  The
        # natural completion is not clean: force-kill the whole group, then
        # reap with a bounded settle.  Report ``group_remaining`` (bounded,
        # degraded) instead of plain success.
        try:
            self._ops.group_kill(handle, _SIGKILL)
        except Exception:
            pass
        try:
            self._ops.group_reap(handle, timeout=1.0)
        except Exception:
            pass
        self._mark_terminated(m, "group_remaining")
        self._ops.close(handle)
        self._release_handle(session_id)
        return ManagedRunResult(exit_code=124, timed_out=False,
                                outcome="group_remaining")

    def _mark_terminated(self, m: ManagedSessionManifest, outcome: str) -> None:
        m.state = TERMINATED
        self._set_reason(m, outcome)
        self._drop_owner(m.session_id)

    def _wait_gone(self, handle, timeout: float) -> bool:
        """True iff the group is *provably* gone within ``timeout``.

        An exception from ``group_alive`` means liveness is UNKNOWN — that is
        not "gone".  We keep checking until the deadline; only a *definitive*
        alive=False (or a final timeout-surviving non-alive probe) counts as
        gone.  Callers that need certainty escalate to SIGKILL via the 2.0
        forced path when this returns False, so a process we cannot observe is
        forcibly reaped rather than reported dead.
        """
        if timeout <= 0:
            return not self._group_alive_checked(handle)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if not self._ops.group_alive(handle):
                    return True
            except Exception:
                # unverified: keep polling; never claim success on an unknown.
                pass
            time.sleep(min(0.05, max(0.01, (deadline - time.monotonic()) / 10)))
        try:
            return not self._ops.group_alive(handle)
        except Exception:
            return False  # aliveness unknown -> caller force-kills

    # ---- recovery / persistence --------------------------------------------- #

    def recover(self) -> dict[str, ManagedSessionManifest]:
        """Load durable manifests from ``manifest_dir`` (crash recovery).

        A recovered session is re-rooted to ``running`` but has no live OS
        handle: a subsequent terminate returns ``no_live_process`` unless the
        manifest was already terminal.  There is NO automatic adoption of
        existing OS processes.
        """
        self._maybe_prune(force=True)
        out: dict[str, ManagedSessionManifest] = {}
        for p in sorted(self._manifest_dir.glob(f"*{self.MANIFEST_SUFFIX}")):
            try:
                m = ManagedSessionManifest.from_json(p.read_text())
            except Exception:
                continue
            if not m.session_id or not m.process_group_id:
                continue
            if m.state in TERMINAL_STATES:
                self._entries[m.session_id] = _ManagedEntry(m, None)
            else:
                if not m.session_id.startswith("adopt_"):
                    m.state = RUNNING
                self._set_reason(m, "recovered")
                self._entries[m.session_id] = _ManagedEntry(m, None)
            out[m.session_id] = m
        return out

    #: 终态 manifest / owner 记录保留期与清理节流间隔。
    PRUNE_MAX_AGE_S = 7 * 86400
    PRUNE_INTERVAL_S = 3600

    def _maybe_prune(self, force: bool = False) -> None:
        now = time.monotonic()
        last = getattr(self, "_last_prune", None)
        if not force and last is not None and now - last < self.PRUNE_INTERVAL_S:
            return
        self._last_prune = now
        try:
            self.prune_manifests()
        except Exception:
            pass

    def prune_manifests(self, max_age_s: float | None = None) -> int:
        """删除超过保留期的终态 manifest、孤儿 owner 记录与残留 tmp。

        只删终态（或无法解析）且 mtime 早于保留期的文件；非终态会话的
        manifest 永不因年龄被删。返回删除的 manifest 数。
        """
        max_age = self.PRUNE_MAX_AGE_S if max_age_s is None else float(max_age_s)
        cutoff = time.time() - max_age
        removed = 0

        def old(path: Path) -> bool:
            try:
                return path.stat().st_mtime < cutoff
            except OSError:
                return False

        for p in list(self._manifest_dir.glob(f"*{self.MANIFEST_SUFFIX}")):
            if not old(p):
                continue
            try:
                m = ManagedSessionManifest.from_json(p.read_text())
                sid, terminal = m.session_id, m.state in TERMINAL_STATES
            except Exception:
                sid, terminal = None, True
            if not terminal:
                continue
            entry = self._entries.get(sid) if sid else None
            if entry is not None and entry.handle is not None:
                continue
            try:
                p.unlink()
            except OSError:
                continue
            removed += 1
            if sid:
                self._entries.pop(sid, None)
                self._drop_owner(sid)
        for p in list(self._manifest_dir.glob("*.json.tmp")):
            if old(p):
                try:
                    p.unlink()
                except OSError:
                    pass
        owners = self._manifest_dir / "owners"
        if owners.is_dir():
            for p in list(owners.iterdir()):
                sid = p.name[:-len(self.MANIFEST_SUFFIX)] if p.name.endswith(self.MANIFEST_SUFFIX) else None
                manifest = self._manifest_dir / p.name if sid else None
                if old(p) and (manifest is None or not manifest.exists()):
                    try:
                        p.unlink()
                    except OSError:
                        pass
        return removed

    def _persist(self, m: ManagedSessionManifest) -> None:
        target = self._manifest_dir / f"{m.session_id}{self.MANIFEST_SUFFIX}"
        tmp = target.with_suffix(".json.tmp")
        try:
            tmp.write_text(m.to_json())
            os.replace(str(tmp), str(target))
        except OSError:
            raise GroupError("manifest_write_failed")

    # ---- status / DTO --------------------------------------------------------- #

    def status(self, session_id: str) -> dict[str, Any]:
        """Bounded public status (never pid/cwd/argv/env/command/path)."""
        m = self.get(session_id)
        return {
            "session_id": m.session_id,
            "attempt_id": m.attempt_id,
            "process_group_id": m.process_group_id,  # opaque grp_<hex>
            "agent": m.agent,
            "machine_id": m.machine_id,
            "managed": True,
            "capture_quality": "best_effort",
            "control_capability": "available",
            "state": m.state,
            "process_group_capability": m.group_capability,
            "platform": m.platform,
            "group_capability_flags": _flags_for(m.group_capability),
            "created_at": m.created_at,
            "updated_at": m.updated_at,
            "reason": m.reason,
        }

    def all_status(self) -> dict[str, dict[str, Any]]:
        return {sid: _mask(self.get(sid)) for sid in sorted(self._entries)}


def _mask(m: ManagedSessionManifest) -> dict[str, Any]:
    return {
        "session_id": m.session_id,
        "attempt_id": m.attempt_id,
        "process_group_id": m.process_group_id,  # opaque grp_<hex>
        "agent": m.agent,
        "machine_id": m.machine_id,
        "managed": True,
        "state": m.state,
        "process_group_capability": m.group_capability,
        "platform": m.platform,
        "reason": m.reason,
    }


def _flags_for(cap: str) -> list[str]:
    out = []
    if cap == GROUP_CAPABILITY_CGROUP:
        out.append("process_group")
        out.append("cgroup")
    else:
        out.append("process_group")
    return out


def _rfc3339() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _native_readable(path: str) -> bool:
    """Presence + read-access of a native file (existence check + R_OK only).

    The content is never opened or read — availability probing only; any
    probe failure is a bounded False (fail closed).
    """
    try:
        if not path:
            return False
        if not os.path.exists(path):
            return False
        return bool(os.access(path, os.R_OK))
    except (OSError, TypeError, ValueError):
        return False

