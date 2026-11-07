"""tools/client_update/services.py — 发现并操控本机 agent-fleet 常驻服务。

按 ProgramArguments / ExecStart 里引用的入口脚本识别角色，不依赖 label 命名：

* macOS：``~/Library/LaunchAgents/*.plist``，``launchctl print/kickstart``；
* Linux：``~/.config/systemd/user/*.service``，``systemctl --user``。

所有外部命令经 ``run`` 注入，便于测试；永不 ``kill``、永不 ``kickstart -k``——
旧进程只通过 drain 文件自愿退出。
"""
from __future__ import annotations

import os
import plistlib
import re
import shlex
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from fleet_core.process import local_process

SCRIPT_ROLES = {
    "agent-self-report.py": "probe",
    "agent-runner.py": "runner",
    "control_client_cli.py": "control-client",
}

Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _default_run(cmd: list[str]) -> subprocess.CompletedProcess:
    return local_process.run(cmd, capture_output=True, text=True, timeout=30, check=False)


@dataclass
class Service:
    role: str
    kind: str  # launchd | systemd
    label: str
    unit_path: Path
    script: Path
    args: list[str] = field(default_factory=list)

    def code_root(self) -> Path:
        """脚本所在代码树根（tools/ 的上一级，或 tools/supervisor/ 的上两级）。"""
        parts = self.script.parts
        if "tools" in parts:
            idx = len(parts) - 1 - parts[::-1].index("tools")
            return Path(*parts[:idx])
        return self.script.parent

    def relative_script(self) -> Path:
        return self.script.relative_to(self.code_root())

    def uses(self, current: Path) -> bool:
        """服务是否经 ``current`` 符号链接启动（未解析路径比较）。"""
        return self.code_root() == current

    def to_dict(self) -> dict:
        return {"role": self.role, "kind": self.kind, "label": self.label,
                "unit": str(self.unit_path), "script": str(self.script)}


def _role_of(args: list[str]) -> tuple[str, Path] | None:
    for arg in args:
        name = Path(arg).name
        if name in SCRIPT_ROLES:
            return SCRIPT_ROLES[name], Path(arg)
    return None


def launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def systemd_user_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def discover(*, launch_dir: Path | None = None, systemd_dir: Path | None = None) -> list[Service]:
    found: list[Service] = []
    launch_dir = launch_dir or launch_agents_dir()
    if launch_dir.is_dir():
        for plist in sorted(launch_dir.glob("*.plist")):
            try:
                data = plistlib.loads(plist.read_bytes())
            except Exception:
                continue
            args = [str(a) for a in data.get("ProgramArguments") or []]
            hit = _role_of(args)
            if hit and isinstance(data.get("Label"), str):
                found.append(Service(hit[0], "launchd", data["Label"], plist, hit[1], args))
    systemd_dir = systemd_dir or systemd_user_dir()
    if systemd_dir.is_dir():
        for unit in sorted(systemd_dir.glob("*.service")):
            try:
                text = unit.read_text(encoding="utf-8")
            except OSError:
                continue
            match = re.search(r"^ExecStart=(.+)$", text, re.MULTILINE)
            if not match:
                continue
            args = shlex.split(match.group(1))
            hit = _role_of(args)
            if hit:
                found.append(Service(hit[0], "systemd", unit.name, unit, hit[1], args))
    return found


def _domain() -> str:
    return f"gui/{os.getuid()}"


def kickstart(service: Service, run: Runner = _default_run) -> bool:
    """确保服务在跑（不强杀）；KeepAlive 服务退出后由 launchd 自己拉起。"""
    if service.kind == "launchd":
        result = run(["launchctl", "kickstart", f"{_domain()}/{service.label}"])
    else:
        result = run(["systemctl", "--user", "start", service.label])
    return result.returncode == 0


def loaded_pid(service: Service, run: Runner = _default_run) -> int | None:
    if service.kind == "launchd":
        result = run(["launchctl", "print", f"{_domain()}/{service.label}"])
        match = re.search(r"^\s*pid = (\d+)", result.stdout or "", re.MULTILINE)
    else:
        result = run(["systemctl", "--user", "show", "-p", "MainPID", "--value", service.label])
        match = re.search(r"^(\d+)", (result.stdout or "").strip())
    if result.returncode != 0 or not match:
        return None
    pid = int(match.group(1))
    return pid or None


def migrate(service: Service, current: Path, *, stamp: str | None = None,
            run: Runner = _default_run) -> Path | None:
    """把服务入口改写到 ``current/<相对脚本>``；返回备份路径，已迁移返回 None。

    只改写定义文件（先备份、再校验）；生效需要重新加载服务，由调用方显式决定。
    """
    if service.uses(current):
        return None
    target = current / service.relative_script()
    stamp = stamp or time.strftime("%Y%m%d")
    backup = service.unit_path.with_name(
        f"{service.unit_path.name}.agent-fleet-backup-{stamp}-r7")
    if not backup.exists():
        shutil.copy2(service.unit_path, backup)
    if service.kind == "launchd":
        data = plistlib.loads(service.unit_path.read_bytes())
        data["ProgramArguments"] = [
            str(target) if Path(a) == service.script else a
            for a in data["ProgramArguments"]]
        tmp = service.unit_path.with_name(service.unit_path.name + ".tmp")
        tmp.write_bytes(plistlib.dumps(data))
        if shutil.which("plutil"):
            lint = run(["plutil", "-lint", str(tmp)])
            if lint.returncode != 0:
                tmp.unlink()
                raise RuntimeError(f"plutil -lint 失败: {service.unit_path.name}")
        os.replace(tmp, service.unit_path)
    else:
        text = service.unit_path.read_text(encoding="utf-8")
        tmp = service.unit_path.with_name(service.unit_path.name + ".tmp")
        tmp.write_text(text.replace(str(service.script), str(target)), encoding="utf-8")
        os.replace(tmp, service.unit_path)
    return backup


def reload(service: Service, run: Runner = _default_run, *,
           sleep: Callable[[float], None] = time.sleep, wait_s: float = 30.0) -> bool:
    """重新加载定义（会重启该服务）。仅 ``migrate-layout --reload`` 使用。

    ``bootout`` 是异步的：旧进程退出前立即 ``bootstrap`` 会失败（EIO），留下服务
    处于未加载状态。故先轮询到 label 消失，再带重试 bootstrap。
    """
    if service.kind == "launchd":
        target = f"{_domain()}/{service.label}"
        run(["launchctl", "bootout", target])
        waited = 0.0
        while waited < wait_s and run(["launchctl", "print", target]).returncode == 0:
            sleep(0.5)
            waited += 0.5
        for attempt in range(3):
            result = run(["launchctl", "bootstrap", _domain(), str(service.unit_path)])
            if result.returncode == 0:
                break
            sleep(1.0 + attempt)
    else:
        run(["systemctl", "--user", "daemon-reload"])
        result = run(["systemctl", "--user", "restart", service.label])
    return result.returncode == 0
