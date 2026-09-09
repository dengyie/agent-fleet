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

公开输出（``status`` / manifest）永远不包含 pid、cwd、argv、env、command、
path 或 token/secret；``process_group_id`` 是 opaque ``grp_<hex>``，绝不会
是裸 leader PID。
"""
from __future__ import annotations

import os
import platform as _platform_mod
import select
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from report_schema import INSTANCE_FAMILIES

from tools.supervisor.model import (
    GROUP_CAPABILITY_CGROUP,
    GROUP_CAPABILITY_PGROUP,
    PAUSED,
    QUARANTINED,
    RUNNING,
    TERMINATED,
    TERMINAL_STATES,
    ManagedSessionManifest,
    new_opaque_id,
    validate_opaque,
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
    "group_remaining",
    "already_finished", "no_live_process", "control_failed",
    "appended", "applied", "unknown_profile", "family_mismatch",
    "resume_unverified", "unsupported_action",
})

# Raw byte-buffer cap per process group handle (drops oldest bytes beyond
# this, so a child emitting a huge line-free stream cannot OOM the Reader).
_ABUF_MAX = 1 << 20
# Post-exit tail-flush bounds: per-pump step and the whole-flush wall budget.
_FLUSH_STEP = 0.2
_FLUSH_BUDGET = 2.0


class GroupError(RuntimeError):
    """Stable, bounded process-group error (no raw text)."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail[:200]
        super().__init__(code)


class UnknownSessionError(KeyError):
    """Raised for a session id the Supervisor has no manifest for."""

    def __init__(self, session_id: str):
        super().__init__(f"unknown session: {session_id}")


# --------------------------------------------------------------------------- #
# GroupOps — smallest possible platform abstraction over a process group.
# --------------------------------------------------------------------------- #

class GroupOps:
    """Contract for a supervised process-group backend."""

    def __init__(self, platform: str | None = None):
        self._platform = (platform or _platform_mod.system()).lower()

    # ---- capability --------------------------------------------------------

    def capability(self):
        """Return ``(capability_level, version)``."""
        if self._platform == "linux" and self._cgroup_available():
            return (GROUP_CAPABILITY_CGROUP, "2")
        return (GROUP_CAPABILITY_PGROUP, "1")

    def _cgroup_available(self) -> bool:
        try:
            for p in ("/sys/fs/cgroup/cgroup.controllers",
                      "/sys/fs/cgroup/unified/cgroup.controllers"):
                if os.path.exists(p) and os.access(p, os.R_OK):
                    return True
        except OSError:
            return False
        return False

    # ---- spawn / query ----------------------------------------------------- #
    def create(self, argv: Sequence[str], cwd: str, env: Mapping[str, str]):
        raise NotImplementedError

    def attach(self, pid: int, started_at: str, exe_path: str):
        """Create the private handle for an EXISTING local process group."""
        raise NotImplementedError

    def proc_poll(self, handle) -> int | None:
        raise NotImplementedError

    def proc_wait(self, handle, timeout: float | None = None) -> int | None:
        raise NotImplementedError

    def group_id(self, handle) -> str:
        raise NotImplementedError

    def group_alive(self, handle) -> bool:
        raise NotImplementedError

    def group_terminate(self, handle, sig: int) -> bool:
        """Best-effort signal to the whole group (returns False on failure)."""
        raise NotImplementedError

    def group_kill(self, handle, sig: int) -> bool:
        raise NotImplementedError

    def group_reap(self, handle, timeout: float | None = None) -> bool:
        raise NotImplementedError

    def escaped(self, handle) -> bool:
        return False

    def close(self, handle) -> None:
        pass

    def pump(self, handle, timeout: float, on_line=None,
             should_abort=None) -> str:
        """Output drain with a bounded wait.

        Returns one of ``"line"`` (a line was delivered to ``on_line``),
        ``"eof"`` (pipe closed), ``"timeout"`` (no line within ``timeout``),
        ``"abort"`` (``should_abort`` became true).
        """
        time.sleep(max(0.001, min(0.05, float(timeout))))
        return "timeout"


# --------------------------------------------------------------------------- #
# Real POSIX backend (subprocess in a fresh session + os.killpg)
# --------------------------------------------------------------------------- #

class POSIXGroupOps(GroupOps):
    """Shared POSIX implementation (start_new_session + ``os.killpg``)."""

    _READ_CHUNK = 65536

    @staticmethod
    def _attached(handle) -> bool:
        """True iff ``handle`` binds an *attached* (not spawned) process."""
        return isinstance(handle, _AttachedHandle)

    def attach(self, pid: int, started_at: str, exe_path: str):
        """Bind an existing local group (pgid of the leader pid)."""
        pgid = os.getpgid(int(pid))
        return _AttachedHandle(pid=int(pid), pgid=int(pgid),
                               started_at=started_at, exe_path=exe_path)

    def create(self, argv, cwd, env):
        try:
            proc = subprocess.Popen(
                list(argv),
                cwd=cwd or "/tmp",
                env=_bounded_spawn_env(env),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
                restore_signals=True,
                bufsize=0,
            )
        except OSError:
            raise GroupError("spawn_failed")
        return _Bound(proc=proc)

    def proc_poll(self, handle):
        if self._attached(handle):
            return None  # not our child; liveness lives on the group
        return handle.proc.poll()

    def proc_wait(self, handle, timeout=None):
        if self._attached(handle):
            return None
        try:
            return handle.proc.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            return None

    def group_id(self, handle):
        # Not used to derive the manifest's process_group_id (opaque keeps it
        # so the raw leader PID never reaches public outputs).
        if self._attached(handle):
            return f"grp_{handle.pgid}"
        return f"grp_{handle.proc.pid}"

    def group_alive(self, handle):
        if self._attached(handle):
            # liveness = the process GROUP still exists (no /proc deref).
            try:
                os.killpg(int(handle.pgid), 0)
            except ProcessLookupError:
                return False
            except (PermissionError, OSError):
                return True  # cannot verify → assume alive
            return True
        return handle.proc.poll() is None

    def group_terminate(self, handle, sig):
        if self._attached(handle):
            try:
                os.killpg(int(handle.pgid), sig)
            except (ProcessLookupError, PermissionError, OSError):
                return False
            return True
        try:
            os.killpg(handle.proc.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return False
        return True

    def group_kill(self, handle, sig):
        return self.group_terminate(handle, sig)

    def group_reap(self, handle, timeout=None):
        if self._attached(handle):
            return True  # not our child — nothing to wait/reap
        try:
            handle.proc.wait(timeout=max(0.0, timeout or 0))
        except (subprocess.TimeoutExpired, OSError):
            return False
        return True

    def close(self, handle):
        if self._attached(handle):
            return  # metadata close only; an attached handle owns no pipe
        try:
            handle.proc.stdout.close()
        except Exception:
            pass

    # ---- deadline-safe output reading --------------------------------------

    def _drain_lines(self, handle, on_line, flush=False) -> bool:
        """Split complete lines from the handle's internal byte buffer.

        Delivers each line to ``on_line`` (truncated to a bounded cap).
        Returns True if at least one line was delivered.
        """
        buf = handle._abuf
        handled_only_dirty = False
        delivered = False
        while True:
            idx = buf.find(b"\n")
            if idx < 0:
                break
            raw = buf[:idx]
            buf = buf[idx + 1:]
            if raw:
                line = raw.decode("utf-8", "replace").rstrip("\r")
                if line:
                    delivered = True
                    try:
                        if on_line:
                            on_line(line[:2048])
                    except Exception:
                        pass
        if flush and buf:
            line = buf.decode("utf-8", "replace").rstrip("\r\n")
            if line:
                delivered = True
                try:
                    if on_line:
                        on_line(line[:2048])
                except Exception:
                    pass
            buf = b""
        handle._abuf = buf
        return delivered

    def pump(self, handle, timeout, on_line=None, should_abort=None) -> str:
        """Read the child pipe with progress/timeout guarantees.

        Never blocks past ``timeout`` (per-step cap 0.2s), so a silent child
        cannot stall ``Supervisor.wait_session``.  ``select`` + ``os.read``
        fills the byte buffer; complete lines are handed to ``on_line``.
        """
        if handle is None or handle.proc is None:
            return "eof"
        fds = handle.proc.stdout
        if fds is None:
            return "eof"
        try:
            fd = fds.fileno()
        except Exception:
            return "eof"
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            if should_abort is not None and should_abort():
                return "abort"
            if self._drain_lines(handle, on_line):
                return "line"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout"
            try:
                r, _, _ = select.select([fd], [], [],
                                        max(0.0, min(remaining, 0.2)))
            except (OSError, ValueError):
                return "eof"
            if not r:
                continue
            try:
                chunk = os.read(fd, self._READ_CHUNK)
            except (OSError, ValueError):
                return "eof"
            if not chunk:
                # EOF — emit any partial trailing line, report eof.
                self._drain_lines(handle, on_line, flush=True)
                handle._abuf = b""
                return "eof"
            # Append, but keep the raw byte buffer bounded: drop oldest bytes
            # beyond the cap so a line-free stream (progress bars using
            # ``\r``, binary tail) can never grow without limit.
            if len(handle._abuf) + len(chunk) > _ABUF_MAX:
                keep = handle._abuf[-(_ABUF_MAX - len(chunk)):] if len(
                    chunk) < _ABUF_MAX else b""
                handle._abuf = keep + chunk
            else:
                handle._abuf += chunk


class LinuxGroupOps(POSIXGroupOps):
    """Linux backend: cgroup v2 when available, else process-group only."""

    def __init__(self, cgroup_available: bool | None = None):
        super().__init__(platform="linux")
        self._override = cgroup_available

    def _cgroup_available(self) -> bool:
        if self._override is not None:
            return self._override
        return super()._cgroup_available()

    def capability(self):
        if self._cgroup_available():
            return (GROUP_CAPABILITY_CGROUP, "2")
        return (GROUP_CAPABILITY_PGROUP, "1")


class MacGroupOps(POSIXGroupOps):
    """macOS backend: process-group only (os.killpg), never tmux."""

    def __init__(self):
        super().__init__(platform="darwin")

    def capability(self):
        return (GROUP_CAPABILITY_PGROUP, "1")


@dataclass
class _Bound:
    """Mutable handle bundle for an owned process group.

    The durable manifest never stores the raw pid/cwd/env/argv so a
    recovered Supervisor cannot auto-restart it.  Optional native-resume
    identity here is in-memory only and is also copied onto ``_ManagedEntry``
    so it survives ``_release_handle``.
    """

    proc: Any = None
    group_id: str = ""
    native_file_path: str | None = None
    cwd: str | None = None
    env: dict[str, str] | None = None
    resume_token: str | None = None
    exe_path: str | None = None
    _abuf: bytes = b""


@dataclass
class _AttachedHandle:
    """Private handle for an EXISTING local process bound via ``attach_to_*``.

    ``pid`` / ``pgid`` / ``started_at`` / ``exe_path`` plus optional native
    resume identity (``native_file_path`` / ``cwd`` / ``env`` /
    ``resume_token``) live ONLY here — never on the durable manifest, never
    in any public status/JSON row.  ``proc`` stays ``None`` because the
    process was not spawned by the Supervisor, so there is no pipe/Popen to
    read; real backends signal it by the *process group* id
    (``os.killpg(pgid, …)``).
    """

    pid: int
    pgid: int
    started_at: str
    exe_path: str
    proc: Any = None
    native_file_path: str | None = None
    cwd: str | None = None
    env: dict[str, str] | None = None
    resume_token: str | None = None
    _abuf: bytes = b""


# --------------------------------------------------------------------------- #
# Supervisor
# --------------------------------------------------------------------------- #

@dataclass
class _ManagedEntry:
    manifest: ManagedSessionManifest
    handle: Any = None  # GroupOps handle (live only after launch/resume)
    probe: Any = None   # the LIVE-identity reader used at attach (Task 9)
    # Private native-resume identity.  Never persisted, never public.
    # Survives ``_release_handle`` so a naturally finished session can still
    # be resumed into the same native transcript (not SIGCONT, not /tmp).
    cwd: str | None = None
    env: dict[str, str] | None = None
    native_file_path: str | None = None
    resume_token: str | None = None
    exe_path: str | None = None


@dataclass
class ManagedRunResult:
    """Bounded result of a managed execution (no raw exceptions/paths)."""

    exit_code: int
    log_tail: str = ""
    timed_out: bool = False
    aborted: bool = False
    outcome: str = ""  # bounded terminate/control outcome code


class Supervisor:
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
            if _platform_mod.system().lower() == "linux":
                ops = LinuxGroupOps()
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
            return "pid_reused"
        sid = validate_opaque(
            session_id if session_id else new_opaque_id("adopt"),
            "session_id")
        try:
            handle = self._ops.attach(pid_int, started_at, exe_path)
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
        # remember the SAME identity reader used for this attach so the guard's
        # follow-up re-check sees identical live readings; probe=None replays
        # the default reader (`self._default_attach_probe`).
        # Native path/token/exe stay on the private entry (never the manifest)
        # so a later native resume does not have to re-read the live process.
        entry = _ManagedEntry(
            manifest, handle,
            probe if probe is not None else None,
            native_file_path=native_file_path,
            resume_token=_resume_token_from_native_path(native_file_path),
            exe_path=str(exe_path) if exe_path else None,
        )
        _stamp_private_identity(handle, entry)
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
        entry = self._entries.pop(session_id, None)
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
            self.detach(session_id)   # idempotent; NEVER a signal
            return code
        return ""

    # ---- lookup ------------------------------------------------------------ #

    def get(self, session_id: str) -> ManagedSessionManifest:
        entry = self._entries.get(session_id)
        if entry is None:
            raise UnknownSessionError(session_id)
        return entry.manifest

    def _handle_of(self, session_id: str):
        entry = self._entries.get(session_id)
        if entry is None:
            raise UnknownSessionError(session_id)
        return entry.handle

    def _release_handle(self, session_id: str) -> None:
        """Drop the private OS handle so no raw pid/cwd/argv remains live."""
        entry = self._entries.get(session_id)
        if entry is not None:
            entry.handle = None

    # ---- controls ---------------------------------------------------------- #

    @staticmethod
    def _signal_ok(handle, ops, sig) -> bool:
        """Return True iff the whole group was signalled (bounded)."""
        try:
            return bool(ops.group_terminate(handle, sig))
        except Exception:
            return False

    def pause_session(self, session_id: str) -> str:
        """Pause an owned live process group (SIGSTOP)."""
        m = self.get(session_id)
        if m.state in TERMINAL_STATES:
            return m.state
        handle = self._handle_of(session_id)
        if handle is None or not self._group_alive_checked(handle):
            return "no_live_process"
        gate = self._guard_adopted(session_id)
        if gate:
            return gate
        if not self._signal_ok(handle, self._ops, signal.SIGSTOP):
            return self._fail_operation(m, "pause_failed")
        m.state = PAUSED
        self._set_reason(m, "pause")
        return PAUSED

    def resume_session(self, session_id: str) -> str:
        """Resume a stopped process group (SIGCONT thaw)."""
        m = self.get(session_id)
        if m.state in TERMINAL_STATES:
            return m.state
        handle = self._handle_of(session_id)
        if handle is None or not self._group_alive_checked(handle):
            return "no_live_process"
        gate = self._guard_adopted(session_id)
        if gate:
            return gate
        if not self._signal_ok(handle, self._ops, signal.SIGCONT):
            return self._fail_operation(m, "resume_failed")
        m.state = RUNNING
        self._set_reason(m, "resumed")
        return RUNNING

    def quarantine_session(self, session_id: str) -> str:
        """Quarantine a session: SIGSTOP the group before freezing state.

        A failed stop is surfaced as ``quarantine_failed`` (bounded), never
        silently reported as success.
        """
        m = self.get(session_id)
        if m.state in TERMINAL_STATES:
            return m.state
        handle = self._handle_of(session_id)
        if handle is None or not self._group_alive_checked(handle):
            return "no_live_process"
        gate = self._guard_adopted(session_id)
        if gate:
            return gate
        if not self._signal_ok(handle, self._ops, signal.SIGSTOP):
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

    def _resume_allowed(self, m, session_id: str) -> bool:
        """True only when the family has a proven resume capability.

        An explicit ``capability_manifest['resume']`` wins.  Otherwise a
        verifiable family (codex / claude_code) may be probed from the
        attached exe; pi / Hermes / missing exe stay False.
        """
        caps = getattr(m, "capability_manifest", None)
        if not isinstance(caps, Mapping):
            caps = {}
        if "resume" in caps:
            return bool(caps.get("resume"))
        family = str(getattr(m, "agent", "") or "")
        if family not in ("codex", "claude_code"):
            return False
        exe = None
        try:
            handle = self._handle_of(session_id)
        except Exception:
            handle = None
        if handle is not None:
            exe = getattr(handle, "exe_path", None)
        if not isinstance(exe, str) or not exe:
            entry = self._entries.get(session_id)
            exe = getattr(entry, "exe_path", None) if entry is not None else None
        if not isinstance(exe, str) or not exe:
            return False
        try:
            from tools.session.probe import probe_agent
            manifest = probe_agent(family, [exe], verify_resume=True)
            claimed = bool(getattr(manifest, "resume", False))
        except Exception:
            return False
        try:
            updated = dict(caps)
            updated["resume"] = claimed
            m.capability_manifest = updated
            self._persist(m)
        except Exception:
            pass
        return claimed

    def append_user_turn(self, session_id: str, text: str) -> str:
        """Follow-up via native session resume, never stdin /tmp sibling.

        ``create(argv, None, {})`` would land in ``/tmp``, inherit a secret-free
        env, close unused stdout (SIGPIPE), and orphan the handle — that is
        not resume.  Hermes stays ``unsupported_action``.  Families without
        proven resume stay ``resume_unverified``.  Claimed resume without a
        private native identity (cwd/env/token/exe) is still refused.  A
        complete identity starts a new CLI process with ``--resume``, the
        original cwd/env, and stdin=DEVNULL; it is never SIGCONT thaw.
        Private identity lives on the entry so a naturally finished session
        can still resume after ``_release_handle``.
        """
        m = self.get(session_id)
        gate = self._guard_adopted(session_id)
        if gate:
            return gate
        family = str(getattr(m, "agent", "") or "")
        if family == "hermes":
            return "unsupported_action"
        if family not in ("codex", "claude_code", "pi"):
            return "unsupported_action"
        if not self._resume_allowed(m, session_id):
            return "resume_unverified"
        if not isinstance(text, str) or not text:
            return "control_failed"
        if m.state == QUARANTINED:
            return "unsupported_action"
        identity = _native_resume_identity(self._entries.get(session_id))
        if identity is None:
            if m.state in TERMINAL_STATES:
                return m.state
            # Do not call GroupOps.create.  Missing native identity is a
            # sibling spawn, not resume.
            return "unsupported_action"
        cwd, env, token, exe = identity
        argv = _native_resume_argv(family, exe, token, text)
        if not validate_command(argv) or not cwd:
            return "unsupported_action"
        entry = self._entries[session_id]
        old = entry.handle
        try:
            handle = self._ops.create(argv, cwd, env)
        except Exception:
            return self._fail_operation(m, "control_failed")
        if old is not None:
            try:
                self._ops.close(old)
            except Exception:
                pass
        entry.handle = handle
        _stamp_private_identity(handle, entry)
        m.state = RUNNING
        self._set_reason(m, "appended")
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
        """Stateful terminate: graceful SIGTERM -> bounded grace -> SIGKILL."""
        m = self.get(session_id)
        if m.state in TERMINAL_STATES:
            return m.state
        handle = self._handle_of(session_id)
        if handle is None or not self._group_alive_checked(handle):
            if handle is None:
                self._mark_terminated(m, "no_live_process")
                return "no_live_process"
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
            self._signal_ok(handle, self._ops, signal.SIGTERM)
            self._signal_ok(handle, self._ops, signal.SIGKILL)
            self._mark_terminated(m, "escape_unverified")
            self._ops.close(handle)
            self._release_handle(session_id)
            return "escape_unverified"

        # graceful
        self._signal_ok(handle, self._ops, signal.SIGTERM)
        if self._wait_gone(handle, max(0.0, float(grace_s))):
            self._mark_terminated(m, "terminated")
            self._ops.close(handle)
            self._release_handle(session_id)
            return "terminated"

        # forced
        self._signal_ok(handle, self._ops, signal.SIGKILL)
        if self._wait_gone(handle, 2.0):
            self._mark_terminated(m, "terminated_forced")
            self._ops.close(handle)
            self._release_handle(session_id)
            return "terminated_forced"
        self._mark_terminated(m, "escape_unverified")
        self._ops.close(handle)
        self._release_handle(session_id)
        return "escape_unverified"

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
        on_line = on_line or (lambda _s: None)
        should_abort = should_abort or (lambda: False)
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        aborted = False
        timed_out = False

        while True:
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
                timed_out = True
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
            try:
                self.terminate_session(session_id, grace_s=1.0)
            except Exception:
                pass
            self._ops.close(handle)
            self._release_handle(session_id)
            return ManagedRunResult(exit_code=130, aborted=True,
                                    outcome="aborted")
        # timed_out
        try:
            self.terminate_session(
                session_id, grace_s=min(2.0, max(0.1, timeout_s / 20)))
        except Exception:
            pass
        self._ops.close(handle)
        self._release_handle(session_id)
        return ManagedRunResult(exit_code=124, timed_out=True,
                                outcome="timed_out")

    def _finish_completed(self, m, handle, session_id, rc, on_line):
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
            self._mark_terminated(m, "terminated")
            return ManagedRunResult(exit_code=int(rc or 0),
                                    outcome="finished")

        # A member kept the stdout pipe open past the flush budget.  The
        # natural completion is not clean: force-kill the whole group, then
        # reap with a bounded settle.  Report ``group_remaining`` (bounded,
        # degraded) instead of plain success.
        try:
            self._ops.group_kill(handle, signal.SIGKILL)
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
                m.state = RUNNING
                self._set_reason(m, "recovered")
                self._entries[m.session_id] = _ManagedEntry(m, None)
            out[m.session_id] = m
        return out

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


# Spawn environment: the *only* env a managed CLI sees.  An empty allowlist
# must never map to ``None`` (which would inherit the entire parent env,
# including secrets); it maps to a bounded, well-known-safe base instead.
# Arbitrary parent env vars are never passed through.
_SAFE_ENV_KEYS = (
    "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL",
    "PATH", "TMPDIR", "TERM", "COLORTERM", "CI", "GITHUB_ACTIONS",
)


def _bounded_spawn_env(env):
    """Return the exact allowlist, or a bounded base env when empty."""
    if env:
        return dict(env)
    base = {}
    for key in _SAFE_ENV_KEYS:
        val = os.environ.get(key)
        if val is not None:
            base[key] = val
    base.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")
    base.setdefault("LANG", "C.UTF-8")
    base.setdefault("HOME", os.environ.get("HOME") or os.path.expanduser("~"))
    return base


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


def _stamp_private_identity(handle, entry) -> None:
    """Copy private native-resume fields between entry and live handle."""
    if entry is None:
        return
    names = ("cwd", "env", "native_file_path", "resume_token", "exe_path")
    for name in names:
        val = getattr(entry, name, None)
        if val is None and handle is not None:
            val = getattr(handle, name, None)
        if val is None:
            continue
        if name == "env" and isinstance(val, Mapping):
            val = dict(val)
        try:
            setattr(entry, name, val)
        except Exception:
            pass
        if handle is None:
            continue
        try:
            setattr(handle, name, val)
        except Exception:
            pass


def _resume_token_from_native_path(path: str | None) -> str | None:
    """Best-effort token from a native transcript filename; never opens it."""
    if not isinstance(path, str) or not path:
        return None
    stem = Path(path).stem
    if not stem or stem.startswith("rollout-"):
        return None
    if not all(ch.isalnum() or ch in "._-" for ch in stem):
        return None
    if not stem[0].isalnum():
        return None
    return stem


def _native_resume_identity(entry):
    """Return ``(cwd, env, token, exe)`` when a private native identity exists.

    Identity is read from the entry first (survives ``_release_handle``) and
    then from the live handle.  Missing cwd/env/token/exe, or a ``/tmp``
    fallback cwd, is not resume — callers must refuse sibling spawn.
    """
    if entry is None:
        return None
    handle = getattr(entry, "handle", None)

    def _pick(name):
        for src in (entry, handle):
            if src is None:
                continue
            val = getattr(src, name, None)
            if val:
                return val
        return None

    cwd = _pick("cwd")
    env = _pick("env")
    token = _pick("resume_token")
    exe = _pick("exe_path")
    native = _pick("native_file_path")
    if not isinstance(cwd, str) or not cwd or cwd == "/tmp":
        return None
    if not isinstance(token, str) or not token:
        return None
    if not isinstance(exe, str) or not exe:
        return None
    if not isinstance(env, Mapping):
        return None
    env = dict(env)
    entry.cwd = cwd
    entry.env = env
    entry.resume_token = token
    entry.exe_path = exe
    if isinstance(native, str) and native:
        entry.native_file_path = native
    _stamp_private_identity(handle, entry)
    return (cwd, env, token, exe)


_RESUME_FORBIDDEN_FLAGS = frozenset({
    "--ephemeral", "--ignore-user-config", "-p", "--print",
})


def _native_resume_argv(family: str, exe: str, token: str, text: str) -> list[str]:
    """Build a native ``--resume`` argv.  Never reuse adapter defaults."""
    if not exe or not token or not isinstance(text, str) or not text:
        return []
    if family == "codex":
        argv = [exe, "exec", "--resume", token, text]
    elif family in ("claude_code", "pi"):
        argv = [exe, "--resume", token, text]
    else:
        return []
    if any(flag in argv for flag in _RESUME_FORBIDDEN_FLAGS):
        return []
    return argv


def _handle_pid(handle) -> int | None:
    """Return the raw pid bound to a private handle, or None when absent."""
    if handle is None:
        return None
    raw = getattr(handle, "pid", None)
    if raw is None and getattr(handle, "proc", None) is not None:
        raw = getattr(handle.proc, "pid", None)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def validate_command(argv: Sequence[str]) -> bool:
    if not isinstance(argv, Sequence) or isinstance(argv, (str, bytes)):
        return False
    if not argv:
        return False
    return all(isinstance(a, str) and a for a in argv)
