"""tools/supervisor/native_resume.py — 原生会话续接（native resume）。

从 ``supervisor.py`` 拆出：family 续接能力集合（派生自 ``agent_profiles``）、
私有续接身份（cwd/env/token/exe）的提取、family resume argv 构造，以及
``Supervisor.append_user_turn`` 的前置判定（``NativeResumeMixin``）。
"""
from __future__ import annotations

import os
import json
import subprocess
from pathlib import Path
from typing import Mapping

from agent_profiles import (NATIVE_RESUME_AGENT_TYPES, PROFILES,
                            RESUME_VERIFIABLE_AGENT_TYPES,
                            SESSION_PATH_RESUME_AGENT_TYPES)
from fleet_core.process.local_process import run as run_local
from tools.supervisor.group_ops import validate_command
from tools.supervisor.handles import _ManagedEntry, _stamp_private_identity
from tools.supervisor.model import PAUSED, QUARANTINED, TERMINAL_STATES

# Per-family native resume capabilities, derived from agent_profiles (the
# single source of truth).  Native: a family resume argv exists and attach with
# a native path stamps ``resume``.  Verifiable: resume may be proven by probing
# the attached exe.  Session-path: the resume token is the native file path.
_NATIVE_RESUME_FAMILIES = frozenset(NATIVE_RESUME_AGENT_TYPES)
_RESUME_VERIFIABLE_FAMILIES = frozenset(RESUME_VERIFIABLE_AGENT_TYPES)
_SESSION_PATH_RESUME_FAMILIES = frozenset(SESSION_PATH_RESUME_AGENT_TYPES)


def _live_process_cwd(pid: int) -> str | None:
    """Best-effort live cwd for an attached pid.  Never opens files.

    Linux uses ``/proc/<pid>/cwd``; macOS falls back to ``lsof -Fn``.
    ``/tmp`` is not resume identity.  Fail closed on any error.
    """
    try:
        pid_i = int(pid)
    except (TypeError, ValueError):
        return None
    if pid_i <= 0:
        return None
    try:
        path = os.readlink(f"/proc/{pid_i}/cwd")
        if isinstance(path, str) and path and path != "/tmp" and os.path.isdir(path):
            return path
    except OSError:
        pass
    lsof = next((p for p in ("/usr/sbin/lsof", "/usr/bin/lsof", "/bin/lsof")
                 if os.path.isfile(p)), None)
    if lsof is None:
        return None
    try:
        completed = run_local(
            [lsof, "-a", "-p", str(pid_i), "-d", "cwd", "-Fn"],
            capture_output=True, text=True, timeout=2,
            stdin=subprocess.DEVNULL, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in (completed.stdout or "").splitlines():
        if not line.startswith("n"):
            continue
        path = line[1:].strip()
        if path and path != "/tmp" and os.path.isdir(path):
            return path
    return None


def _resume_token_from_native_path(path: str | None) -> str | None:
    """Read the exact transcript identity; verify rollout metadata against its filename."""
    if not isinstance(path, str) or not path:
        return None
    stem = Path(path).stem
    if stem.startswith("rollout-"):
        try:
            with open(path, encoding='utf-8') as stream:
                first = json.loads(stream.readline(1 << 20))
            token = first.get('payload', {}).get('id') if first.get('type') == 'session_meta' else None
            return token if isinstance(token, str) and token and stem.endswith('-' + token) else None
        except (OSError, ValueError, AttributeError):
            return None
    if not stem:
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
            if val or (name == "env" and isinstance(val, Mapping)):
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


_RESUME_FORBIDDEN_FLAGS = frozenset({"--ephemeral", "--ignore-user-config", "--no-session-persistence"})


def continuation_options(family: str, command: list[str]) -> list[str]:
    """Keep explicit provider/model/policy options, without replaying the prompt.

    These are options supported by the family's resume command. Output paths,
    session selectors and initial-turn attachments must not be replayed.
    """
    valued = {
        'codex': {'-c', '--config', '-m', '--model', '--enable', '--disable'},
        'claude_code': {'--model', '--fallback-model', '--settings', '--setting-sources',
                        '--permission-mode', '--effort', '--agent', '--agents',
                        '--system-prompt', '--append-system-prompt', '--mcp-config'},
    }.get(family, set())
    switches = {
        'codex': {'--dangerously-bypass-approvals-and-sandbox', '--skip-git-repo-check',
                  '--strict-config', '--ignore-rules', '--approve-for-me'},
        'claude_code': {'--dangerously-skip-permissions', '--bare', '--strict-mcp-config'},
    }.get(family, set())
    result = []
    args = iter(command[1:])
    for arg in args:
        if arg == '--':
            break
        flag = arg.split('=', 1)[0]
        if flag in valued:
            if '=' in arg:
                result.append(arg)
            else:
                value = next(args, None)
                if value is not None:
                    result.extend((arg, value))
        elif arg in switches:
            result.append(arg)
    return result


def _native_resume_argv(family: str, exe: str, token: str, text: str) -> list[str]:
    """Build the family-specific native resume argv."""
    if not exe or not token or not isinstance(text, str) or not text:
        return []
    # token 以 '-' 开头会被当作选项；text 以 '-' 开头时只有支持 '--' 的
    # CLI（clap / commander）插 '--'，其余拒绝。
    if str(token).startswith("-"):
        return []
    leading_dash = text.startswith("-")
    if family == "codex":
        argv = [exe, "exec", "resume", "--json", token] + (["--", text] if leading_dash else [text])
    elif family == "claude_code":
        argv = [exe, "--print", "--verbose", "--output-format", "stream-json", "--resume", token] + (["--", text] if leading_dash else [text])
    elif family == "pi":
        if leading_dash:
            return []
        # --resume is an interactive picker; --session takes path or id.
        argv = [exe, "--session", token, text]
    else:
        return []
    if any(flag in argv for flag in _RESUME_FORBIDDEN_FLAGS):
        return []
    return argv


class NativeResumeMixin:
    """``Supervisor`` 的续接判定：能力证明与续接 argv/身份准备。"""

    def _resume_allowed(self, m, session_id: str) -> bool:
        """True only when the family has a proven resume capability.

        An explicit ``capability_manifest['resume']`` wins.  Otherwise a
        verifiable family (codex / claude_code) may be probed from the
        attached exe; Hermes / missing exe stay False.  ``pi`` is claimed
        only when attach already stamped a native session file (see
        ``attach_to_existing``).
        """
        caps = getattr(m, "capability_manifest", None)
        if not isinstance(caps, Mapping):
            caps = {}
        if "resume" in caps:
            return bool(caps.get("resume"))
        family = str(getattr(m, "agent", "") or "")
        if family not in _RESUME_VERIFIABLE_FAMILIES:
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
            # probe keys families by CLI name (claude_code -> claude).
            probe_family = PROFILES[family].probe_family or family
            manifest = probe_agent(probe_family, [exe], verify_resume=True)
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

    def _prepare_user_turn(self, session_id: str, text: str,
                           entry: _ManagedEntry):
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
        m = entry.manifest
        if entry.handle is None:
            disk = self._read_disk_manifest(session_id)
            if disk is not None:
                m.state, m.reason = disk.state, disk.reason
            if self._continuations.path(session_id).exists() and not self._continuations.restore(entry):
                return 'model_config_unavailable'
        if m.state in TERMINAL_STATES and m.reason != "completed":
            return m.state
        gate = self._guard_adopted(session_id)
        if gate:
            return gate
        family = str(getattr(m, "agent", "") or "")
        if family == "hermes":
            return "unsupported_action"
        if family not in _NATIVE_RESUME_FAMILIES:
            return "unsupported_action"
        if not self._resume_allowed(m, session_id):
            return "resume_unverified"
        if not isinstance(text, str) or not text:
            return "control_failed"
        if m.state in (PAUSED, QUARANTINED):
            return "unsupported_action"
        identity = _native_resume_identity(entry)
        if identity is None:
            if m.state in TERMINAL_STATES:
                return m.state
            # Do not call GroupOps.create.  Missing native identity is a
            # sibling spawn, not resume.
            return "unsupported_action"
        cwd, env, token, exe = identity
        from tools.supervisor.model_config import effective_model_config
        try:
            options, env = effective_model_config(entry, self._continuations.root)
        except (ValueError, OSError, TypeError):
            return 'model_config_unavailable'
        argv = _native_resume_argv(family, exe, token, text)
        if argv:
            at = 2 if family == 'codex' else 1
            argv[at:at] = options
        if not validate_command(argv) or not cwd:
            return "unsupported_action"
        return m, entry.handle, argv, cwd, env

    def configure_model(self, session_id, config):
        """Atomically bind a model to a finished managed native conversation."""
        from dataclasses import replace
        from fleet_core.session_models import AGENT_PROTOCOLS, validate_model_delivery
        from tools.supervisor.model_config import codex_provider, original_model, _claude_settings
        self.get(session_id)
        entry = self._entries[session_id]
        with entry.control_lock:
            if entry.resume_pending is not None:
                return 'session_busy'
            m = entry.manifest
            if m.capability_manifest.get('desktop_bridge') or m.agent not in AGENT_PROTOCOLS:
                return 'unsupported_action'
            if m.state != 'terminated' or m.reason != 'completed':
                return 'session_busy'
            if not self.continuation_ready(session_id):
                return 'native_session_unbound'
            try:
                value = validate_model_delivery(config, m.agent)
                if value['mode'] == 'catalog':
                    if m.agent == 'codex':
                        codex_provider(entry)
                    else:
                        _claude_settings(entry.resume_options, entry.cwd)
                baseline = entry.restore_model if entry.model_config is not None else original_model(entry)
                if value['mode'] == 'original' and entry.model_config is not None and not baseline:
                    return 'model_original_unavailable'
                candidate = replace(entry, model_config=None if value['mode'] == 'original' else value,
                                    restore_model=baseline)
                if not self._continuations.save(candidate):
                    return 'model_config_unavailable'
            except ValueError as exc:
                return str(exc) if str(exc) in {'native_provider_unbound', 'native_settings_unreadable'} else 'model_config_unavailable'
            except (OSError, TypeError):
                return 'model_config_unavailable'
            entry.model_config = candidate.model_config
            entry.restore_model = candidate.restore_model
            return 'model_restored' if value['mode'] == 'original' else 'model_configured'

    def continuation_ready(self, session_id: str) -> bool:
        self.get(session_id)
        entry = self._entries[session_id]
        return bool(self._continuations.restore(entry)
                    and entry.manifest.capability_manifest.get('resume'))

    def _capture_native_identity(self, entry, line: str) -> None:
        if not entry.persist_continuation:
            return
        try:
            row = json.loads(line)
            token = None
            if entry.manifest.agent == 'codex' and row.get('type') == 'thread.started':
                token = row.get('thread_id')
            elif entry.manifest.agent == 'claude_code' and row.get('type') in {'system', 'result'}:
                token = row.get('session_id')
            if not isinstance(token, str) or not token or token.startswith('-'):
                return
            entry.resume_token = token
            if self._continuations.save(entry):
                entry.manifest.capability_manifest = {**entry.manifest.capability_manifest, 'resume': True}
                self._persist(entry.manifest)
        except (OSError, ValueError, TypeError, AttributeError):
            return
