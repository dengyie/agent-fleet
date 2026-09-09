# Task 3 完成报告：runner_config 回退到注册表默认

## Status
DONE

## 提交哈希
64e9a85944baf9a9d441fb2c5c8bb77886b135ac（feat/agent-profile-registry 分支上）

## 测试摘要
tests/test_runner_config.py：先写 3 个失败测试（`test_missing_agents_falls_back_to_registry` 确认 `KeyError: 'codex'`），实现回退后 3 passed；回归——tests/test_runner.py + tests/test_config.py 共 54 passed，零回归。

## 疑虑 / 备注
- `agent_profiles.py` 位于仓库根，`tools/runner_config.py` 用 `from agent_profiles import PROFILES` 导入。pytest 运行时仓库根在 sys.path 上，测试通过；但 `load_config` 若被非仓库根 CWD 的脚本导入时可能找不到 `agent_profiles`。受 Task 2 同样的导入方式约束，未做额外处理，仅提示。
- 回退循环中 `timeout_s` 用 `profile.default_timeout_s`，不经过 `_to_int`（注册表值是 int 常量，无类型验证问题）。与只剩顶层 `hub/machine` 的最小配置配合正常。
- 部分指定（含显式 `command: null` 的 hermes）保持原样不覆盖，符合零行为回归约束，由 3 个测试覆盖验证。