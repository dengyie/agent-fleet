"""Pure-local agent instance discovery for the push-only probe.

This module enumerates running agent processes on the local machine and maps
them to immutable :class:`Instance` metadata rows.  It strictly executes
locally: it never opens a network socket, never writes state, never signals
or controls a process, and never returns exception text, raw unbounded paths,
or unclassified families.

Guarantees used by the rest of the probe/hub pipeline:

- family names come only from :data:`agent_profiles.OBSERVABLE_AGENT_TYPES`
  (``codex`` | ``claude_code`` | ``hermes`` | ``generic``) — there is no
  free-form family string;
- ``discover_instances()`` returns rows stably sorted by ``(pid, started_at)``;
- ``ps`` is invoked as ``ps -eo user=,pid=,ppid=,pgid=,comm=,args=`` (the same
  column set the generic connector historically used, plus ``pgid``); a
  malformed or permission-failed row is dropped on its own and never
  poisons sibling rows; a whole-command failure yields ``[]`` and sets
  ``last_discovery_error`` to the bounded ``DISCOVERY_ERROR_CODE`` so the
  caller can bucket the failure without inspecting any exception text;
- ``cmdline``/``exe_path``/``native_file_path``/``started_at`` are bounded at
  the ``Instance`` boundary (the schema boundary for Task 1); per-row
  permission/parse failures never surface as exception strings.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator

from agent_profiles import OBSERVABLE_AGENT_TYPES, PROFILES

# ---------------------------------------------------------------------------
# fixed boundaries (schema boundary for Task 1)
# ---------------------------------------------------------------------------

MAX_CMDLINE_CHARS = 200
MAX_EXE_PATH_CHARS = 512
MAX_NATIVE_PATH_CHARS = 512
MAX_STARTED_AT_CHARS = 32

PS_TIMEOUT_S = 10.0

# Fixed bounded code for whole-discovery failure; callers branch on it.
DISCOVERY_ERROR_CODE = "discovery_error"

# Minimal neutral environment so ``ps -o lstart`` output is locale-independent
# (C / POSIX weekday-month names) and no caller secrets reach the child.
_PS_ENV = {
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
}

_PS_FIELDS = "user=,pid=,ppid=,pgid=,comm=,args="
_LSTART_FIELDS = "pid=,lstart="

# Process-name aliases per observable family — derived from each Profile's
# ``basenames`` (single source of truth); ``generic`` keeps its substring
# ``pattern`` and never appears here.
_FAMILY_BASENAMES = {
    family: tuple(PROFILES[family].basenames)
    for family in OBSERVABLE_AGENT_TYPES
    if PROFILES[family].basenames
}

_GENERIC_PATTERN = (PROFILES.get("generic").pattern if PROFILES.get("generic") else "")
_GENERIC_RE = re.compile(_GENERIC_PATTERN, re.IGNORECASE)

# Families allowed in the fixed schema (== agent_profiles observable list).
_OBSERVABLE_FAMILIES = frozenset(OBSERVABLE_AGENT_TYPES)


# ---------------------------------------------------------------------------
# row and instance schema
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProcRow:
    """One parsed ``ps`` line (row adapter output).

    ``exe_path`` is best-effort absolute, ``started_at`` is ISO8601 UTC
    (``YYYY-MM-DDTHH:MM:SSZ``), and ``pgid``/``ppid`` are IDs returned by the
    OS; the extra optional fields are informational and defaulted so callers
    can construct minimal rows in tests/fixtures.
    """

    user: str
    pid: int
    pgid: int
    exe_path: str
    cmdline: str
    started_at: str = ""
    ppid: int | None = None
    comm: str | None = None


@dataclass(frozen=True)
class Instance:
    """Immutable metadata for one observable agent process.

    Field order is the Task 1 contract (positional construction in tests).
    """

    pid: int
    pgid: int
    exe_path: str
    cmdline: str
    agent_family: str            # codex | claude_code | hermes | generic
    native_file_path: str | None
    started_at: str              # ISO8601
    attachable: bool


# latest bounded discovery failure (``None`` = last call succeeded or returned
# an empty-but-healthy list); never contains exception text.
last_discovery_error: str | None = None

# set True when the most recent ``enumerate_process_rows`` could not run ``ps``
# (whole-command failure), so discover can scope empty results as an error.
_last_enumerate_failed: bool = False


# ---------------------------------------------------------------------------
# bounded local subprocess helpers
# ---------------------------------------------------------------------------

def _run_ps(argv: list[str], timeout: float = PS_TIMEOUT_S) -> str:
    """Run one bounded local ``ps``; returns stdout, or ``""`` on any failure."""
    try:
        result = subprocess.run(
            ["ps", *argv],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            cwd=tempfile.gettempdir(),
            env=_PS_ENV,
        )
    except (OSError, subprocess.SubprocessError, ValueError, UnicodeDecodeError):
        return ""
    if result.returncode != 0:
        return ""
    return (result.stdout or "")


def _iter_lines(text: str) -> Iterator[str]:
    for line in text.splitlines():
        if line.strip():
            yield line


def _lstart_to_iso(tokens: list[str]) -> str:
    """Convert C-locale ``lstart`` tokens to ``YYYY-MM-DDTHH:MM:SSZ``."""
    text = " ".join(tokens)
    for fmt in ("%a %b %d %H:%M:%S %Y", "%b %d %H:%M:%S %Y"):
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return ""


def _start_map(ltext: str) -> dict[int, str]:
    out: dict[int, str] = {}
    for line in _iter_lines(ltext or ""):
        tokens = line.split()
        if len(tokens) < 6:
            continue
        try:
            pid = int(tokens[0])
        except ValueError:
            continue
        iso = _lstart_to_iso(tokens[1:6])
        if iso:
            out[pid] = iso
    return out


def _argv0(args: str) -> str:
    if not args:
        return ""
    return args.split(None, 1)[0].strip()


def _exe_path_for(pid: int, comm: str | None, args: str) -> str:
    argv0 = _argv0(args)
    # 1) /proc/<pid>/exe (Linux) — always canonical when available.
    proc_exe = f"/proc/{pid}/exe"
    if os.path.lexists(proc_exe):
        try:
            return os.path.realpath(proc_exe) or proc_exe
        except OSError:
            pass
    # 2) absolute argv0 (macOS/Linux launchd-style).
    if argv0.startswith("/"):
        try:
            if os.path.exists(argv0):
                return os.path.realpath(argv0) or argv0
        except OSError:
            pass
    else:
        found = shutil.which(argv0)
        if found:
            return found
    # 3) comm fallback (often an absolute path itself on macOS).
    if comm and os.path.isabs(comm):
        try:
            if os.path.exists(comm):
                return os.path.realpath(comm) or comm
        except OSError:
            pass
        return comm
    return argv0 or comm or ""


# ---------------------------------------------------------------------------
# process enumeration (pure local)
# ---------------------------------------------------------------------------

def enumerate_process_rows() -> list[ProcRow]:
    """Parse the local process table into a ``ProcRow`` list.

    Never raises: a ``ps`` command failure returns ``[]`` and sets the bounded
    ``_last_enumerate_failed`` flag (callers such as ``discover_instances``
    bucket that as ``discovery_error``); malformed lines are dropped one by
    one without poisoning the rest.
    """
    global _last_enumerate_failed
    text = _run_ps(["-eo", _PS_FIELDS])
    if not text:
        _last_enumerate_failed = True
        return []
    ltext = _run_ps(["-eo", _LSTART_FIELDS])
    started = _start_map(ltext)
    _last_enumerate_failed = False
    rows: list[ProcRow] = []
    for line in _iter_lines(text):
        try:
            parts = line.split(None, 5)
            if len(parts) < 6:
                continue
            user, pid_s, ppid_s, pgid_s, comm, args = parts
            pid = int(pid_s)
            pgid = int(pgid_s)
            ppid = int(ppid_s)
        except (ValueError, TypeError, AttributeError):
            continue
        rows.append(ProcRow(
            user=user,
            pid=pid,
            pgid=pgid,
            exe_path=_exe_path_for(pid, comm, args),
            cmdline=args,
            started_at=started.get(pid, ""),
            ppid=ppid,
            comm=comm,
        ))
    return rows


# ---------------------------------------------------------------------------
# family classification (bounded to OBSERVABLE_AGENT_TYPES)
# ---------------------------------------------------------------------------

def classify_family(exe_path: str, cmdline: str) -> str | None:
    """Map one process to an observable family or ``None``.

    Classification is bounded to ``OBSERVABLE_AGENT_TYPES``; the result is
    always one of ``codex|claude_code|hermes|generic`` or ``None``.  The
    executable basename is inspected first (the exact family aliases), then
    the first argv token against the profile's ``generic`` pattern (so a
    helper process such as ``grep codex`` is never misclassified).
    """
    if not exe_path:
        return None
    base = os.path.basename(exe_path).strip().lower()
    if base:
        for family, aliases in _FAMILY_BASENAMES.items():
            if base in aliases:
                return family
    argv0 = _argv0(cmdline or "").strip().lower()
    probe = base or argv0
    if probe:
        if _GENERIC_RE.search(probe):
            return "generic"
    return None


def current_user() -> str:
    """Current local user name (best effort, never raises)."""
    import getpass
    try:
        user = getpass.getuser()
    except Exception:
        user = ""
    if not user:
        try:
            import pwd
            user = pwd.getpwuid(os.getuid()).pw_name
        except Exception:
            user = ""
    return user


# ---------------------------------------------------------------------------
# native transcript path helpers (best-effort, read-only, bounded)
# ---------------------------------------------------------------------------

def _resume_token(cmdline: str) -> str | None:
    m = re.search(r"(?:--resume|-r)\s+([A-Za-z0-9][A-Za-z0-9._-]*)", cmdline or "")
    return m.group(1) if m else None


def _most_recent_native(base_dir: str, suffix: str = ".jsonl", depth: int = 2,
                       name_contains: str | None = None) -> str | None:
    """Newest matching native transcript under ``base_dir`` (bounded, best-effort).

    The walk is limited to ``depth`` subdirectories level and stops after 512
    candidate files, so it can never become a full-tree scan.
    """
    if not base_dir or not os.path.isdir(base_dir):
        return None
    candidates: list[tuple[float, str]] = []
    root_len = len(base_dir.rstrip(os.sep)) + 1
    scanned = 0
    for dirpath, _dirnames, filenames in os.walk(base_dir):
        if dirpath[root_len:].count(os.sep) >= depth:
            continue
        for name in filenames:
            if not name.endswith(suffix):
                continue
            if name_contains and name_contains not in name.lower():
                continue
            scanned += 1
            if scanned > 512:
                return _topmost(candidates)  # bounded scan
            path = os.path.join(dirpath, name)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            candidates.append((mtime, path))
    return _topmost(candidates)


def _topmost(candidates: list[tuple[float, str]]) -> str | None:
    if not candidates:
        return None
    return max(candidates, key=lambda pair: pair[0])[1]


def _codex_native_path(cmdline: str) -> str | None:
    resume = _resume_token(cmdline)
    base = os.path.expanduser("~/.codex/sessions")
    if resume:
        p = os.path.join(base, f"{resume}.jsonl")
        if os.path.isfile(p):
            return p
    # Modern codex rollouts live at YYYY/MM/DD/rollout-*.jsonl — three
    # directory levels; depth must cover them or every lookup returns None.
    return _most_recent_native(base, ".jsonl", depth=3)


def _claude_native_path(cmdline: str) -> str | None:
    resume = _resume_token(cmdline)
    base = os.path.expanduser("~/.claude/projects")
    if resume:
        # Claude uses a project-encoded path; best-effort: match the newest
        # file whose stem contains the resume token within a bounded scan.
        found = _most_recent_native(base, ".jsonl", depth=2,
                                    name_contains=resume.lower())
        if found:
            return found
    return _most_recent_native(base, ".jsonl", depth=2)


# Family-specific native path resolvers above; families WITHOUT dedicated
# logic (pi and future Profile-driven additions) fall back to the registry's
# ``native_session_root``/``native_session_depth`` fields.
_PROFILE_NATIVE_RESOLVERS = {
    "codex": _codex_native_path,
    "claude_code": _claude_native_path,
}


def _profile_native_path(family: str, cmdline: str) -> str | None:
    """Profile-driven native transcript lookup (bounded, best-effort)."""
    profile = PROFILES.get(family)
    if not profile or not profile.native_session_root:
        return None
    base = os.path.expanduser(profile.native_session_root)
    return _most_recent_native(base, ".jsonl", depth=profile.native_session_depth)


def native_path_for(row: ProcRow) -> str | None:
    """Best-effort native transcript path for a known agent family.

    Read-only and bounded; returns ``None`` when it cannot be determined.
    Dedicated resolvers keep codex/claude semantics; the registry fallback
    covers profile-declared families without dedicated logic.
    """
    try:
        family = classify_family(str(row.exe_path or ""), str(row.cmdline or ""))
    except Exception:
        return None
    resolver = _PROFILE_NATIVE_RESOLVERS.get(family)
    if resolver is not None:
        try:
            return resolver(row.cmdline or "")
        except Exception:
            return None
    try:
        return _profile_native_path(family, row.cmdline or "")
    except Exception:
        return None


def can_attach(row: ProcRow, native: str | None) -> bool:
    """Attachability: same-user and (if a native path is offered) readability."""
    try:
        if (row.user or "") != current_user():
            return False
    except Exception:
        return False
    if isinstance(native, str) and native:
        try:
            return os.access(native, os.R_OK)
        except (OSError, ValueError):
            return False
    return True


# ---------------------------------------------------------------------------
# public adapter: rows -> ordered instances
# ---------------------------------------------------------------------------

def _instance_from_row(row: Any) -> Instance | None:
    try:
        pid = int(row.pid)
        pgid = int(row.pgid)
    except (TypeError, ValueError):
        return None
    exe_path = str(row.exe_path or "") if row.exe_path is not None else ""
    cmdline = str(row.cmdline or "") if row.cmdline is not None else ""
    if not exe_path.strip():
        return None
    family = classify_family(exe_path, cmdline)
    if family not in _OBSERVABLE_FAMILIES:
        return None
    try:
        native = native_path_for(row)
    except Exception:
        native = None
    try:
        attachable = bool(can_attach(row, native))
    except Exception:
        attachable = False
    if not isinstance(native, str):
        native = None
    else:
        native = native[:MAX_NATIVE_PATH_CHARS] or None
    started = row.started_at if isinstance(row.started_at, str) else ""
    return Instance(
        pid=pid,
        pgid=pgid,
        exe_path=exe_path[:MAX_EXE_PATH_CHARS],
        cmdline=cmdline[:MAX_CMDLINE_CHARS],
        agent_family=family,
        native_file_path=native,
        started_at=started[:MAX_STARTED_AT_CHARS],
        attachable=attachable,
    )


def discover_instances() -> list[Instance]:
    """Return locally-discovered agent instances sorted by ``(pid, started_at)``.

    Never raises: a whole ``ps`` failure (or any bad row) is handled
    separately, and a whole-command failure sets ``last_discovery_error`` to
    the bounded ``DISCOVERY_ERROR_CODE`` (never exception text).
    """
    global last_discovery_error
    last_discovery_error = None
    try:
        rows = enumerate_process_rows()
    except Exception:
        last_discovery_error = DISCOVERY_ERROR_CODE
        return []
    if _last_enumerate_failed:
        last_discovery_error = DISCOVERY_ERROR_CODE
    out: list[Instance] = []
    for row in rows:
        try:
            inst = _instance_from_row(row)
            if inst is not None:
                out.append(inst)
        except Exception:
            # malformed/permission-bad row: skip only it.
            continue
    out.sort(key=lambda inst: (inst.pid, inst.started_at))
    return out


__all__ = [
    "DISCOVERY_ERROR_CODE",
    "MAX_CMDLINE_CHARS",
    "Instance",
    "OBSERVABLE_AGENT_TYPES",
    "ProcRow",
    "can_attach",
    "classify_family",
    "current_user",
    "discover_instances",
    "enumerate_process_rows",
    "last_discovery_error",
    "native_path_for",
]