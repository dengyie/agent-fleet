"""Pure-local agent instance discovery for the push-only probe.

This module enumerates running agent processes on the local machine and maps
them to immutable :class:`Instance` metadata rows.  It strictly executes
locally: it never opens a network socket, never writes state, never signals
or controls a process, and never returns exception text, raw unbounded paths,
or unclassified families.

Guarantees used by the rest of the probe/hub pipeline:

- family names come only from :data:`agent_profiles.OBSERVABLE_AGENT_TYPES`
  (``codex`` | ``claude_code`` | ``hermes`` | ``pi`` | ``zcode`` |
  ``generic``) — there is no free-form family string;
- ``discover_instances()`` returns rows stably sorted by ``(pid, started_at)``;
- ``ps`` is invoked as ``ps -eo user=,pid=,ppid=,pgid=,args=``.  ``comm`` is
  deliberately NOT a column: it may contain spaces (misaligning a whitespace
  split) and macOS truncates it to 16 chars, so the executable path comes from
  ``/proc/<pid>/exe`` (Linux), ``proc_pidpath`` (macOS) or the space-aware
  argv0 resolution instead; a
  malformed or permission-failed row is dropped on its own and never
  poisons sibling rows; a whole-command failure yields ``[]`` and sets
  ``last_discovery_error`` to the bounded ``DISCOVERY_ERROR_CODE`` so the
  caller can bucket the failure without inspecting any exception text;
- ``cmdline``/``exe_path``/``native_file_path``/``started_at`` are bounded at
  the ``Instance`` boundary (the schema boundary for Task 1); per-row
  permission/parse failures never surface as exception strings.
"""

from __future__ import annotations

import base64
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator

from agent_profiles import OBSERVABLE_AGENT_TYPES, PROFILES
from tools import local_process

# ---------------------------------------------------------------------------
# fixed boundaries (schema boundary for Task 1)
# ---------------------------------------------------------------------------

MAX_CMDLINE_CHARS = 200
MAX_EXE_PATH_CHARS = 512
MAX_NATIVE_PATH_CHARS = 512
MAX_STARTED_AT_CHARS = 32

PS_TIMEOUT_S = 10.0
POWERSHELL_TIMEOUT_S = 15.0

# Fixed bounded code for whole-discovery failure; callers branch on it.
DISCOVERY_ERROR_CODE = "discovery_error"

# Minimal neutral environment so ``ps -o lstart`` output is UTC and locale-independent
# (C / POSIX weekday-month names) and no caller secrets reach the child.
_PS_ENV = {
    "LANG": "C",
    "LC_ALL": "C",
    "TZ": "UTC",
    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
}

_PS_FIELDS = "user=,pid=,ppid=,pgid=,args="

# 解释器型 argv0：只有这些进程才看 argv[1]（脚本路径）做家族识别。
_INTERPRETER_RE = re.compile(
    r"^(?:node|nodejs|bun|deno|python(?:[0-9]+(?:\.[0-9]+)*)?)(?:\.exe)?$",
    re.IGNORECASE)
# 解析含空格的 macOS 可执行路径时最多尝试的空格切分点（有界 stat 次数）。
_MAX_SPACED_ARGV0_PROBES = 16
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
        result = local_process.run(
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


def _run_powershell_json(script: str, timeout: float = POWERSHELL_TIMEOUT_S):
    """Run a fixed local Windows process query and decode its JSON output."""
    if os.name != "nt":
        return None
    executable = (shutil.which("powershell.exe")
                  or shutil.which("powershell") or "powershell.exe")
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    try:
        result = local_process.run(
            [executable, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            cwd=tempfile.gettempdir(),
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if result.returncode != 0:
        return None
    try:
        raw = result.stdout.decode("utf-8", "replace").strip()
        return json.loads(raw) if raw else []
    except (UnicodeError, ValueError, TypeError):
        return None


def _enumerate_windows_process_rows() -> list[ProcRow] | None:
    """Read matching Windows processes through the built-in CIM provider.

    ``Win32_Process.GetOwner`` is a per-process RPC and is unexpectedly slow
    on this host (dozens of calls can exceed the discovery timeout).  Run it
    only after a narrow CLI candidate filter so unrelated Electron/browser
    helpers are neither queried nor treated as agent CLIs.  The owner value
    remains necessary for same-user attachability checks.
    """
    script = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$rows = @(
  Get-CimInstance Win32_Process |
    Where-Object {
      $name = [string]$_.Name
      $cmd = [string]$_.CommandLine
      $direct = $name -match '(?i)^(codex|codex-cli|claude|hermes|pi|zcode|opencode|aider|astrabot|openclaw)(\.exe)?$'
      $interpreter = $name -match '(?i)^(node|python|python3|bun|deno|npm|npx)(\.exe)?$'
      $script_cli = $cmd -match '(?i)(^|[\\/ "])(codex|claude|zcode|hermes|opencode|aider|astrabot|openclaw)(\.c?js|\.py)?([ "]|$)'
      # ZCode's desktop launcher and Electron children share ZCode.exe.  Only
      # keep its CLI forms, whose command line names the bundled zcode.cjs.
      ($direct -and (($name -notmatch '(?i)^zcode(\.exe)?$') -or
        ($cmd -match '(?i)zcode\.cjs'))) -or ($interpreter -and $script_cli)
    } |
    ForEach-Object {
      $user = ''
      try {
        $owner = Invoke-CimMethod -InputObject $_ -MethodName GetOwner
        if ($owner.ReturnValue -eq 0) { $user = [string]$owner.User }
      } catch { }
      $created = ''
      try { $created = $_.CreationDate.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') } catch { }
      [pscustomobject]@{
        User = $user
        ProcessId = [int]$_.ProcessId
        ParentProcessId = [int]$_.ParentProcessId
        ExecutablePath = [string]$_.ExecutablePath
        CommandLine = [string]$_.CommandLine
        Name = [string]$_.Name
        CreationUtc = $created
      }
    }
)
@($rows) | ConvertTo-Json -Compress -Depth 3
'''
    payload = _run_powershell_json(script)
    if payload is None:
        return None
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        return None
    rows: list[ProcRow] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            pid = int(item.get("ProcessId"))
            ppid = int(item.get("ParentProcessId") or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 0:
            continue
        name = str(item.get("Name") or "")
        rows.append(ProcRow(
            user=str(item.get("User") or ""),
            pid=pid,
            pgid=pid,
            exe_path=str(item.get("ExecutablePath") or name),
            cmdline=str(item.get("CommandLine") or name),
            started_at=str(item.get("CreationUtc") or ""),
            ppid=ppid,
            comm=name,
        ))
    return rows


def _iter_lines(text: str) -> Iterator[str]:
    for line in text.splitlines():
        if line.strip():
            yield line


def _lstart_to_iso(tokens: list[str]) -> str:
    """Format UTC C-locale tokens collected with ``_PS_ENV``."""
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


def _argv_tokens(args: str) -> list[str]:
    """按空白切分 argv，但成对引号内的空白不切分（引号本身剥除）。

    WMI/ps 采集的 cmdline 保留引号原样（``node "C:\\Program Files\\x.cjs"``
    之类），朴素 whitespace split 会把含空格路径截断在第一个空格。未闭合
    引号退化为「取到串尾」，与 cmd/POSIX 的宽容解析一致。
    """
    tokens: list[str] = []
    i, n = 0, len(args)
    while i < n:
        while i < n and args[i].isspace():
            i += 1
        if i >= n:
            break
        if args[i] in ('"', "'"):
            close = args.find(args[i], i + 1)
            if close == -1:
                tokens.append(args[i + 1:])
                break
            tokens.append(args[i + 1:close])
            i = close + 1
        else:
            j = i
            while j < n and not args[j].isspace():
                j += 1
            tokens.append(args[i:j])
            i = j
    return tokens


def _argv1(args: str) -> str:
    """Second argv token (script path for interpreter-run CLIs like node)."""
    tokens = _argv_tokens(args) if args else []
    return tokens[1].strip() if len(tokens) > 1 else ""


def _token_basename(token: str) -> str:
    """跨平台 argv token 的 basename：剥引号后按斜杠和反斜杠切分。

    ps/WMI 采集的 cmdline 可能带引号且分隔符与采集机相关（Windows 反斜杠
    + 引号包裹的含空格路径）；本函数不依赖运行平台语义，保证分类结果
    只由 token 内容决定。
    """
    return token.strip('"').replace("\\", "/").rsplit("/", 1)[-1].strip()


_libproc = None


def _darwin_exe_path(pid: int) -> str:
    """macOS: the kernel's full executable path for ``pid`` (``""`` if unknown).

    ``ps -o comm=`` is truncated to 16 chars on Darwin and ``args`` is not
    quoted, so ``proc_pidpath`` is the only reliable exe source there.
    """
    global _libproc
    if not sys.platform.startswith("darwin"):
        return ""
    try:
        import ctypes
        if _libproc is None:
            _libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        buf = ctypes.create_string_buffer(4096)
        size = _libproc.proc_pidpath(int(pid), buf, ctypes.c_uint32(4096))
        if size <= 0:
            return ""
        return buf.value.decode("utf-8", "replace")
    except (OSError, AttributeError, ValueError, TypeError):
        return ""


def _spaced_abs_argv0(args: str) -> str:
    """Resolve an unquoted absolute argv0 that itself contains spaces.

    ``ps`` prints ``/Applications/X.app/.../X Helper (Renderer) --type=...``
    unquoted; try the prefixes ending at the first few spaces and return the
    first one that is an existing regular file.
    """
    if not args.startswith("/"):
        return ""
    index = -1
    for _ in range(_MAX_SPACED_ARGV0_PROBES):
        index = args.find(" ", index + 1)
        if index == -1:
            candidate = args
        else:
            candidate = args[:index]
        try:
            if os.path.isfile(candidate):
                return os.path.realpath(candidate) or candidate
        except (OSError, ValueError):
            return ""
        if index == -1:
            break
    return ""


def _exe_path_for(pid: int, comm: str | None, args: str) -> str:
    argv0 = _argv0(args)
    # 1) /proc/<pid>/exe (Linux) — always canonical when available.
    proc_exe = f"/proc/{pid}/exe"
    if os.path.lexists(proc_exe):
        try:
            return os.path.realpath(proc_exe) or proc_exe
        except OSError:
            pass
    # 1b) macOS kernel path (never the truncated ``comm``).
    darwin = _darwin_exe_path(pid)
    if darwin:
        return darwin
    # 2) absolute argv0 (macOS/Linux launchd-style).
    if argv0.startswith("/"):
        try:
            if os.path.exists(argv0):
                return os.path.realpath(argv0) or argv0
        except OSError:
            pass
        spaced = _spaced_abs_argv0(args)
        if spaced:
            return spaced
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
    if os.name == "nt":
        windows_rows = _enumerate_windows_process_rows()
        _last_enumerate_failed = windows_rows is None
        return windows_rows or []

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
            parts = line.split(None, 4)
            if len(parts) < 5:
                continue
            user, pid_s, ppid_s, pgid_s, args = parts
            pid = int(pid_s)
            pgid = int(pgid_s)
            ppid = int(ppid_s)
        except (ValueError, TypeError, AttributeError):
            continue
        exe_path = _exe_path_for(pid, None, args)
        rows.append(ProcRow(
            user=user,
            pid=pid,
            pgid=pgid,
            exe_path=exe_path,
            cmdline=args,
            started_at=started.get(pid, ""),
            ppid=ppid,
            comm=_token_basename(exe_path) or None,
        ))
    return rows


# ---------------------------------------------------------------------------
# family classification (bounded to OBSERVABLE_AGENT_TYPES)
# ---------------------------------------------------------------------------

def classify_family(exe_path: str, cmdline: str) -> str | None:
    """Map one process to an observable family or ``None``.

    Classification is bounded to ``OBSERVABLE_AGENT_TYPES``; the result is
    an observable family id or ``None``.  The executable basename is
    inspected first (the exact family aliases), then — for interpreter-run
    script CLIs — the second argv token's basename (e.g. ``zcode.cjs``);
    finally the first argv token against the profile's ``generic`` pattern
    (so a helper process such as ``grep codex`` is never misclassified).
    """
    if not exe_path:
        return None
    # Electron/Chromium 子进程（renderer/gpu/utility…）带 ``--type=``：它们是
    # 桌面 App 的 helper，不是 agent CLI 实例。
    if any(token.startswith("--type=")
           for token in _argv_tokens(cmdline or "")):
        return None
    base = _token_basename(exe_path).strip().lower()
    if base.endswith(".exe"):
        base = base[:-4]
    if base:
        for family, aliases in _FAMILY_BASENAMES.items():
            if base in aliases:
                return family
    argv0 = _argv0(cmdline or "").strip().lower()
    # 解释器直跑的脚本型 CLI（argv0 是 node/python 等）：argv[1] 的
    # basename 仍可精确命中家族别名（如 npm/桌面版跑法
    # ``node .../resources/glm/zcode.cjs``）。只检查这一个 token，
    # 不做全命令行扫描——``grep codex`` 之类的 helper 永不误分类。
    argv0_base = _token_basename(_argv_tokens(cmdline or "")[0]
                                 if (cmdline or "").strip() else "")
    interpreted = bool(_INTERPRETER_RE.match(base) or
                       _INTERPRETER_RE.match(argv0_base))
    script_base = (_token_basename(_argv1(cmdline or "")).lower()
                   if interpreted else "")
    if script_base.endswith(".exe"):
        script_base = script_base[:-4]
    if script_base:
        for family, aliases in _FAMILY_BASENAMES.items():
            if script_base in aliases:
                return family
    probe = base or argv0
    if probe:
        if _GENERIC_RE.search(probe):
            return "generic"
    return None


def current_user() -> str:
    """Current local user name (best effort, never raises)."""
    import getpass
    if os.name == "nt":
        # NSSM services run as LocalSystem on this machine while inspecting
        # the interactive user's CLI processes.  The service wrapper sets a
        # bounded identity hint so attachability remains user-scoped.
        hinted = os.environ.get("AGENT_FLEET_LOCAL_USER", "").strip()
        if hinted:
            return hinted[:128]
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

_NATIVE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def _option_token(cmdline: str, flags: tuple[str, ...]) -> str | None:
    tokens = _argv_tokens(cmdline or "")
    found = None
    for index, token in enumerate(tokens):
        for flag in flags:
            if token == flag:
                if index + 1 >= len(tokens):
                    return None
                value = tokens[index + 1]
            elif token.startswith(flag + "="):
                value = token[len(flag) + 1:]
            else:
                continue
            if not _NATIVE_ID_RE.fullmatch(value):
                return None
            if found is not None and value != found:
                return None
            found = value
    return found


def _option_present(cmdline: str, flags: tuple[str, ...]) -> bool:
    tokens = _argv_tokens(cmdline or "")
    return any(token == flag or token.startswith(flag + "=")
               for token in tokens for flag in flags)


def _resume_token(cmdline: str) -> str | None:
    return _option_token(cmdline, ("--resume", "-r"))


def _codex_resume_token(cmdline: str) -> str | None:
    """Extract an explicit Codex native id from either resume spelling."""
    has_flag = _option_present(cmdline, ("--resume", "-r"))
    flagged = _resume_token(cmdline) if has_flag else None
    if has_flag and flagged is None:
        return None
    tokens = _argv_tokens(cmdline or "")
    positional_token = None
    if "resume" in tokens:
        resume_at = tokens.index("resume")
        value_options = {
            "-c", "--config", "--enable", "--disable", "-i", "--image",
            "-m", "--model", "-p", "--profile", "-s", "--sandbox",
            "-C", "--cd", "--add-dir", "-a", "--ask-for-approval",
            "--remote", "--remote-auth-token-env", "--local-provider",
            "--thread-source", "--output-schema", "-o", "--output-last-message",
            "--resume", "-r",
        }
        positional = []
        skip_value = False
        for token in tokens[resume_at + 1:]:
            if skip_value:
                skip_value = False
                continue
            if token in value_options:
                skip_value = True
            elif token.startswith("-") and token != "-":
                continue
            else:
                positional.append(token)
        if not positional or not _NATIVE_ID_RE.fullmatch(positional[0]):
            return None
        positional_token = positional[0]
    if flagged and positional_token and flagged != positional_token:
        return None
    return flagged or positional_token


def _unique_native_named(base_dir: str, filename: str, *, depth: int,
                         codex_rollout: bool = False) -> str | None:
    """Return one transcript, counting legacy and rollout matches together.

    按已知目录布局直接定位（每层一个 ``*`` 通配的 glob），不再整树扫描并在
    512 个文件处静默失败——``~/.claude/projects`` / ``~/.codex/sessions``
    在真实机器上轻易超过该数量。多个匹配仍按歧义返回 ``None``。
    """
    if not base_dir or not os.path.isdir(base_dir):
        return None
    if not _NATIVE_ID_RE.fullmatch(filename):
        return None
    names = [glob.escape(filename)]
    if codex_rollout:
        names.append("rollout-*-" + glob.escape(filename))
    root = glob.escape(base_dir)
    found = None
    try:
        for level in range(depth + 1):
            for name in names:
                pattern = os.path.join(root, *(["*"] * level), name)
                for path in glob.iglob(pattern):
                    if os.path.islink(path) or not os.path.isfile(path):
                        continue
                    if found is not None and os.path.normcase(path) != os.path.normcase(found):
                        return None
                    found = path
    except (OSError, ValueError):
        return None
    return found


def _codex_native_path(cmdline: str) -> str | None:
    resume = _codex_resume_token(cmdline)
    base = os.path.expanduser("~/.codex/sessions")
    if not resume:
        return None
    rollout = _unique_native_named(base, f"{resume}.jsonl", depth=3,
                                   codex_rollout=True)
    if rollout is None:
        return None
    if os.path.normcase(os.path.basename(rollout)) == os.path.normcase(f"{resume}.jsonl"):
        return rollout
    try:
        with open(rollout, "r", encoding="utf-8") as stream:
            # 真实 Codex rollout 首行 session_meta（含 base_instructions）常达
            # 20KB+，16KB 上限会截断成非法 JSON，导致 resume 绑定永远失败。
            first = stream.readline(1 << 20)
        metadata = json.loads(first)
        if metadata.get("type") == "session_meta" and metadata.get("payload", {}).get("id") == resume:
            return rollout
    except (OSError, UnicodeError, ValueError, AttributeError):
        pass
    return None


def _claude_native_path(cmdline: str) -> str | None:
    session_id = _option_token(cmdline, ("--session-id", "--resume", "-r"))
    if not session_id:
        return None
    base = os.path.expanduser("~/.claude/projects")
    return _unique_native_named(base, f"{session_id}.jsonl", depth=2)


def _option_value(cmdline: str, flags: tuple[str, ...]) -> str | None:
    """Read one unvalidated option value, failing closed on ambiguity."""
    tokens = _argv_tokens(cmdline or "")
    found = None
    for index, token in enumerate(tokens):
        value = None
        for flag in flags:
            if token == flag:
                if index + 1 >= len(tokens):
                    return None
                value = tokens[index + 1]
            elif token.startswith(flag + "="):
                value = token[len(flag) + 1:]
            if value is not None:
                break
        if value is None:
            continue
        if not value or (found is not None and value != found):
            return None
        found = value
    return found


def _native_path_under(base_dir: str, value: str) -> str | None:
    """Accept an explicit transcript path only inside its configured root."""
    if not value:
        return None
    try:
        base = os.path.realpath(os.path.abspath(base_dir))
        candidate = os.path.realpath(os.path.abspath(os.path.expanduser(value)))
        if os.path.commonpath((base, candidate)) != base:
            return None
        if not os.path.isfile(candidate) or not candidate.lower().endswith(".jsonl"):
            return None
        return candidate
    except (OSError, TypeError, ValueError):
        return None


def _pi_native_path(cmdline: str) -> str | None:
    """Resolve Pi's explicitly selected session, never the newest file."""
    base = os.path.expanduser("~/.pi/agent/sessions")
    has_path = _option_present(cmdline, ("--session",))
    has_id = _option_present(cmdline, ("--session-id",))
    if has_path and has_id:
        return None
    path_value = _option_value(cmdline, ("--session",)) if has_path else None
    if has_path:
        if not path_value:
            return None
        exact = _native_path_under(base, path_value)
        if exact:
            return exact
        if _NATIVE_ID_RE.fullmatch(path_value):
            return _unique_native_named(base, f"{path_value}.jsonl", depth=2)
        return None
    session_id = _option_token(cmdline, ("--session-id",))
    if not session_id:
        return None
    return _unique_native_named(base, f"{session_id}.jsonl", depth=2)


# Family-specific native path resolvers above.  A profile-declared storage
# root is not an identity proof: families without an explicit resolver remain
# unassociated until their process exposes a stable session selector.
_PROFILE_NATIVE_RESOLVERS = {
    "codex": _codex_native_path,
    "claude_code": _claude_native_path,
    "pi": _pi_native_path,
}


def native_path_for(row: ProcRow) -> str | None:
    """Best-effort native transcript path for a known agent family.

    Read-only and bounded; returns ``None`` when it cannot be determined.
    Dedicated resolvers keep each family-specific identity contract; families
    without one remain unassociated rather than selecting by file recency.
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
    return None


def can_attach(row: ProcRow, native: str | None) -> bool:
    """Attachability: same-user and (if a native path is offered) readability."""
    try:
        if (row.user or "").casefold() != current_user().casefold():
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
