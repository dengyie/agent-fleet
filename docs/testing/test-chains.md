# 测试链路、回归与证据核验

请求级模型、五项 Token、耗时、费用来源、重试与交互验收详见[请求元数据测试矩阵](request-metadata.md)。

本页说明测试如何串联，以及怎样核验“通过”的含义。[测试入口](README.md)提供环境安装和完整命令；[journeys.json](journeys.json)是必需 selector 的唯一清单；[外部验收](release-acceptance.md)定义真实服务的交付证据。

## 从用户操作到持久化结果

| 链路 | 必经阶段 | 关键断言与测试入口 | 矩阵 |
|---|---|---|---|
| 账号生命周期 | 邀请 → 发码 → 注册 → 登录 → 同文档页面切换/历史前进后退 → 每分钟 session 检查 → 改密/重置 → 撤销旧会话 → 停用 | `test_frontend_browser.py::test_assistant_auth_in_real_browser` 通过真实 Chromium 断言站内切页和浏览器历史保留同一 document、不重复查询 `/api/operator/session`；`test_full_flow.py::test_invitation_registration_recovery_and_owner_isolation` 验证跨账号 404、旧 Cookie 失效；`test_accounts.py` 验证过期/重复验证码；浏览器验证守卫和退出 | AUTH-01…04、MAIL-01 |
| 助手与产物 | 页面 → 账号 Cookie → 提交 → SQLite → scheduler → provider factory → 严格模型 HTTP → list/write/read/artifact → 最终回复 → 下载 → 刷新恢复 | `test_full_flow.py` 比较产物字节和 SHA-256、唯一 Run、重建 app 后的结果；`test_full_flow_browser.py` 验证关闭页面后继续完成、重新打开恢复、502 可见 | ASSIST-01…02、MODEL-01…02、FILES-01 |
| Run workspace artifact | conversation 的冻结 workspace → `workspace.write` → `workspace.artifact` → ArtifactStore manifest → owner/workspace API list/download | `test_platform_run_worker.py::test_worker_artifact_is_listed_and_downloadable_in_conversation_workspace` 以真实 Flask/SQLite/worker 验证生成产物通过现有路由可见且下载字节一致 | FILES-01 |
| 本地验收 | CLI 登录 → readiness → 专用 acceptance-turns → 冻结策略 → 新 worker → 模型 HTTP → Broker → 真实目录 → events → 恢复 → logout → JSON 报告；前端打包拒绝复用含未清单文件的输出目录 | `test_acceptance_flow.py::test_acceptance_http_chain_closes_report_and_node_journal[local]` 和 `test_platform_acceptance_check.py` 要求真实 list、最终回复、恢复和旧 Cookie 撤销全部成立；`test_release_layout.py::DeploymentSafetyTests::test_package_refuses_existing_output_with_unlisted_files` 锁定精确 release 目录 | RELEASE-01 |
| 节点验收 | 同一账号入口 → RemoteToolBroker → command DB → HTTP poll → NodeClient → NodeJournal → DirectoryBackend → HTTP receipt → 模型第二轮 → Run 终态 → 报告 | 上述用例的 `[remote]` 实例使用独立节点目录；模型第二轮必须看到节点 sentinel 文件，Hub 目录为空；命令、journal、Hub receipt 均 succeeded；重复命令不能重执行 | NODE-01、RELEASE-01 |
| 不确定提交 | Run 已持久化 → 202 响应被阻塞 → 统一截止时间到期 → unconfirmed → GET 对话按 client_token 找消息 → 找回唯一 Run → 重建 Hub → 完成 | `test_acceptance_flow.py::test_accepted_response_timeout_recovers_unique_run_by_token` 要求仅一次提交、报告保留对话和 token、GET 恢复无重发 | ASSIST-02、RELEASE-01 |
| 质量证据 | pytest 全量收集 → 参数集合 → setup/call/teardown → journeys 门禁 → JUnit/截图 → CI artifact → main 打包前置条件 | `test_journey_gate.py` 用子进程模拟缺失、筛选、缓存、跳过及失败；完整运行同时检查测试退出码与所有 journey 结果 | GATE-01 |
| 自动部署与 guardian 进程控制 | 同 run 前后端 → 包校验 → 受限 SSH → 维护/空闲检查 → 停写备份 → 原目录更新 → pidfd 固定进程身份后停止 → 启动/探针 → 发布记录；失败恢复旧代码 | `test_auto_deploy.py` 使用真实临时文件和 SQLite 验证 inode/配置/数据保留、部分复制和健康失败、任务竞态、状态提交失败、回滚失败屏障、缺失结果不假绿；Guardian/deployer regressions 验证 PID 复用、同一 pidfd TERM/KILL 与失效时 fail closed；`test_release_layout.py::RuntimeStoreHygieneTests::test_parallel_full_release_archives_have_isolated_staging` 验证并发 tar/provenance 临时文件隔离，发布包测试要求包含 pidfd helper | RELEASE-01 |
| 浏览器 session stale-node 对账与 staging 清理 | 节点心跳超过 300 秒且仍 enabled 时，每轮最多将 128 个 `open/closing` session 原子转为 `unknown`；ArtifactStore 只清理自身、超过 1 小时的中断 staging 目录，每轮最多 128 个；严格边界、未观测/禁用/缺失节点、旧表、symlink 和重复轮次保持安全；只在 browser、remote-execution、签名 gate 同时开启时由显式 entrypoint 启动 | `tests/test_platform_browser_reconciliation.py` 覆盖 TTL、分批、输入校验、legacy schema、gate 生命周期；`tests/test_platform_artifact_staging.py` 覆盖年龄、身份、上限和非法输入；对账不证明 Chromium 退出，也不重放命令，staging 清理不触碰已发布 artifact | RELEASE-01 |

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
| BROWSER-01 | Hub 与 Node 策略拒绝用户提供的 loopback URL；driver 声明及方法映射在 open/dispatch 前校验，探测异常保留 cause 并返回稳定错误，派发异常保留 cause 并映射为 backend failure；transport 和 Chromium 各自只在一次性测试 fixture 中启用 loopback；Chromium 用例断言 CSS 实际生效、图片完成解码、输入框在 PNG 中被遮罩，并验证 redirect/第二 origin 的目标服务器未收到请求；只允许 301/302/303/307/308 跳转，其他 3xx 在读取正文前拒绝；DNS pinning、TLS 和预算见矩阵。该 fixture 不证明 Node runtime 接入或进程级 egress 隔离 |
| BROWSER-02 | browser.submit 需 owner 精确授权、一次性签名派发和 Node 端无重放；真实 DOM/network driver 仍需外部运行时验收 |
| MEMORY-01 | 显式选择 → 冻结内容 → worker 注入：矩阵中的 memory/context 用例 |
| MONITOR-01、ACTION-01、SCHEDULE-01 | 监控证据 → 事件 → 只读诊断/审批操作 → 定时触发；分别验证资源/owner/revision 与去重边界 |
| SESSION-01、SSE-01、ADOPT-01 | runner 会话正文交付、事件分页重连、接管撤销；native 续聊必须绑定私有身份并恢复同一会话，缺少身份不得启动 sibling；terminate/quarantine/pause/resume 必须 fence 在途续聊，未确认回收时返回 `escape_unverified`；旧进程完成回调不得覆盖新 handle，同一会话只允许一个 native resume 在途，暂停会话不得启动 sibling；跨 session 并发追加不得突破机器级加密 spool 配额；同一 session 并发 transcript ingest 不得突破 raw retention 配额；只有 ingest token 而无 source-capability 证明的 `exact` 事件必须在创建 session 或写入 redacted/raw transcript 前拒绝，signed source upgrade 仍未接通；raw retention 每批有界、删除与审计原子提交，并且只在 transcript gate 开启时运行；接管控制入队、exact capture 升级与撤销 CAS 必须串行，撤销后不得再排入控制命令或完成质量升级；exact 升级必须隔离 operator/ingest 鉴权，拒绝 revoked/pending/unknown/非法 session，重复升级只写一条审计，exact 事件同时保留 redacted 流和加密 raw；会话缺少正文不能算任务成功 |
| BACKUP-01 | SQLite在线快照 → 加密及key-id校验 → 独立目录恢复 → integrity/FK与产物校验；覆盖失败清理、真正原子 no-overwrite（包括目标目录竞态）和有界retention |
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

## 审查问题的固定回归入口

| 原故障 | 必须保持的结果 | 回归 selector（省略 `tests/`） |
|---|---|---|
| `.md` MIME 依赖主机数据库，Markdown 被下载为二进制且无法预览 | 支持预览的文本扩展有确定 MIME；显式二进制类型仍拒绝预览；内容、owner 隔离、完整性和截断契约不变 | `test_platform_artifact_routes.py::test_artifact_http_api_is_operator_scoped_and_integrity_checked`、`test_platform_artifact_routes.py::test_artifact_preview_is_bounded_owner_scoped_and_text_only`，已绑定 FILES-01 |
| MemoryItem 校验/SQLite 失败丢失根因，损坏标签被当成空列表 | 仓储、应用服务保留类型化 cause；事务清理不覆盖首个 SQLite 错误；损坏标签失败关闭；HTTP 仍返回固定 503 错误且不泄露存储正文 | `test_platform_memory_error_chains.py`，四个 selector 已绑定 MEMORY-01 |
| 非法记忆选择返回503，Unicode和搜索失败丢失异常链 | 非法ID/选择器/预算/版本/查询返回400且不入队；搜索不可用仍503；保留原因但不泄漏存储错误正文 | `test_memory_context_input_validation.py`，已绑定 MEMORY-01 |
| 损坏的冻结记忆快照被强制转换、重新截断或忽略 | worker 在模型调用前校验冻结字段与原始预算；损坏快照失败并保留有界事件，正常快照保持原样 | `test_memory_context_snapshot_validation.py`，已绑定 MEMORY-01 |
| 只调度字典序前100个 owner，后台失败静默或不能等待停止 | owner 使用索引游标分批循环；单 owner 失败记录脱敏诊断并继续；停止句柄可 signal/join | `test_platform_schedule_recovery.py`、`test_platform_schedules.py`，已绑定 SCHEDULE-01 |
| 健康数据库锁定/访问失败被误判损坏，重建空库且遗留连接 | 真实独占锁和非损坏 SQLite 错误保留原文件并抛出原异常；初始化失败关闭连接；明确损坏仍保留原文件后重建 | `test_task_database_initialization.py`，已绑定 RELEASE-01；包含 Python 3.10 无错误码兼容与扩展错误码 |
| 旧任务表删除后升级中断，重启创建空表而旧数据滞留迁移表 | 历史 schema 升级在一个事务中；复制/删除/重命名失败及进程退出均完整回滚；重试和并发启动保留任务、租约、结果、文件和审计 | `test_task_schema_migration.py`，已绑定 RELEASE-01；schema 固定来自 `182e034` |
| 只读验收先写后拒绝 | 执行前拒绝；文件字节、产物数量、节点派发均无变化 | `test_platform_acceptance_check.py::test_acceptance_denies_tools_before_side_effects`；`test_platform_run_worker.py::test_acceptance_policy_is_frozen_and_enforced_by_new_worker`；上述 adversarial wire 用例 |
| 单个参数 node ID、`-k`、`--deselect` 隐藏失败参数 | 不完整集合不得生成 passed 报告；完整兄弟参数仍属于必需集合 | `test_journey_gate.py::test_hidden_failing_parameter_cannot_pass_journey` |
| 慢响应或截止后才到达 succeeded | 统一截止时间约束创建、提交、轮询、events、恢复；返回 acceptance_timeout/unconfirmed 并保留已知定位信息 | `test_acceptance_deadline.py::test_model_deadline_rejects_late_success_without_replay` |
| `--lf` 缓存提前裁剪参数 | 与 require-journeys / journey-report 同用时退出 UsageError；普通局部调试可使用 | `test_journey_gate.py::test_last_failed_cache_cannot_hide_required_parameters` |
| DNS/TCP/TLS 超过总预算 | DNS 不能迟到后发送请求；多地址连接共享预算；TLS 保留证书/主机名校验 | `test_acceptance_deadline.py` 中 DNS、TCP、TLS 用例 |
| 窗口 metadata 接受 NaN / Infinity 并写入 SQLite | 严格 JSON 编码；API 返回 `invalid_metadata`，创建失败且不留下窗口行 | `test_platform_execution_windows.py::test_window_metadata_rejects_non_finite_json_before_persistence`、`test_window_metadata_http_rejects_non_finite_json_before_persistence`；WINDOW-01 |
| 对话 overrides 接受 NaN / Infinity、无效 Unicode 或显式非对象假值 | 严格 JSON 编码并要求对象；HTTP、应用、仓储和领域边界返回有界校验错误，不创建对话或 Run | `test_platform_conversation.py::test_conversation_rejects_non_finite_overrides_before_persistence`、`test_platform_conversation.py::test_conversation_rejects_invalid_unicode_override_before_persistence`、`test_platform_conversation.py::test_conversation_rejects_falsey_non_object_overrides`、`test_platform_conversation.py::test_turn_rejects_falsey_non_object_overrides`、`test_platform_conversation.py::test_platform_repository_rejects_non_mapping_overrides_before_writes`、`test_platform_defaults.py::test_default_resolution_rejects_non_mapping_overrides`；CONFIG-01 |
| 不完整请求事件含缺失或无效 `started_at` 时 Run 读取返回 503 | 请求投影只公开有限且可显示的 UTC epoch；无效/缺失时间不伪造 elapsed，也不阻断 Run 历史 | `test_request_metadata_api.py::test_active_request_with_invalid_start_time_does_not_break_run_projection`、`test_request_metadata_api.py::test_pending_terminal_unknown_and_projection_queries_are_batched`；USAGE-01 |
HTTP传输边界的后续回归见[请求元数据矩阵](request-metadata.md)：两origin重定向、真实chunked DONE、响应头/体滴流、共享DNS上限与迟到隔离、代理CONNECT及TLS校验、未读响应关闭、worker异常链和超时后调度槽复用，均已加入 MODEL-02 / RUN-02 / RELEASE-01 的逐函数门禁。

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

Linux CI 会在测试成功后、上传证据和启动打包前调用 tools.testing.release_evidence；验证失败会阻断 package job。需要人工复核时，应在**与报告 revision 对应的干净代码检出**中执行下方校验。本机 dirty 报告只能作为开发证据。校验器绑定报告 revision、clean tree、矩阵 SHA-256、全部 journey/参数化 selector、JUnit failure/error/skip、浏览器 PNG 签名和外部项 not_run 状态。Linux artifact 中任何 skip 都会阻断打包。pytest subtests 的总数可高于 JUnit testcase 元素数，校验器分别验证 journey 参数计数与 JUnit 汇总，不假设两者总数相等。

```bash
python3 -m tools.testing.release_evidence \
  --evidence /tmp/fleet-ci-evidence \
  --checkout "$PWD" \
  --revision "$(git rev-parse HEAD)"
```

此校验器也会读取实际 Git `HEAD` 和 porcelain 状态；`--revision` 必须等于 checkout commit，checkout 必须干净。报告中的 revision/dirty 字段不能替代实际 Git 检查。人工诊断优先检查校验器给出的错误码与原始 JUnit/JSON，不维护第二份容易漂移的校验实现。

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
