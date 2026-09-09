from pathlib import Path

import pytest

from tools.runner_config import ConfigError, load_config


def _write(tmp: Path, agents_block: str = "") -> Path:
    cred = tmp / "runner-credential"
    cred.write_text("secret-token")
    body = (
        "hub: https://example.com\n"
        "machine: m1\n"
        f"credential_file: {cred}\n"
        "projects:\n"
        f'  p1:\n    path: "{tmp}"\n'
        + agents_block
    )
    p = tmp / "runner.yaml"
    p.write_text(body)
    return p


def test_missing_agents_falls_back_to_registry(tmp_path):
    cfg = load_config(_write(tmp_path))
    assert cfg.agents["codex"]["command"] == [
        "codex", "exec", "--approve-for-me", "--ephemeral",
        "--ignore-user-config", "--json", "{instruction}"]
    assert cfg.agents["claude_code"]["command"] == ["claude", "-p", "{instruction}"]
    assert cfg.agents["hermes"]["command"] is None
    assert cfg.agents["codex"]["timeout_s"] == 1800


def test_explicit_override_wins(tmp_path):
    p = _write(tmp_path, (
        "agents:\n"
        "  codex:\n"
        '    command: ["/opt/bin/codex", "run", "{instruction}"]\n'
        "    timeout_s: 3600\n"
    ))
    cfg = load_config(p)
    assert cfg.agents["codex"]["command"] == ["/opt/bin/codex", "run", "{instruction}"]
    assert cfg.agents["codex"]["timeout_s"] == 3600


def test_explicit_null_command_not_overridden(tmp_path):
    p = _write(tmp_path, "agents:\n  hermes:\n    command: null\n")
    cfg = load_config(p)
    assert cfg.agents["hermes"]["command"] is None


def test_explicit_zero_timeout_s_is_config_error(tmp_path):
    # 显式 0 是配置错误（永不超时 = 挂死风险），绝不能静默回退默认 1800。
    p = _write(tmp_path, "agents:\n  codex:\n    timeout_s: 0\n")
    with pytest.raises(ConfigError, match="timeout_s"):
        load_config(p)


def test_poll_interval_allows_small_positive_values(tmp_path):
    # 轮询间隔允许1秒等小值，不应被 timeout 的零值校验拒绝
    p = _write(tmp_path, "poll_interval_s: 1\nheartbeat_interval_s: 5\n")
    cfg = load_config(p)
    assert cfg.poll_interval_s == 1
    assert cfg.heartbeat_interval_s == 5


def test_timeout_negative_rejected(tmp_path):
    # 负数 timeout 必须被拒绝
    p = _write(tmp_path, "agents:\n  codex:\n    timeout_s: -10\n")
    with pytest.raises(ConfigError, match="timeout_s.*> 0"):
        load_config(p)

