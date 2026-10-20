"""Launch policy for every console child process the fleet spawns.

Two Windows-only failure modes are decided here, once, instead of at each
call site:

* **Visible flash windows.** A GUI-subsystem parent (``pythonw.exe``, a
  scheduled task without a console) gives every console child it spawns a new
  *visible* ``conhost`` window unless the child is created with
  ``CREATE_NO_WINDOW``. The child still inherits a console, it is just not
  shown. On POSIX the flag does not exist and the value is ``0`` (accepted
  and ignored).
* **Decode crashes.** ``text=True`` without ``errors="replace"`` turns any
  non-UTF-8 byte a child prints into a ``UnicodeDecodeError`` raised inside
  the reader thread; ``stdout`` then stays ``None`` and the caller silently
  loses the whole output. Text-mode spawns therefore default to
  ``errors="replace"``. ``errors`` is only valid in text mode, so binary
  spawns are left untouched.

Import :func:`run` / :func:`popen` in preference to ``subprocess.run`` /
``subprocess.Popen`` for anything that may execute on Windows;
``tests/test_local_process.py`` enforces this with an AST guard.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from functools import lru_cache

# pythonw（无控制台）spawn 控制台子进程时会新建可见 conhost 闪窗；隐藏之。
# POSIX 无此语义，置 0（传给 subprocess 会被接受并忽略）。
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

# Directories that hold the Windows ``bash.exe`` *shim* rather than a real
# shell: ``System32`` hosts the WSL launcher (exits 1 with a UTF-16 "not
# installed" notice) and ``WindowsApps`` hosts Microsoft Store redirectors.
_STUB_DIR_PARTS = ("windows\\system32", "windowsapps")


def _launch_policy(kwargs):
    """Return ``kwargs`` with the hidden-window flag and lenient decode applied."""
    options = dict(kwargs)
    options["creationflags"] = int(options.get("creationflags", 0)) | NO_WINDOW
    text_mode = bool(
        options.get("text")
        or options.get("encoding")
        or options.get("universal_newlines")
    )
    if text_mode:
        options.setdefault("errors", "replace")
    return options


def run(argv, **kwargs):
    """``subprocess.run`` under the fleet launch policy."""
    return subprocess.run(argv, **_launch_policy(kwargs))


def popen(argv, **kwargs):
    """``subprocess.Popen`` under the fleet launch policy."""
    return subprocess.Popen(argv, **_launch_policy(kwargs))


def _is_stub_dir(path: str) -> bool:
    normalized = os.path.normcase(os.path.abspath(path))
    return any(part in normalized for part in _STUB_DIR_PARTS)


def _non_stub_path() -> str:
    entries = [
        entry
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if entry and not _is_stub_dir(entry)
    ]
    return os.pathsep.join(entries)


@lru_cache(maxsize=8)
def _resolve_bash_cached(override: str | None) -> str | None:
    """Locate a *real* POSIX shell on Windows, skipping the WSL/Store shims."""
    if os.name != "nt":
        return "bash"
    # An override is still validated: pointing it at the WSL launcher would
    # reproduce the exact silent-empty-output failure this module exists to
    # prevent, so a stub path is ignored in favour of normal discovery.
    if override and os.path.isfile(override) and not _is_stub_dir(override):
        return override
    candidates = [shutil.which("bash", path=_non_stub_path())]
    git = shutil.which("git", path=_non_stub_path())
    if git:
        git_root = os.path.dirname(os.path.dirname(git))
        candidates.append(os.path.join(git_root, "bin", "bash.exe"))
        candidates.append(os.path.join(git_root, "usr", "bin", "bash.exe"))
    for root in (
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("LOCALAPPDATA"),
    ):
        if root:
            candidates.append(os.path.join(root, "Git", "bin", "bash.exe"))
            candidates.append(os.path.join(root, "Programs", "Git", "bin", "bash.exe"))
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and not _is_stub_dir(candidate):
            return candidate
    return None


def resolve_bash() -> str | None:
    """Absolute path of a usable ``bash``, or ``None`` when none exists.

    On POSIX this is ``"bash"`` (resolved by the OS at spawn time). On Windows
    the ``System32``/``WindowsApps`` shims are rejected because they are not
    real shells — including when named by ``AGENT_FLEET_BASH``. Results are
    memoised on that override, so changing the variable invalidates the cache
    naturally and repeated calls never re-scan ``PATH`` or the filesystem.
    """
    return _resolve_bash_cached(os.environ.get("AGENT_FLEET_BASH"))


resolve_bash.cache_clear = _resolve_bash_cached.cache_clear
