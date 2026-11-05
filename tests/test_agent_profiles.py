# tests/test_agent_profiles.py
from agent_profiles import (
    DEFAULT_TIMEOUT_S, EXECUTABLE_AGENT_TYPES, OBSERVABLE_AGENT_TYPES,
    PROFILES, default_command, is_executable, profile_basenames,
)


def test_executable_types_exactly_five():
    assert EXECUTABLE_AGENT_TYPES == (
        "codex", "claude_code", "hermes", "pi", "zcode")


def test_observable_types_six_including_generic():
    assert OBSERVABLE_AGENT_TYPES == (
        "codex", "claude_code", "hermes", "pi", "zcode", "generic")
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


def test_zcode_profile_default_command_and_session_layout():
    cmd = default_command("zcode")
    assert cmd is not None and cmd == ("zcode", "-p", "{instruction}")
    profile = PROFILES["zcode"]
    # shim 与桌面版脚本直跑两种进程形态都要能精确分类
    assert profile.basenames == ("zcode", "zcode.cjs")
    assert profile.native_session_root == "~/.zcode/cli/rollout"
    # rollout 目录是平铺 model-io-sess_*.jsonl，深度 1 即覆盖
    assert profile.native_session_depth == 1


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
    assert HUB_TYPES == ("codex", "claude_code", "hermes", "pi", "zcode")


def test_probe_default_agent_types_derived_from_registry():
    assert PROBE_TYPES is OBSERVABLE_AGENT_TYPES
    assert set(PROBE_TYPES) == set(OBSERVABLE_AGENT_TYPES)
    assert len(PROBE_TYPES) == 6


import connectors
import tools.adapters as adapters
from agent_profiles import (
    NATIVE_RESUME_AGENT_TYPES, RESUME_VERIFIABLE_AGENT_TYPES,
    SESSION_PATH_RESUME_AGENT_TYPES,
)
from tools.probe import discovery
from tools.supervisor import supervisor as sup_mod


def test_adapter_registry_matches_executable_types():
    # 每个可执行 family 都要有 runner adapter,且不多出注册表外的 adapter
    assert set(adapters._REGISTRY) == set(EXECUTABLE_AGENT_TYPES)


def test_connector_registry_subset_of_observable_types():
    # generic 走 connectors.create 的回退分支,不进注册表
    assert set(connectors._REGISTRY) <= set(OBSERVABLE_AGENT_TYPES)


def test_resume_capability_types_derived_from_profiles():
    assert NATIVE_RESUME_AGENT_TYPES == ("codex", "claude_code", "pi")
    assert RESUME_VERIFIABLE_AGENT_TYPES == ("codex", "claude_code")
    assert SESSION_PATH_RESUME_AGENT_TYPES == ("pi",)
    # 可探针验证 / 按路径续接 都必须先是原生可续接 family
    assert set(RESUME_VERIFIABLE_AGENT_TYPES) <= set(NATIVE_RESUME_AGENT_TYPES)
    assert set(SESSION_PATH_RESUME_AGENT_TYPES) <= set(NATIVE_RESUME_AGENT_TYPES)
    assert set(NATIVE_RESUME_AGENT_TYPES) <= set(EXECUTABLE_AGENT_TYPES)


def test_supervisor_capability_sets_equal_profile_derived_sets():
    assert sup_mod._NATIVE_RESUME_FAMILIES == frozenset(NATIVE_RESUME_AGENT_TYPES)
    assert sup_mod._RESUME_VERIFIABLE_FAMILIES == frozenset(
        RESUME_VERIFIABLE_AGENT_TYPES)
    assert sup_mod._SESSION_PATH_RESUME_FAMILIES == frozenset(
        SESSION_PATH_RESUME_AGENT_TYPES)


def test_supervisor_resume_argv_exists_exactly_for_native_resume_families():
    built = {
        family for family in OBSERVABLE_AGENT_TYPES
        if sup_mod._native_resume_argv(family, "/bin/agent", "tok", "hi")
    }
    assert built == set(NATIVE_RESUME_AGENT_TYPES)


def test_discovery_native_resolver_keys_subset_of_profiles():
    assert set(discovery._PROFILE_NATIVE_RESOLVERS) <= set(PROFILES)
