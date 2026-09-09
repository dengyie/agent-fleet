"""tools/runner_config.py — agent-runner 配置加载与校验

配置位于 ~/.config/agent-fleet/runner.yaml（不进 git）。
项目白名单 + adapter 命令模板都在这里定义，页面无法越权。
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from agent_profiles import PROFILES

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "agent-fleet" / "runner.yaml"
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "agent-fleet"


class ConfigError(Exception):
    pass


@dataclass
class RunnerConfig:
    hub: str
    machine: str
    credential: str
    runner_id: str
    projects: dict  # name -> Path（绝对、已存在）
    agents: dict    # agent_type -> {"command": [..], "timeout_s": int}
    poll_interval_s: int = 15
    heartbeat_interval_s: int = 30
    cache_dir: Path = field(default=DEFAULT_CACHE_DIR)
    # ---- additive managed-session settings (Task 8) ---------------------
    # ``supervisor_enabled`` is opt-in machine config.  When False (the
    # default) the runner keeps the legacy BaseAdapter spawn/heartbeat path
    # byte-for-byte unchanged.
    supervisor_enabled: bool = False
    supervisor_manifest_dir: Path | None = None
    supervisor_credential: str | None = None
    supervisor_public_key: bytes | None = None

    @property
    def managed_enabled(self) -> bool:
        """Opt-in managed mode: enabled flag + a durable manifest dir."""
        return bool(self.supervisor_enabled and self.supervisor_manifest_dir)


def _err(msg):
    raise ConfigError(msg)


def _to_int(value, default, what, allow_zero=False):
    """解析整数配置，支持可选的零值校验。

    Args:
        value: 配置值
        default: 默认值（当 value 为 None 时）
        what: 字段描述（用于错误信息）
        allow_zero: 为 False 时拒绝 <= 0 的值（用于 timeout_s）
    """
    if value is None:
        return default
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        _err(f"{what} 必须是整数，得到: {value!r} ({exc})")
    if not allow_zero and result <= 0:
        _err(f"{what} 必须 > 0，得到: {result}")
    return result


def load_config(path=DEFAULT_CONFIG_PATH):
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError, UnicodeDecodeError) as exc:
        _err(f"无法读取/解析配置 {path}: {exc}")
    if not isinstance(data, dict):
        _err("runner.yaml 必须是 mapping")

    hub = str(data.get("hub") or "").rstrip("/")
    if not hub.startswith("https://"):
        _err("hub 必须是 https:// URL（runner 凭据仅允许走 HTTPS）")
    machine = str(data.get("machine") or "")
    if not machine:
        _err("machine 必填")

    cred_file = Path(str(data.get("credential_file")
                         or Path.home() / ".config" / "agent-fleet" / "runner-credential")).expanduser()
    try:
        credential = cred_file.read_text().strip()
    except OSError:
        _err(f"无法读取 runner credential 文件 {cred_file}")
    if not credential:
        _err("runner credential 为空")

    projects = {}
    raw_projects = data.get("projects") or {}
    if not isinstance(raw_projects, dict):
        _err("projects 必须是 mapping")
    for name, entry in raw_projects.items():
        if not isinstance(entry, dict):
            _err(f"项目 {name} 配置必须是 mapping")
        p = Path(str((entry or {}).get("path") or "")).expanduser()
        if not p.is_absolute():
            _err(f"项目 {name} 路径必须是绝对路径")
        if not p.is_dir():
            _err(f"项目 {name} 路径不存在: {p}")
        projects[str(name)] = p
    if not projects:
        _err("projects 白名单为空")

    agents = {}
    raw_agents = data.get("agents") or {}
    if not isinstance(raw_agents, dict):
        _err("agents 必须是 mapping")
    for agent_type, entry in raw_agents.items():
        if not isinstance(entry, dict):
            _err(f"agents.{agent_type} 配置必须是 mapping")
        entry = entry or {}
        command = entry.get("command")
        if command is not None and not (
                isinstance(command, list) and all(isinstance(c, str) for c in command)):
            _err(f"agents.{agent_type}.command 必须是字符串数组")
        agents[str(agent_type)] = {
            "command": command,
            "timeout_s": _to_int(entry.get("timeout_s"), 1800, f"agents.{agent_type}.timeout_s", allow_zero=False),
        }

    # 注册表默认回退:runner.yaml 完全缺省的可执行家族用内置默认值补齐。
    # 部分指定(含显式 command: null)的家族保持原样,尊重操作者意图。
    for family, profile in PROFILES.items():
        if not profile.executable or family in agents:
            continue
        agents[family] = {
            "command": (list(profile.default_command)
                        if profile.default_command else None),
            "timeout_s": profile.default_timeout_s,
        }

    supervisor = data.get("supervisor") or {}
    if not isinstance(supervisor, dict):
        _err("supervisor 配置必须是 mapping")
    supervisor_enabled = bool(supervisor.get("enabled"))
    supervisor_manifest_dir = None
    if supervisor_enabled:
        sdir = str(supervisor.get("manifest_dir") or "").strip()
        if not sdir:
            _err("supervisor.enabled=true 必须配置 supervisor.manifest_dir")
        supervisor_manifest_dir = Path(sdir).expanduser()

    supervisor_credential = _load_supervisor_credential(supervisor, machine)
    supervisor_public_key = _load_supervisor_public_key(supervisor)

    return RunnerConfig(
        hub=hub,
        machine=machine,
        credential=credential,
        runner_id=str(data.get("runner_id") or machine),
        projects=projects,
        agents=agents,
        supervisor_enabled=supervisor_enabled,
        supervisor_manifest_dir=supervisor_manifest_dir,
        supervisor_credential=supervisor_credential,
        supervisor_public_key=supervisor_public_key,
        poll_interval_s=_to_int(data.get("poll_interval_s"), 15, "poll_interval_s", allow_zero=True),
        heartbeat_interval_s=_to_int(data.get("heartbeat_interval_s"), 30, "heartbeat_interval_s", allow_zero=True),
        cache_dir=Path(str(data.get("cache_dir") or DEFAULT_CACHE_DIR)).expanduser(),
    )


def _load_supervisor_credential(supervisor, machine):
    """Read the supervisor secret. File may be secret-only or ``machine:secret``.

    ControlClient prefixes ``machine:`` onto the secret when building the
    header, so a matching prefix is stripped here. A different prefix is
    rejected rather than silently sending the wrong identity.
    """
    raw_path = str(supervisor.get("credential_file") or "").strip()
    if not raw_path:
        return None
    path = Path(raw_path).expanduser()
    try:
        raw = path.read_text().strip()
    except OSError:
        _err(f"无法读取 supervisor credential 文件 {path}")
    if not raw:
        _err("supervisor credential 为空")
    if ":" in raw:
        prefix, _, secret = raw.partition(":")
        if prefix != machine or not secret:
            _err("supervisor credential 的 machine 前缀与 machine 不一致")
        return secret
    return raw


def _load_supervisor_public_key(supervisor):
    """Decode the Hub Ed25519 public key (base64 of 32 raw bytes)."""
    raw_path = str(supervisor.get("public_key_file") or "").strip()
    if not raw_path:
        return None
    path = Path(raw_path).expanduser()
    try:
        text = path.read_text().strip()
    except OSError:
        _err(f"无法读取 supervisor public_key 文件 {path}")
    if not text:
        _err("supervisor public_key 为空")
    try:
        decoded = base64.b64decode(text, validate=True)
    except (ValueError, TypeError):
        _err("supervisor public_key 不是合法 base64")
    if len(decoded) != 32:
        _err("supervisor public_key 必须是 32 字节 Ed25519 公钥")
    return decoded
