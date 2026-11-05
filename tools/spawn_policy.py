"""tools/spawn_policy.py — agent / git 子进程的统一启动策略。

两条执行路径（legacy ``adapters.base`` 与 managed ``supervisor``）共用这里：

* ``build_child_env``：显式白名单环境。runner 自身的 hub 凭据
  （``AGENT_FLEET_*``、``*_TOKEN`` 等）永不继承给 agent；adapter 可声明
  额外放行键（例如 CLI 自己的 API key）。
* ``place_instruction``：把不可信 instruction 放到 argv 的位置参数上。
  以 ``-`` 开头时只有已知支持 ``--`` 的 CLI 会插入 ``--``，否则拒绝，
  避免 ``--help`` / ``--config=...`` 被当作选项解析。
* ``refuse_windows_batch``：Windows 上 ``.cmd`` / ``.bat`` 目标经 cmd.exe
  二次解析（BatBadBut），含 cmd 元字符的不可信文本一律拒绝。
"""
from __future__ import annotations

import os
import re
import shutil
from typing import Iterable, Mapping, Sequence

# 只放行与身份/locale/路径/代理/证书相关的通用键；任何应用凭据都不在其中。
SAFE_ENV_KEYS = (
    "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
    "PATH", "TMPDIR", "TEMP", "TMP", "TERM", "COLORTERM", "TZ",
    "CI", "GITHUB_ACTIONS",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME",
    "XDG_RUNTIME_DIR",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
    "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "SYSTEMROOT",
    "WINDIR", "ComSpec", "PATHEXT",
)

#: runner 自身的凭据前缀：即便 adapter / runner.yaml 声明也不放行。
_FORBIDDEN_PREFIXES = ("AGENT_FLEET_",)
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")

#: cmd.exe 会解释的字符（引号逃逸、变量展开、管道/重定向、换行）。
_CMD_META = frozenset('"%!^&|<>()\r\n')


class UnsafeInstruction(ValueError):
    """instruction 无法安全放入 argv；message 为固定短码。"""


def env_key_allowed(key: str) -> bool:
    return (isinstance(key, str) and bool(ENV_KEY_RE.fullmatch(key))
            and not key.upper().startswith(_FORBIDDEN_PREFIXES))


def build_child_env(extra_keys: Iterable[str] = (),
                    source: Mapping[str, str] | None = None) -> dict[str, str]:
    """白名单环境：SAFE_ENV_KEYS + 合法的 extra_keys，缺省值补齐。"""
    source = os.environ if source is None else source
    keys = list(SAFE_ENV_KEYS)
    keys.extend(k for k in extra_keys if env_key_allowed(k) and k not in keys)
    env = {k: source[k] for k in keys if source.get(k) is not None}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")
    env.setdefault("LANG", "C.UTF-8")
    if os.name == "nt":
        env.setdefault("HOME", source.get("USERPROFILE") or os.path.expanduser("~"))
        env.setdefault("USERPROFILE", os.path.expanduser("~"))
        env.setdefault("SYSTEMROOT", source.get("SystemRoot") or r"C:\Windows")
        env.setdefault("ComSpec", source.get("ComSpec") or r"C:\Windows\System32\cmd.exe")
    else:
        env.setdefault("HOME", source.get("HOME") or os.path.expanduser("~"))
    return env


def place_instruction(template: Sequence[str], instruction: str, *,
                      end_of_options: bool) -> list[str]:
    """把 instruction 作为单个位置参数插入模板。

    ``{instruction}`` 是插值点，缺省追加到末尾。instruction 以 ``-`` 开头时：
    ``end_of_options`` 为真则在其前插 ``--``，否则抛 :class:`UnsafeInstruction`。
    """
    instruction = str(instruction)
    leading_dash = instruction.startswith("-")
    if leading_dash and not end_of_options:
        raise UnsafeInstruction("instruction_leading_dash")
    value = ["--", instruction] if leading_dash else [instruction]
    argv = list(template)
    if "{instruction}" in argv:
        out: list[str] = []
        for c in argv:
            out.extend(value if c == "{instruction}" else [c])
        return out
    return argv + value


def refuse_windows_batch(argv: Sequence[str], untrusted: str,
                         env: Mapping[str, str] | None = None) -> None:
    """Windows：``.cmd``/``.bat`` 目标 + 含 cmd 元字符的不可信文本 → 拒绝。"""
    if os.name != "nt" or not argv:
        return
    exe = str(argv[0])
    path_env = (env or {}).get("PATH") if env else None
    resolved = shutil.which(exe, path=path_env) or exe
    if os.path.splitext(resolved)[1].lower() not in (".cmd", ".bat"):
        return
    if any(ch in _CMD_META for ch in str(untrusted)):
        raise UnsafeInstruction("windows_batch_unsafe")


__all__ = [
    "SAFE_ENV_KEYS",
    "UnsafeInstruction",
    "build_child_env",
    "env_key_allowed",
    "place_instruction",
    "refuse_windows_batch",
]
