# Task 2 report: 收敛 DEFAULT_AGENT_TYPES 到注册表

- **status**: DONE
- **提交哈希**: `22bb57d` (feat: derive DEFAULT_AGENT_TYPES from profile registry)
- **测试摘要**: `python3 -m pytest tests/ -q` → 1069 passed, 2 skipped, 143 subtests passed (全量零回归);`tests/test_agent_profiles.py` 8 passed(含 2 个新增身份断言)

## 改动

- `hub/domain/task.py`:顶部 import 区加 `from agent_profiles import EXECUTABLE_AGENT_TYPES`;`DEFAULT_AGENT_TYPES = ("codex", "claude_code", "hermes")` → `DEFAULT_AGENT_TYPES = EXECUTABLE_AGENT_TYPES`。与既有 `from report_schema import ...` 同模式,无循环 import。
- `tools/probe_collectors.py`:import 区加 `from agent_profiles import OBSERVABLE_AGENT_TYPES`;`DEFAULT_AGENT_TYPES = ("hermes", "claude_code", "codex", "generic")` → `DEFAULT_AGENT_TYPES = OBSERVABLE_AGENT_TYPES`。
- `tests/test_agent_profiles.py`:末尾追加 2 个身份断言测试(`HUB_TYPES is EXECUTABLE_AGENT_TYPES`、`PROBE_TYPES is OBSERVABLE_AGENT_TYPES`),先红后绿(TDD)。

## 疑虑 / 备注

1. **brief 标题「三处」与实际两处字面量**:全仓 grep `DEFAULT_AGENT_TYPES`,字面量定义仅两处(`hub/domain/task.py`、`tools/probe_collectors.py`);第三处引用 `tools/agent-self-report.py:48` 是 `from tools.probe_collectors import DEFAULT_AGENT_TYPES`,已随 probe_collectors 转由注册表派生,无需改动。与 brief Step 3/5 的文件清单一致。
2. **`tests/test_task_domain.py` 不存在**:brief Step 4 引用该文件,但本 worktree 无此文件。task-domain 覆盖实际在 `tests/test_services.py`(imports `hub.domain.task` / `validate_task_input`);已改跑 `tests/test_task_api.py tests/test_services.py tests/test_push_only.py`(89 passed)并补全量 `tests/` 验证,零回归。
3. **probe 顺序变化**:probe 侧 `DEFAULT_AGENT_TYPES` 顺序由 `("hermes", "claude_code", "codex", "generic")` 变为 `("codex", "claude_code", "hermes", "generic")`(hermes 开头 → codex 开头)。brief 已注明 ruling 批准,语义无差;`collect_all`/`parse_agent_types` 均按集合处理,未发现按顺序断言的测试(brief Step 4 预期)。
4. 未提交 `.superpowers/` 目录(仅按 brief Step 5 提交 3 个文件);注册表对象未被修改。
