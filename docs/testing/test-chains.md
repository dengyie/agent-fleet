# 测试链路、回归与证据核验

请求级模型、五项 Token、耗时、费用来源、重试与交互验收详见[请求元数据测试矩阵](request-metadata.md)。

本页说明测试如何串联，以及怎样核验“通过”的含义。[测试入口](README.md)提供环境安装和完整命令；[journeys.json](journeys.json)是必需 selector 的唯一清单；[外部验收](release-acceptance.md)定义真实服务的交付证据。

## 从用户操作到持久化结果

| 链路 | 必经阶段 | 关键断言与测试入口 | 矩阵 |
|---|---|---|---|
| 账号生命周期 | 邀请 → 发码 → 注册 → 登录 → 改密/重置 → 撤销旧会话 → 停用 | `test_full_flow.py::test_invitation_registration_recovery_and_owner_isolation` 验证跨账号 404、旧 Cookie 失效；`test_accounts.py` 验证过期/重复验证码；浏览器验证守卫和退出 | AUTH-01…04、MAIL-01 |
| 助手与产物 | 页面 → 账号 Cookie → 提交 → SQLite → scheduler → provider factory → 严格模型 HTTP → list/write/read/artifact → 最终回复 → 下载 → 刷新恢复 | `test_full_flow.py` 比较产物字节和 SHA-256、唯一 Run、重建 app 后的结果；`test_full_flow_browser.py` 验证关闭页面后继续完成、重新打开恢复、502 可见 | ASSIST-01…02、MODEL-01…02、FILES-01 |
| 本地验收 | CLI 登录 → readiness → 专用 acceptance-turns → 冻结策略 → 新 worker → 模型 HTTP → Broker → 真实目录 → events → 恢复 → logout → JSON 报告 | `test_acceptance_flow.py::test_acceptance_http_chain_closes_report_and_node_journal[local]` 和 `test_platform_acceptance_check.py` 要求真实 list、最终回复、恢复和旧 Cookie 撤销全部成立 | RELEASE-01 |
| 节点验收 | 同一账号入口 → RemoteToolBroker → command DB → HTTP poll → NodeClient → NodeJournal → DirectoryBackend → HTTP receipt → 模型第二轮 → Run 终态 → 报告 | 上述用例的 `[remote]` 实例使用独立节点目录；模型第二轮必须看到节点 sentinel 文件，Hub 目录为空；命令、journal、Hub receipt 均 succeeded；重复命令不能重执行 | NODE-01、RELEASE-01 |
| 不确定提交 | Run 已持久化 → 202 响应被阻塞 → 统一截止时间到期 → unconfirmed → GET 对话按 client_token 找消息 → 找回唯一 Run → 重建 Hub → 完成 | `test_acceptance_flow.py::test_accepted_response_timeout_recovers_unique_run_by_token` 要求仅一次提交、报告保留对话和 token、GET 恢复无重发 | ASSIST-02、RELEASE-01 |
| 质量证据 | pytest 全量收集 → 参数集合 → setup/call/teardown → journeys 门禁 → JUnit/截图 → CI artifact → main 打包前置条件 | `test_journey_gate.py` 用子进程模拟缺失、筛选、缓存、跳过及失败；完整运行同时检查测试退出码与所有 journey 结果 | GATE-01 |
| 自动部署 | 同 run 前后端 → 包校验 → 受限 SSH → 维护/空闲检查 → 停写备份 → 原目录更新 → 启动/探针 → 发布记录；失败恢复旧代码 | `test_auto_deploy.py` 使用真实临时文件和 SQLite 验证 inode/配置/数据保留、部分复制和健康失败、任务竞态、状态提交失败、回滚失败屏障、缺失结果不假绿 | RELEASE-01 |

以上文件均位于 `tests/`。同一测试为多个 journey 提供证据时只执行一次；selector 数、参数实例数和 pytest 用例总数不是同一指标。

### 只读权限的四道证据

1. **HTTP 入口和身份。** `test_acceptance_route_enforces_account_owner_origin_and_policy` 通过真实账号 HTTP 验证匿名、伪造 Origin、其他 owner、已撤销 Cookie 被拒绝，拒绝后数据库没有新消息或 Run。
2. **持久化权限。** `test_acceptance_queued_policy_survives_hub_restart` 提交 `null`、`unrestricted` 和写工具数组等客户端权限覆盖值；服务端仍保存 `acceptance_read_only`。新 Hub/new worker 只声明 list。`test_acceptance_http_idempotency_cannot_cross_policy` 验证普通与验收任务的 token 双向冲突，同策略重试则返回原 Run。
3. **模型 HTTP 边界。** `test_adversarial_wire_tool_cannot_reach_local_or_remote_backend` 让独立 HTTP 对端强制返回 write、artifact、exec、read。请求中只声明 `workspace_list`；适配器拒绝未声明工具，模型只请求一次，零工具执行、零节点派发、零产物，原文件字节不变。
4. **Broker 边界。** `test_platform_acceptance_check.py::test_acceptance_denies_tools_before_side_effects` 注入内部 ModelResponse，绕过适配器验证本地 Broker 的执行前拒绝；`test_platform_run_worker.py::test_acceptance_policy_is_frozen_and_enforced_by_new_worker` 覆盖本地和远程 Broker，确认禁止工具不进入 backend/enqueue。它们证明权限约束不只依赖模型或适配器。

### 其余能力如何接入链路

下表列出主测试模块，函数级必需集合、验收标准和边界仍以矩阵为准。

| 矩阵 | 接入位置 / 主测试模块 |
|---|---|
| RUN-01、RUN-02 | 提交和 worker 生命周期：`test_full_flow.py`、`test_platform_run_worker.py`、`test_platform_run_scheduler.py` |
| CONFIG-01 | 提交前引用校验与冻结配置：`test_platform_defaults.py`、`test_platform_provider_canary.py` |
| USAGE-01 | 模型前预算准入、模型后幂等记账与 unknown 释放：`test_platform_usage.py` |
| WINDOW-01 | 执行窗口授权、票据、写入租约：矩阵中的 execution window API 用例；真实 PTY 另做 NODE-LIVE |
| MEMORY-01 | 显式选择 → 冻结内容 → worker 注入：矩阵中的 memory/context 用例 |
| MONITOR-01、ACTION-01、SCHEDULE-01 | 监控证据 → 事件 → 只读诊断/审批操作 → 定时触发；分别验证资源/owner/revision 与去重边界 |
| SESSION-01、SSE-01、ADOPT-01 | runner 会话正文交付、事件分页重连、接管撤销；会话缺少正文不能算任务成功 |
| BACKUP-01 | SQLite backup API → 加密备份 → 独立目录恢复 → integrity/FK 与产物校验 |
| UI-01 | 控制台路由、页面操作、长文本/Markdown/恶意 HTML；与助手贯穿浏览器用例共同验证 UI |

列出任一组当前绑定的完整 selector，无需手抄可能过期的清单：

```bash
python3 - <<'PY'
import json
matrix = json.load(open('docs/testing/journeys.json'))
for row in matrix['journeys']:
    print(row['id'], row['title'])
    for selector in row['tests']:
        print('  ' + selector)
PY
```

## 五项审查问题的固定回归入口

| 原故障 | 必须保持的结果 | 回归 selector（省略 `tests/`） |
|---|---|---|
| 只读验收先写后拒绝 | 执行前拒绝；文件字节、产物数量、节点派发均无变化 | `test_platform_acceptance_check.py::test_acceptance_denies_tools_before_side_effects`；`test_platform_run_worker.py::test_acceptance_policy_is_frozen_and_enforced_by_new_worker`；上述 adversarial wire 用例 |
| 单个参数 node ID、`-k`、`--deselect` 隐藏失败参数 | 不完整集合不得生成 passed 报告；完整兄弟参数仍属于必需集合 | `test_journey_gate.py::test_hidden_failing_parameter_cannot_pass_journey` |
| 慢响应或截止后才到达 succeeded | 统一截止时间约束创建、提交、轮询、events、恢复；返回 acceptance_timeout/unconfirmed 并保留已知定位信息 | `test_acceptance_deadline.py::test_model_deadline_rejects_late_success_without_replay` |
| `--lf` 缓存提前裁剪参数 | 与 require-journeys / journey-report 同用时退出 UsageError；普通局部调试可使用 | `test_journey_gate.py::test_last_failed_cache_cannot_hide_required_parameters` |
| DNS/TCP/TLS 超过总预算 | DNS 不能迟到后发送请求；多地址连接共享预算；TLS 保留证书/主机名校验 | `test_acceptance_deadline.py` 中 DNS、TCP、TLS 用例 |

修改缺陷时先证明用例能识别坏行为：通过受控输入或在隔离工作树临时移除对应修复使其失败，再恢复并运行同一用例。新链路通过不能替代旧回归用例；不要修改失败断言来接受未完成的行为。

## 按层运行

命令均从仓库根目录执行；首次使用按[测试入口](README.md)安装依赖。

```bash
# 账号验收、模型 HTTP、真实 loopback 节点及重启/未知提交恢复
PYTHONPATH=. .venv/bin/python -m pytest tests/test_acceptance_flow.py -q

# 五项审查边界及 Broker/节点集成
PYTHONPATH=. .venv/bin/python -m pytest \
  tests/test_platform_acceptance_check.py tests/test_acceptance_deadline.py \
  tests/test_platform_run_worker.py tests/test_platform_node_http_e2e.py \
  tests/test_journey_gate.py -q

# 账号至文件/产物，以及邮件的真实 loopback 协议
PYTHONPATH=. .venv/bin/python -m pytest \
  tests/test_full_flow.py tests/test_account_smtp_transport.py -q

# 发布包、自动部署事务、回滚与受限接收端
PYTHONPATH=. .venv/bin/python -m pytest \
  tests/test_auto_deploy.py tests/test_release_layout.py -q

# 浏览器贯穿（需要已安装 Chromium）
FLEET_PLAYWRIGHT_MODULE=/tmp/fleet-browser/node_modules/playwright \
FLEET_BROWSER_CHANNEL=chromium \
FLEET_SCREENSHOTS=/tmp/fleet-chain-browser \
PYTHONPATH=. .venv/bin/python -m pytest tests/test_full_flow_browser.py -q
```

这些都是定位命令。交付验证必须运行[完整门禁命令](README.md#一条命令运行完整门禁)，不加 `-k`、参数实例 node ID、`--deselect` 或 `--lf` 来缩小范围。不带 `--require-journeys` 的局部运行不能证明完整覆盖；仅 `--journey-report` 输出证据也不会强制进程因覆盖不完整而失败。

## 核验 CI 证据

CI artifact 名为 `agent-fleet-test-evidence`，保留 7 天：

- `journeys.json`：revision、工作树 dirty、矩阵 SHA-256、每个 selector 的 collected/passed/nonpassing_cases、外部 not_run。
- `results.xml`：所有测试的失败、错误、跳过与耗时；流程全绿也不能覆盖矩阵之外的测试失败。
- `browser/`：由浏览器测试输出的截图；缺少浏览器依赖导致的 skip 会阻断相应 journey。

先确认 GitHub run 的 `headSha` 是本次 PR head，并核对 CI conclusion。PR workflow 通常检出 GitHub 生成的 merge commit，因此报告 revision 可能与 PR head 不同；应与该次 workflow 的 checkout SHA 对照。不要拿上次 CI 的结果证明新提交。下载例：

```bash
gh run view RUN_ID --repo dengyie/agent-fleet --json headSha,status,conclusion,url
gh run download RUN_ID --repo dengyie/agent-fleet \
  --name agent-fleet-test-evidence --dir /tmp/fleet-ci-evidence
```

以下核验命令应在**与报告 revision 对应的干净代码检出**中执行。本机运行产生 dirty=true 时只能作为开发证据；提交后使用 Linux CI 的干净报告作为交付证据。命令对 Linux CI 要求零跳过：

```bash
python3 - /tmp/fleet-ci-evidence "$(git rev-parse HEAD)" <<'PY'
import hashlib, json, sys
from pathlib import Path
import xml.etree.ElementTree as ET

evidence = Path(sys.argv[1])
raw = Path('docs/testing/journeys.json').read_bytes()
matrix = json.loads(raw)
report = json.loads((evidence / 'journeys.json').read_text())
assert report['revision'] == sys.argv[2], 'wrong checkout / stale report'
assert report['working_tree_dirty'] is False, 'dirty checkout'
assert report['matrix_sha256'] == hashlib.sha256(raw).hexdigest(), 'wrong matrix'
assert report['ci_status'] == 'passed'
rows = report['journeys']
assert len(rows) == len(matrix['journeys'])
assert {r['id'] for r in rows} == {r['id'] for r in matrix['journeys']}
for expected in matrix['journeys']:
    row = next(r for r in rows if r['id'] == expected['id'])
    assert row['status'] == 'passed'
    assert len(row['selectors']) == len(expected['tests'])
    assert {s['selector'] for s in row['selectors']} == set(expected['tests'])
    for selector in row['selectors']:
        assert selector['status'] == 'passed'
        assert selector['collected'] == selector['passed'] > 0
        assert selector['nonpassing_cases'] == []
root = ET.parse(evidence / 'results.xml').getroot()
cases = list(root.iter('testcase'))
assert cases and not list(root.iter('failure')) and not list(root.iter('error'))
assert not list(root.iter('skipped')), 'inspect skip reason; Linux release evidence must be complete'
assert {r['id'] for r in report['external_checks']} == {r['id'] for r in matrix['external_checks']}
assert all(r['status'] == 'not_run' for r in report['external_checks'])
assert list((evidence / 'browser').rglob('*.png')), 'missing browser screenshots'
print(f"Verified {len(rows)} journeys and {len(cases)} JUnit cases; external checks remain not_run")
PY
```

macOS 上依赖 `/proc` 和真实 stop/resume 的测试可能跳过；保留具体原因，并由 Linux CI 补齐这些验证。不能将预期的本机平台跳过扩展成对其他测试 skip 的豁免。pytest subtests 的统计与 JUnit testcase 数可能不同，核验重点是无失败/错误、参数齐全以及必需流程全通过，不在文档中固定一个会过期的总测试数。

## 故障注入和真实边界

| Fixture / 故障 | 真实执行 | 可控替代 | 未证明的环境 |
|---|---|---|---|
| `StrictProvider` | 真 HTTP、真实产品 serializer、工具历史和 JSON Schema 校验 | 外部模型的响应与故障；forced_tool 故意越权 | 模型实际可用性、质量、额度 |
| `FullFlowHub` | 账号 Cookie、SQLite、scheduler、provider factory、文件与产物 | 临时配置、测试账号；账号测试的 sender 收集邮件 | 生产配置、真实邮箱收到邮件 |
| `LoopbackNode` | 产品 NodeClient/Transport、HTTP poll/receipt、journal、DirectoryBackend、独立目录 | 节点运行在同一测试进程的线程；本用例不启用签名 gate | 真正异地主机、网络故障、PTY；签名另由 NODE-01 契约用例验证 |
| 截止时间对端 | 真实慢响应头/体、TLS socket 与客户端预算 | DNS/TCP 延迟部分采用可控函数/socket；单个已接受响应使用 after_request 阻塞 | 公网线路与生产代理配置 |
| SMTP 对端 | TLS/STARTTLS、认证、MIME、证书与 DATA 失败 | loopback 收件服务 | 实际邮箱送达和反垃圾 |

远程链路 fixture 保留本地默认工作区并显式选择 `remote`；readiness 只预检默认配置，所选节点能否执行由后续实际 command/receipt 决定。CLI 报告里的 `remote_execution=not_tested` 是通用的外部节点验收声明，不能因为 loopback 用例通过就改成生产节点已验收。

遇到不确定提交，先读取报告中的 conversation_id/client_token/run_id 并按[恢复步骤](release-acceptance.md#不确定结果的只读定位)查询；禁止为了得到绿色报告直接重复整个验收命令。证据中不要保存凭据、Cookie、验证码、原始 provider 正文或数据库。截图只允许一次性 fixture 数据。
