# tests/test_agent_profiles.py
from agent_profiles import (
    DEFAULT_TIMEOUT_S, EXECUTABLE_AGENT_TYPES, OBSERVABLE_AGENT_TYPES,
    PROFILES, default_command, is_executable, profile_basenames,
)


def test_executable_types_exactly_four():
    assert EXECUTABLE_AGENT_TYPES == ("codex", "claude_code", "hermes", "pi")


def test_observable_types_five_including_generic():
    assert OBSERVABLE_AGENT_TYPES == (
        "codex", "claude_code", "hermes", "pi", "generic")
    assert set(OBSERVABLE_AGENT_TYPES) == set(PROFILES)


def test_codex_default_command_has_instruction_placeholder():
    cmd = default_command("codex")
    assert cmd is not None and "{instruction}" in cmd


def test_hermes_has_no_default_command():
    assert default_command("hermes") is None


def test_pi_profile_default_command_and_session_layout():
    cmd = default_command("pi")
    assert cmd is not None and cmd == ("pi", "-p", "{instruction}")
    profile = PROFILES["pi"]
    assert profile.basenames == ("pi",)
    assert profile.native_session_root == "~/.pi/agent/sessions"
    assert profile.native_session_depth == 2


def test_generic_not_executable_but_observable():
    assert not is_executable("generic")
    assert PROFILES["generic"].pattern == "claude|codex|astrbot|openclaw|opencode|aider"
    # generic 绝不能进精确 basename 集(靠 substring pattern 匹配)
    assert profile_basenames("generic") == ()
    # pi 是 substring 危险词(会命中 pip/pipenv),只允许精确 basename
    assert profile_basenames("pi") == ("pi",)


def test_default_timeout_applies():
    assert all(p.default_timeout_s == DEFAULT_TIMEOUT_S for p in PROFILES.values())


from hub.domain.task import DEFAULT_AGENT_TYPES as HUB_TYPES
from tools.probe_collectors import DEFAULT_AGENT_TYPES as PROBE_TYPES


def test_hub_default_agent_types_derived_from_registry():
    # 身份断言:必须与注册表同一个 tuple 对象(派生),而非字面量副本
    assert HUB_TYPES is EXECUTABLE_AGENT_TYPES
    assert HUB_TYPES == ("codex", "claude_code", "hermes", "pi")


def test_probe_default_agent_types_derived_from_registry():
    assert PROBE_TYPES is OBSERVABLE_AGENT_TYPES
    assert set(PROBE_TYPES) == set(OBSERVABLE_AGENT_TYPES)
    assert len(PROBE_TYPES) == 5
