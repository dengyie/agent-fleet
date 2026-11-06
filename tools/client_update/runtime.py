"""tools/client_update/runtime.py — 常驻进程侧的客户端版本感知（零网络、零第三方依赖）。

* :func:`client_info` / :func:`client_headers`：本进程代码树的版本，进出站请求头。
* :func:`announce`：启动回执 ``state/runtime/<role>.json``，updater 据此确认新版本已接管。
* :func:`drain_requested`：updater 切换 ``current`` 后写 ``state/drain/<role>.json``；
  旧版本进程在两轮之间看到它就自行退出，由 launchd/systemd KeepAlive 以新代码拉起。
"""
from __future__ import annotations

import json
import os
import secrets
import time
from functools import cache
from pathlib import Path
from typing import Any

from fleet_core import client_release as cr
from fleet_core.identity import ProcessIdentity

ROLES = ("probe", "runner", "control-client")
CODE_ROOT = Path(__file__).resolve().parents[2]
LEGACY_VERSION = "legacy"


def client_home() -> Path:
    override = os.environ.get("AGENT_FLEET_CLIENT_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share" / "agent-fleet" / "client"


@cache
def client_info(root: Path = CODE_ROOT) -> dict[str, Any]:
    """``{version, sequence, channel, managed}``；非 updater 安装的旧树为 legacy/0。"""
    info = cr.read_release_info(root)
    if info is None:
        return {"version": LEGACY_VERSION, "sequence": 0,
                "channel": cr.DEFAULT_CHANNEL, "managed": False}
    return {"version": info["version"], "sequence": info["sequence"],
            "channel": info["channel"], "managed": True}


def client_headers(role: str) -> dict[str, str]:
    info = client_info()
    return {
        "User-Agent": f"agent-fleet-{role}/{info['version']}",
        "X-Fleet-Client": (f"version={info['version']}; seq={info['sequence']}; "
                           f"channel={info['channel']}"),
    }


def report_fields() -> dict[str, Any]:
    """probe 上报里的 ``client`` 字段（hub 侧 sanitize_client 白名单）。"""
    info = client_info()
    return {"version": info["version"], "sequence": info["sequence"],
            "channel": info["channel"]}


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def receipt_path(role: str, home: Path | None = None) -> Path:
    return (home or client_home()) / "state" / "runtime" / f"{role}.json"


def drain_path(role: str, home: Path | None = None) -> Path:
    return (home or client_home()) / "state" / "drain" / f"{role}.json"


def announce(role: str) -> None:
    """写启动回执；失败只影响 updater 的确认，不影响业务。"""
    info = client_info()
    try:
        _write_json(receipt_path(role), {
            "role": role, "version": info["version"], "sequence": info["sequence"],
            "managed": info["managed"], "startedAt": int(time.time()),
            "receiptId": secrets.token_hex(16),
            **ProcessIdentity.current().to_dict(),
        })
    except OSError:
        pass


def drain_requested(role: str) -> bool:
    """本进程是否应退出让位新版本。

    只对 updater 管理的树生效（代码根位于 ``client_home/releases``），且仅当
    ``current`` 确实已指向目标版本——否则 KeepAlive 拉起的仍是旧代码，会形成
    退出循环。
    """
    home = client_home()
    info = client_info()
    if not info["managed"] or CODE_ROOT.parent != (home / "releases").resolve():
        return False
    request = read_json(drain_path(role, home))
    target = request.get("targetVersion") if request else None
    if not isinstance(target, str) or target == info["version"]:
        return False
    try:
        return (home / "current").resolve().name == target
    except OSError:
        return False
