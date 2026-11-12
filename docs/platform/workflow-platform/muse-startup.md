# Muse 正常启动登记

此入口复用[受控 Muse 外部适配器](../../superpowers/plans/2026-10-25-external-agent-workflow-dispatch.md)及原 Workflow 协调器，让正常 Hub 配置可登记外部任务模板。返回[平台索引](README.md)；实现与验收见[启动登记计划](../../superpowers/plans/2026-11-12-muse-startup-registration.md)。

## 本地执行边界

现有 transport 固定使用 **Hub 所在机器的 `127.0.0.1:9222`** 和原 Muse CDP helper。它要求操作者已准备并登录相应 Muse 浏览器；配置文件不会代为登录、启动浏览器或接入远端 Node。一个 journal 对应一个明确 account_ref；当前页面、忙碌与人工插入消息检查继续由原 bridge 处理。

当前适配器支持稳定 token 提交、原任务查回与状态观察。取消、停止确证和产物导出仍不支持；不能给这个模板配置 output_specs、validator_refs 或 Node capability_requirements。模型自报完成保留 `agent_reported`，人工接受不会自动完成。

## 初始化 journal

选择持久、独立的绝对路径。预览不会创建文件或请求浏览器；显式初始化只建立本地 journal 的不可变身份：

```bash
python3 -m hub.muse_setup --journal /absolute/persistent/state/muse-account-a.db
python3 -m hub.muse_setup --journal /absolute/persistent/state/muse-account-a.db --initialize
```

重复初始化返回相同 journal_identity，保留原任务。此 ID 不是登录 token。已有 Workflow 使用该文件后必须保存原 journal；替换文件即使同名也不属于同一接收方。不要用空 journal 代替丢失的原任务账本。

## 配置模板

下面脚本读取已初始化 journal 的身份，生成一个单步骤示例。替换绝对路径、owner/workspace、account_ref 和任务 objective；owner/workspace 必须已在 Hub 登记且启用。定义来自受信配置，不能把远端网页或模型输出作为模板。

```bash
python3 - <<'PY'
import json
from pathlib import Path
from tools.browser_bridge.muse import MuseBridge

journal = Path('/absolute/persistent/state/muse-account-a.db')
identity = MuseBridge(journal).journal_identity()
assert identity is not None
definition = {
    'schema_version': 1, 'template_id': 'muse-task', 'revision': 1,
    'acceptance_required': True,
    'steps': [{'step_key': 'ask', 'kind': 'external_agent', 'requires': [], 'contract': {
        'schema_version': 1, 'objective': 'Complete the explicitly delegated task',
        'input_refs': [], 'output_specs': [], 'capability_requirements': [],
        'policy_ref': {'id': 'muse-task', 'revision': 1},
        'validator_refs': [], 'context_refs': [], 'retry_class': 'manual_only',
        'budget': {'max_tokens': 100000, 'max_model_requests': 1,
                   'max_active_seconds': 3600, 'max_wait_seconds': 7200},
    }}],
}
value = {'schema_version': 1, 'muse': [{
    'owner_id': 'operator@example.com', 'workspace_id': 'workspace-a',
    'target_key': 'muse-account-a', 'account_ref': 'account-a',
    'journal_path': str(journal), 'journal_identity': identity, 'definition': definition,
}]}
Path('/absolute/persistent/config/muse.json').write_text(
    json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
PY
```

文件最大 256 KiB、1–32 条登记；每条必须使用以上完整字段。模板可含原合同允许的多个线性 external_agent 步骤，所有步骤使用同一 Muse 适配器、相同 policy_ref 和每目标 1 个槽位。每个 scoped target 和模板键须唯一。Memory/summary/文本输入仍使用既有显式引用及预算合同，新增配置不扩展二进制或 Node 能力支持。

## Hub 开关与恢复

在原 `fleet-gates.conf` 或进程环境中设置：

```text
AGENT_FLEET_PLATFORM_MUSE_REGISTRATION_FILE=/absolute/persistent/config/muse.json
AGENT_FLEET_PLATFORM_EXTERNAL_AGENTS_ENABLED=1
```

平台、Workflow 与新派发开关也必须启用；使用已有的常驻 Hub coordinator 启动方式。路径遵循显式参数、环境、gates 文件的优先级；显式或环境空字符串关闭文件加载。Workflow 总 gate 关闭时不装配。可同时设置 coding-delivery 的 `AGENT_FLEET_PLATFORM_WORKFLOW_REGISTRATION_FILE`，两个配置的不同模板/路由会合并；冲突模板、重复目标或同时注入自定义外部适配器会拒绝启动。

**关闭 `PLATFORM_EXTERNAL_AGENTS_ENABLED` 只关闭新的外部发送。** 保留同一登记文件和 journal，可继续对原任务进行查询及恢复；已有成功步骤不会重新提交。未派发的外部流程保持等待。启动本身只校验配置、工作区和 journal 身份，不访问浏览器、不自动初始化账本。只有派发开关、外部发送开关及原协调器准入均允许时才可能发送。

受控测试验证正常配置产生一次发送、相同 token/binding 重启查回、关闭外部发送后的原任务收尾、pending acceptance 和不伪造校验证据。它不代替真实 Muse 账号/浏览器、断网休眠、生产配置或完整 Linux/browser 发布验收。
