# 全流程测试与质量门禁

VM 原生执行与流式可靠性参考[vm2api 源码对照](../research/vm2api-architecture-reference.md)及
[实施计划](../superpowers/plans/2026-11-27-vm-native-stream-reliability.md)。专项覆盖
64KiB 增量分帧、UTF-8 任意断点、1MiB 边界、合并帧、部分 EOF、写锁/管道反压截止、
派发前取消、close 唤醒、首个错误保持、回收失败/未知时不报成功且保留 owner、重启后
禁止新进程，以及原生进程尚未结束时公开 Hub HTTP 已读到文本/工具的真实整链证据：

```sh
python3 -m pytest tests/test_acp_transport_bounds.py tests/test_agy_acp.py \
  tests/test_acp_lifecycle_races.py tests/test_acp_interactions.py \
  tests/test_supervisor_process.py tests/test_supervisor_attach.py \
  tests/test_review_runner_control.py tests/test_round5_windows_process.py -q
```

使用独立 stdio 进程、实际 Supervisor/加密 spool/临时 SQLite/Flask HTTP；不调用真实
Agent、外部模型或 OAuth。Windows/macOS CI 运行已有 ACP suite 和新增回收守卫；真实 VM
隔离、供应商授权与模型输出的验收仍需单列证据，不能由 fixture 推断。

AGY 多 Google 账号、临时 OAuth 密文、签名控制、不可变任务账号、节点私有 profile、
重启恢复及账号隔离见[账号 API](../native-provider-accounts.md)与
[实施计划](../superpowers/plans/2026-11-25-agy-multi-account.md)：

```sh
python3 -m pytest tests/test_agy_accounts.py tests/test_provider_accounts.py \
  tests/test_provider_account_binding.py tests/test_provider_account_control.py \
  tests/test_provider_account_e2e.py -q
```

整链用独立本地子进程实现登录/ACP 对端，实际经过 Flask、SQLite、签名队列、
ControlClient、两个受管 worker，验证登录无 prompt、重试找回、账号 A 停用/退出
不影响 B、凭据清除而历史保留；不运行真实 AGY、Google 或模型。专项列入
Windows/macOS CI；真实供应商登录仍需单独验收。

网页账号管理与任务选择使用真实 Hub/SQLite 和 Chrome，脚本化节点状态回报，
不调用真实 Google 或模型。桌面与手机均覆盖写响应丢失后刷新恢复、原 token、
授权码不落浏览器存储、账号间取消隔离、停用后禁止新任务，以及已接收任务恢复
保留原绑定。刷新节点目录及切换 Agent 后返回 AGY 不得清空所选账号；已禁用账号
在刷新后仍阻断发送，不得改用默认登录。使用现有 Playwright 和 Chrome（CI 设置 `chromium`）：

```sh
FLEET_PLAYWRIGHT_MODULE=/path/to/node_modules/playwright FLEET_BROWSER_CHANNEL=chrome \
  python3 -m pytest tests/test_provider_accounts_frontend.py -q
```

账号任务领取专项沿用上述五个 suite；[领取授权修复计划](../superpowers/plans/2026-11-26-provider-account-delivery-guard.md)
记录 main 基线的 4 个失败证据。新增真实 HTTP/SQLite 回归覆盖：禁用/退出已提交但节点
尚未执行、管理员降权/停用、账号服务缺失或存储失败时不领取；队首受阻不影响后续任务，
恢复授权后领取原任务；账号写锁持续至 task lease commit，错误后释放；精确账号/owner/
machine 匹配、1501 条授权绑定且 SQLite 参数限额为 32、并发领取唯一 lease、失败回滚，
临时授权关系不进入持久 schema。禁止访问真实 Agent 或 Google。

Hub 的 signed-command SQLite 权限守卫使用 POSIX owner，因此 Windows 对完整
Hub/签名存储用例按平台跳过；实际节点登录/NTFS ACL/worker lease/generation
取消测试仍在 Windows 执行，完整 HTTP→签名队列→节点链路在 Linux/macOS 执行。

自动部署发布事务、配置中的 SQLite 快照清单、schema rollback、receiver 与应用配置边界：

```sh
PYTHONPATH=. python3 -m pytest tests/test_auto_deploy.py \
  tests/test_review_store_auto_release_rollback.py tests/test_release_layout.py -q
```

`auto-deploy.example.json` 的回归检查要求快照清单包含默认 Supervisor outbox `var/supervisor/commands.db`。已安装宿主 receiver 使用独立的 `/etc/agent-fleet-deploy.json`，需按同一清单单独核验；更新应用包不会更新此 root 配置。

Supervisor Windows 进程树、跨实例取消与 leader 退出后的 orphan 存活判定：

```sh
PYTHONPATH=. python3 -m pytest tests/test_round5_windows_process.py \
  tests/test_review_runner_control.py tests/test_supervisor_process.py -q
```

Windows Job Object 不随 Supervisor 句柄自动持久化。恢复时如果原 leader 已退出，必须检查仍挂在其 PID 下的后代；有存活后代或无法枚举时返回 `liveness_unknown`，不能把会话投影为已结束。以上测试用受控进程表覆盖该 Windows 分支；真实 NTFS/Win32 生命周期仍由 Windows CI 执行。

该组还通过两个共享同一 `manifest_dir` 的 Supervisor 实例并发替换同一会话清单和 owner 身份记录，确保各自使用唯一同目录临时文件；原子替换失败时清单保留原内容并尽力清理临时文件，外部错误仍为 `manifest_write_failed`。这些写入回归和 Windows orphan 存活回归也显式列入 Windows、macOS CI。

Windows 同时替换目标的临时 access/share/lock 拒绝（原生错误 5/32/33）最多尝试六次，主动退避等待合计 150ms；持续拒绝仍失败，其他存储错误不重试。测试保留两个写入者真实同时替换，并覆盖短暂争用恢复、持续拒绝保留旧文件和其他错误立即失败；仅重试文件发布，不重跑 Agent 命令。

电脑 Agent 的节点启动目录、原启动请求找回、请求字段绑定与并发/取消回归见[原生任务 HTTP 契约](../native-task-api.md)。目录、启动和主助手共用应用服务；新回归 `tests/test_native_task_api.py` 在 Linux、Windows、macOS CI 执行。

原生会话的消息响应丢失恢复、事件 tail/before/after 游标、会话目录快照分页，以及原生名称、客户端/模型元数据、逐事件可见内容与 source-record 隔离见[原生会话 HTTP 契约](../native-session-api.md)。元数据专项验证两台平台实际 Agent 记录形状、Claude 版本映射、Pi 格式版本排除、ZCode/Hermes 只读 store 提取、保留会话有限 head/tail 回补、稳定事件 ID 丢响应恢复、损坏 checkpoint fail-closed、SQLite/HTTP 投影填空而不覆盖新值及内容游标不变。专项及相关现有 HTTP、SQLite、采集器、前端状态、交互回答和 Supervisor 回归：

元数据的来源、实际复现、分层实现和覆盖边界见[元数据恢复 Review](../reviews/2026-11-25-native-session-metadata.md)及[实施计划](../superpowers/plans/2026-11-25-native-session-metadata-recovery.md)。Codex v5 的 thread-store 名称需同时核对线程 ID、cwd 和 rollout 路径；不猜测其他 `state_*.sqlite` 版本。

ZCode 桌面 idle 扫描从精确会话的只读 SQLite store 补齐已完成工具调用/结果；专项还验证运行中工具行作为安全游标屏障、桌面消息/元数据仅在 spool 持久化后确认、原子 checkpoint 发布失败保留旧状态、损坏的旧 checkpoint fail-closed、重试去重、配额与存储失败、隐藏推理过滤和数据库路径不泄漏：

```sh
python3 -m pytest tests/test_review_node_sync.py tests/test_native_agent_storage.py \
  -q
```

节点私有 JSON checkpoint 复用的 `atomic_private_json` 还要求 replace 失败时保留
旧记录并清理唯一暂存文件；此精确回归同时加入 Windows、macOS Agent adapter CI：

```sh
python3 -m pytest tests/test_workflow_runner_source.py \
  -k atomic_private_json_replace_failure -q
```

包含会话桥接、Hub API、CLI/native transcript 等相关回归的较宽范围命令：

```sh
python3 -m pytest tests/test_review_node_sync.py tests/test_native_agent_storage.py \
  tests/test_native_transcript_detail.py tests/test_zcode_session_binding.py \
  tests/test_session_bridge.py tests/test_session_query_api.py \
  tests/test_native_session_api.py -q
```

```sh
PYTHONPATH=. python3 -m pytest tests/test_session_schema.py \
  tests/test_session_bridge.py tests/test_native_chat_frontend.py \
  tests/test_native_metadata_recovery.py \
  tests/test_native_session_api.py tests/test_session_query_api.py \
  tests/test_agent_lifecycle.py \
  tests/test_session_repository.py tests/test_transcript_repository.py \
  tests/test_transcript_cursors.py \
  tests/test_session_interactions.py tests/test_adoption_control_http.py \
  tests/test_adoption_control_guard.py tests/test_supervisor_control.py \
  tests/test_zcode_session_binding.py tests/test_native_continuation.py \
  tests/test_review_node_sync.py -q
```

此专项还覆盖 Supervisor 消息操作 schema v1→v2 的原子升级、预版本化库初始化、损坏同名表拒绝、NULL token digest 存储约束、已填充表中约束探测的唯一键碰撞，以及既有消息/命令保留；`tests/test_supervisor_control.py` 另通过真实 SQLite 并发让两个不同终态回执竞争，验证只有首个提交被持久化并写入一条终态审计。审查发现、根因和边界见[后端深度 Review](../reviews/2026-11-24-backend-deep-review.md)。

受管续聊的逐轮快照、Windows NTFS ACL 所有者边界、Git 安全读取、SQLite 幂等/迁移、丢包恢复、HTTP 鉴权和原任务结果隔离：

```sh
python3 -m pytest tests/test_continuation_results.py tests/test_task_patch_bounds.py \
  tests/test_task_summary_collection.py tests/test_native_continuation.py \
  tests/test_supervisor_control.py tests/test_native_chat_frontend.py \
  tests/test_release_layout.py -q
```

Windows CI 的原生 Artifact Export probe 验证 NT 固定句柄、junction、ADS、共享写入拒绝和有限读取。`tests/test_continuation_results.py` 覆盖 Administrators/SYSTEM 所有者和 OWNER RIGHTS。真实 NTFS 上的私有结果队列创建、共享写入目录下的源码拒绝和不安全旧队列拒绝，由 Windows CI 显式执行 `tests/fixtures/workflow/windows_artifact_probe.py::test_windows_shared_cache_keeps_results_private`。Windows 上依赖 POSIX `st_uid/getuid` 的 Hub signed-command 测试按平台跳过，由 Linux 和 macOS 执行。

`test_continuation_results.py` 还覆盖 prepared/live/terminal 崩溃草稿、超过一批待恢复记录时的精确会话 fence、cwd/attempt/process-group 变更隔离、执行前磁盘失败、二进制补丁拒绝、不可变 after-tree 测试证据、32 条失败上传后的公平重试，以及 dispatch 默认值/损坏数据和分页索引定义的原子迁移拒绝。浏览器 `continuation-results` 场景通过真实 HTTP/SQLite 在桌面和手机宽度检查按需详情、20+3 分页与暂停/恢复竞态；不运行真实 Agent CLI。

agy 原生 Runner 接入、精确 ID 续聊、可见文本/工具 NDJSON、元数据和消息块身份回归：

```sh
python3 -m pytest tests/test_agy.py tests/test_native_transcript_detail.py \
  tests/test_agent_profiles.py tests/test_agent_capability_layers.py \
  tests/test_release_layout.py \
  tests/test_cross_platform_agents.py tests/test_discovery.py \
  tests/test_session_bridge.py tests/test_review_node_sync.py -q
FLEET_PLAYWRIGHT_MODULE=/path/to/existing/playwright FLEET_BROWSER_CHANNEL=chrome \
  python3 -m pytest tests/test_native_chat_frontend.py -q
```

`results` 浏览器场景经真实 Hub/SQLite HTTP 检查原任务 diff/文件/测试摘要、按需读取、
纯文本 XSS 边界、迟到响应取消及 1440/390px。新节点测试加入 Windows/macOS CI。
测试不运行真实 vendor CLI 或外部模型；安装状态与真实供应商验收单列。

Agy ACP 原生审批回路及旧交互兼容入口：

```sh
python3 -m pytest tests/test_acp_interactions.py tests/test_session_interactions.py \
  tests/test_agy_acp.py tests/test_acp_transport_bounds.py \
  tests/test_acp_lifecycle_races.py \
  tests/test_session_bridge.py tests/test_native_session_api.py \
  tests/test_supervisor_control.py tests/test_supervisor_process.py -q
```

`test_real_acp_permission_http_capture_signed_answer_and_ack` 实际启动 Supervisor
受管 worker 和独立 stdio 对端，经过 RunnerCapture 加密 spool、runner HTTP、临时
SQLite、operator 问题预约、签名 poll、独立恢复的 Supervisor、私有 mailbox 和
ACP 响应写出，再验证成功回执、单条回答事件和恢复运行态。并发和响应丢失只生成
一条命令，普通消息／旧问题回答保持原协议。另验通用 control 接口不能绕过预约、
未协商或非受管来源不能取得审批能力、owner 进程身份不符拒绝、迟到 ACK 不能
声明成功、功能门禁、权限及 SQLite 失败脱敏。以上是本地严格对端证据，不代表
真实供应商 CLI、外部模型或 Windows/macOS 实机验收。

服务动作授权与重放恢复、SQLite owner/idempotency grant 的并发 HTTP 竞争、版本 fencing 和审批消费：

```sh
PYTHONPATH=. python3 -m pytest tests/test_approval_repository.py \
  tests/test_platform_service_actions.py tests/test_platform_service_action_runtime.py \
  tests/test_platform_submit_approvals.py tests/test_platform_submit_http.py \
  tests/test_platform_submit_e2e.py -q
```

`test_concurrent_http_approval_requests_replay_one_grant` 通过真实 HTTP 与 SQLite 同步两个相同请求，验证两个响应都恢复同一待审批 grant、只持久化一条记录；并发批准、consuming 无命令重启恢复、命令已写入但 grant 尚未消费的重启恢复也使用真实临时 SQLite 覆盖。相关根因和红绿证据见[后端深度 Review](../reviews/2026-11-24-backend-deep-review.md)。

Workflow dispatch、lease fencing、源代码产物跨 Task/Workflow 双库提交恢复、review/test 回执丢失和取消/不确定执行恢复：

```sh
PYTHONPATH=. python3 -m pytest tests/test_workflow_dispatch.py \
  tests/test_workflow_coordinator.py tests/test_workflow_control.py \
  tests/test_workflow_task_recovery.py tests/test_workflow_coding_delivery.py \
  tests/test_workflow_review_execution.py tests/test_workflow_test_execution.py -q
```

该组以真实临时 SQLite、受控执行端和故障注入验证持久意图、原执行绑定、迟到回执、崩溃与取消边界；不代表所有 Provider/Runner 与生产环境已完成外部验收。深审范围和具体边界见[后端深度 Review](../reviews/2026-11-24-backend-deep-review.md)。

大文本回归覆盖三万 Unicode 字符的有界分片、工具字段预览的明确截断标记，
以及完整可见工具输出经过真实 RunnerCapture、加密 spool、HTTP 和 SQLite 后
可在 source archive 找回，而普通聊天读取继续排除这些归档行。旧 print 身份
不会因保留 ID 就获得 ACP 续聊能力；未协商 resume 的新会话可完成但不可续聊。

全 Agent 的协议、可见文本增量、工具、原生审批、模型/名称和历史质量证据见
[Agent 观测契约](../agent-observability.md)。`agent_evidence` 为只读追加 DTO，不授予
操作权限；SQLite v6 原子迁移、损坏旧库、HTTP 鉴权、并发事实合并、不可伪造重放及
JavaScript 实际解析入口：

```sh
python3 -m pytest tests/test_agent_evidence.py tests/test_session_repository.py \
  tests/test_agent_capability_layers.py tests/test_native_transcript_detail.py \
  tests/test_native_session_api.py tests/test_session_interactions.py \
  tests/test_frontend_contracts.py tests/test_session_frontend_contracts.py \
  tests/test_boundaries.py tests/test_frontend_separation.py -q
FLEET_PLAYWRIGHT_MODULE=/path/to/existing/playwright FLEET_BROWSER_CHANNEL=chrome \
  python3 -m pytest tests/test_agent_capability_frontend.py \
  tests/test_native_chat_frontend.py -q
```

首次迁移不扫描完整历史，未重放的旧会话显示未知；usage 和内部 reasoning 当前未进入
公开会话契约。专项测试只访问临时 HTTP/SQLite/Node parser，不能代填各平台真实 Agent
及模型验收。浏览器缺依赖或未配置跳过时须另跑有依赖的浏览器门禁。

官方插件及 GitHub CLI 的节点接入、设备码任务与验收边界见[官方插件文档](../platform/official-plugins.md)。

请求级模型、五项 Token、耗时、费用来源、重试与交互验收详见[请求元数据测试矩阵](request-metadata.md)。

侧栏收放归入 `UI-01`：`test_navigation_session_browser[sidebar-motion]` 在真实浏览器中暂停原生过渡，验证侧栏与主内容处于中间位置、同一个切换按钮的纵向位置和尺寸保持一致、快速反向无跳变、收起状态刷新后保留，以及 768/767px 断点、390px 抽屉焦点循环、Esc/遮罩关闭和减少动画偏好。复用现有 UiIcon，不增加动画库或后台接口。

尚未完成的真实浏览器、接管、凭据、Browserbase 与通知工作见[待确认实施决策](browser-notification-proposal.md)；该提案不是已批准规格或通过证明。

Windows / macOS 各 Agent 的安装、执行、原生续聊、模型接管与外部验收边界见[双端 Agent 验收矩阵](agent-platform-acceptance.md)。

电脑 Agent 的网页新建、会话目录、阅读与续聊交互设计见[原生 Agent 网页工作区实施计划](../superpowers/plans/2026-11-21-native-agent-web-workspace.md)。本轮前端实现、公开 API 接点、真实浏览器测试及等待后端的边界见[网页前端接入](../native-web-frontend.md)。新建目录测试使用测试 app 暴露的真实 TaskService；不能据此宣称产品 HTTP 接口或线上节点已验收。

这份文档是测试入口。可执行范围定义在 [journeys.json](journeys.json)；逐阶段断言、审查回归和证据核验见 [test-chains.md](test-chains.md)；外部验收见 [release-acceptance.md](release-acceptance.md)。测试用例、门禁和文档必须随功能一起更新。

## 为什么需要这套门禁

2026-10-03，助手把 `workspace.list` 等内部名称直接发送给 OpenAI 兼容接口。真实上游只接受 `[A-Za-z0-9_-]{1,64}`，返回 400；网关转成 502，用户没有收到回复。旧测试服务也接受带点名称，浏览器测试又直接替换了模型执行器，因此两层测试都没有验证真正失败的边界。

回归原则：测试模型服务独立校验公开协议，不能导入被测序列化器来生成期望值；至少有一条浏览器流程经过真实账号 Cookie、调度器、HTTP 适配器、工具、文件、产物和对话恢复。HTTP 202、`/healthz` 200、`configuration_ready=true` 和大量单元测试通过，都不能代替最终回复及真实产物验收。

## 测试分层

| 层级 | 验证内容 | 外部边界 |
|---|---|---|
| 单元/契约 | 类型、状态机、协议、校验、预算、签名、路径、解析 | 可控输入与故障注入 |
| 集成 | 真实 SQLite、HTTP Hub、账号会话、调度器、provider factory、工具和文件、节点 journal | 外部模型、节点及邮件按用例明确模拟 |
| 浏览器贯穿 | 真实 Chrome/Chromium → 账号登录 → 发任务 → 工具 → 回复 → 预览/下载 → 关闭/刷新恢复 → 失败可见 → 退出 | 模型 HTTP 服务为严格 loopback fixture，浏览器不替换 provider |
| 外部验收 | 每个真实模型、真实收件箱、已启用远程节点、TLS/代理/静态产物和部署回滚 | 必须在相应环境执行，CI 不可代填通过 |

全流程意味着矩阵中的每条能力都有可运行的成功、异常或边界证据，不意味着对所有外部模型的无限输入和未来故障作保证。功能 gate 关闭、外部环境缺失或未执行的项，应写 `not_run` / `blocked` 并记录原因，不能记为通过。

## 一条命令运行完整门禁

需要 Python 3.10+、Node.js 和 Playwright 浏览器。测试依赖与运行依赖分离：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-test.txt
npm install --prefix /tmp/fleet-browser --no-audit --no-fund --package-lock=false playwright@1.63.0
/tmp/fleet-browser/node_modules/.bin/playwright install chromium
FLEET_PLAYWRIGHT_MODULE=/tmp/fleet-browser/node_modules/playwright \
FLEET_BROWSER_CHANNEL=chromium \
FLEET_SCREENSHOTS=/tmp/fleet-test-evidence/browser \
PYTHONPATH=. .venv/bin/python -m pytest tests -q \
  --require-journeys \
  --journey-report=/tmp/fleet-test-evidence/journeys.json \
  --junitxml=/tmp/fleet-test-evidence/results.xml
```

macOS 已安装 Chrome 时可用 `FLEET_BROWSER_CHANNEL=chrome`，其余参数不变。单独运行 `pytest` 可以进行快速局部开发，但缺少浏览器依赖会跳过相关测试，**不能作为发布凭据**。发布只接受带 `--require-journeys` 的完整运行。

局部调试：

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_full_flow.py -q
PYTHONPATH=. .venv/bin/python -m pytest tests/test_platform_provider.py tests/test_platform_provider_canary.py tests/test_provider_transport_boundaries.py tests/test_http_transport.py -q
PYTHONPATH=. .venv/bin/python -m pytest tests/test_account_smtp_transport.py tests/test_journey_gate.py -q
PYTHONPATH=. .venv/bin/python -m pytest tests/test_acceptance_flow.py tests/test_platform_acceptance_check.py tests/test_acceptance_deadline.py -q
# 浏览器测试仍需上面的 FLEET_PLAYWRIGHT_MODULE / FLEET_BROWSER_CHANNEL
PYTHONPATH=. .venv/bin/python -m pytest tests/test_full_flow_browser.py -q
```

## 产品流程目录

每行的具体测试 node ID、验收标准和模拟边界以 [可执行矩阵](journeys.json) 为准；一条测试可为多个流程提供证据，但不会因此变成多个独立执行结果。

| ID | 流程 | 层级 |
|---|---|---|
| AUTH-01 | 登录、页面守卫与退出 | browser |
| AUTH-02 | 邀请注册与邮箱验证码 | integration |
| AUTH-03 | 密码、角色和管理员管理 | integration |
| AUTH-04 | 鉴权、CSRF及限流 | integration |
| MAIL-01 | SMTP与one-mail传输 | integration |
| ASSIST-01 | 浏览器到模型与产物的完整流程 | browser |
| ASSIST-02 | 持久化、幂等和进程重建 | integration |
| MODEL-01 | 工具协议与流式响应 | contract |
| MODEL-02 | 模型异常、超时及错误证据 | integration |
| RUN-01 | 排队与运行中的取消 | integration |
| RUN-02 | 并发、租约和崩溃恢复 | integration |
| CONFIG-01 | 模型工作区选择与配置快照 | integration |
| FILES-01 | 工作区、沙箱和产物安全 | integration |
| USAGE-01 | 用量预算与异常记账 | integration |
| NODE-01 | 远程节点投递、回执和幂等 | integration |
| WINDOW-01 | 执行窗口连接及租约 | integration |
| MEMORY-01 | 记忆管理和显式注入 | integration |
| MONITOR-01 | 监控、健康证据及事件 | integration |
| ACTION-01 | 只读诊断和审批操作 | integration |
| SCHEDULE-01 | 定时任务和重复触发 | integration |
| SESSION-01 | CLI会话采集与补丁交付 | integration |
| SSE-01 | 实时重连、分页与撤销 | integration |
| ADOPT-01 | 会话接管与撤销 | integration |
| BACKUP-01 | 一致备份、加密和恢复 | integration |
| UI-01 | 控制台页面和文本渲染 | browser |
| RELEASE-01 | 发布探活与验收CLI | integration |
| BROWSER-01 | 浏览器离线网络边界 | contract |
| BROWSER-02 | 单次表单提交审批 | integration |
| WEB-AGENT-01 | Muse 本地桥接、签名命令及重复派发保护 | integration |
| MUSE-WORKSPACE | 独立登录态、原生多会话、卡片与审批 | integration |
| GATE-01 | 全流程门禁自身 | contract |

## 新增贯穿测试如何工作

- `tests/support/strict_provider.py` 是独立的 HTTP 协议对端，验证工具名、JSON Schema、历史 tool-call ID、工具结果及认证。它不导入产品 serializer 或工具定义，因而能拒绝本次事故中的错误请求。
- `tests/support/full_flow.py` 启动临时 Hub 和真正的 `start_platform_run_scheduler`。模型凭据是一次性 fixture 值。数据库、工作区、下载内容和邮件记录全部在 pytest 临时目录，未接生产。
- `tests/test_full_flow.py` 从真实 HTTP 登录开始，验证邀请、注册、密码重置、停用、跨用户404、最终回复、产物字节/哈希、重复提交、重新创建 app 后恢复、取消和故障持久化。
- `tests/test_full_flow_browser.py` 不替换模型工厂；页面关闭后释放已阻塞的模型请求，再新开页面验证后台完成。四次工具调用必须留下真实事件，下载文件必须与实际写入内容一致。502 后刷新必须仍显示错误，不能丢掉上一条成功回复。
- `tests/test_account_smtp_transport.py` 进行实际 TLS/STARTTLS、SMTP AUTH 和 MIME 投递；验证不可信证书被拒绝、SMTP DATA 失败不被当作成功或自动重试。测试 CA 只在该 fixture 内受信任。
- `tests/test_acceptance_flow.py` 串联真实账号验收入口、模型 HTTP、节点 HTTP poll/receipt、journal、最终报告与恢复；验证越权 wire 工具没有本地或节点副作用，权限覆盖不能改变冻结策略，Hub 重启后仍只允许 list，已提交响应超时后可通过对话/token 找回唯一 Run。`tests/support/loopback_node.py` 使用产品 NodeClient/Transport 和独立目录，不能代替实际远程主机验收。
- `tests/test_platform_journal_admission.py` 用共享 SQLite 与独立客户端验证崩溃重投、并发准入、迟到回执及终态不可覆盖；阻塞 executor 时另一个命令仍能执行，证明数据库事务没有跨过执行边界。`tests/test_platform_browser_results.py` 验证实际 JSON 字节预算、数值精度、非有限数、Unicode 与 PNG 类型边界；错误结果不得作为成功回执持久化。

- `tests/test_platform_schedule_recovery.py` 验证实际租约到期回收、活跃重复 tick、两实例并发回收、迟到完成不可覆盖、完成时刻与下次执行计算，以及超过目录首页的到期任务发现。全部使用临时 SQLite 和注入时钟，不发送通知或执行外部动作。

- 功能根因回归纳入 `CONFIG-01`、`NODE-01`、`SCHEDULE-01`、`FILES-01` 和 `RELEASE-01`：排队 Run 固定模型地址与凭据引用，最终工作区决定默认节点；定时任务容忍正常轮询延迟，手动触发执行过期任务；节点回执在断网、HTTP 错误、确认丢失和重启后补传，保留原 worker 且不重执行，Hub 已判定的 unknown 保持不变；连续发布和回退源树不重用版本名，跨过中间发布仍能更新；文件覆盖按替换后的实际大小检查配额。测试使用临时数据库、文件、进程回执与注入传输，不证明线上节点或部署已经升级。

- `tests/test_platform_schedule_memory_validation.py` 通过真实 Flask/SQLite 验证字段类型、布尔启用值、Unicode、数值溢出和版本前置条件；非法输入返回稳定 400，且不新增或更新记录。合法布尔值和整数版本的兼容性同时检查。

- `tests/test_task_patch_bounds.py` 将 UTF-8 patch 从认证 runner POST 贯穿至 SQLite 和 operator GET，验证 Hub 再脱敏、100 KiB 内含截断标记、历史行读取边界、非法类型/Unicode 不写入，以及重复处理稳定性。

- `tests/test_task_summary_collection.py` 和 `tests/test_task_summary_contract.py` 验证 20 KiB 文件读取与完整 JSON 存储边界、文件替换/增长时的身份检查和描述符关闭，以及真实 runner HTTP 的非法摘要拒绝。计数必须是非负且不超过 JavaScript 安全整数范围的整数；耗时必须有限且非负。失败名称最多 20 条、每条 200 字符，同时受完整 JSON 的 UTF-8 字节预算限制。缺失文件保持可选，存在但损坏的摘要返回有界错误，不静默当作无测试结果。

- `tests/test_worktree_diff_streaming.py` 用真实 Git 和临时进程验证大补丁采集的 Python 内存上界、stdout/stderr 同时排空、UTF-8 截断、非零退出码，以及管道保持打开或提前关闭时的超时终止和回收。字节限额包含截断标记；此处验证的是采集进程内存，不代表限制 Git 子进程的内部内存。

- `tests/test_workspace_output_bounds.py` 用实际文件和本地进程验证工作区 64 KiB 读取/输出限额在采集时生效，保留非零退出码、stderr、超时和截断信息，并检查无效 UTF-8 替代字符不会突破字节预算。Git、directory 和 sandbox 共用 `fleet_core/process/bounded_process.py` 的非阻塞管道读取及进程组回收；配置 launcher 的测试只是临时对端，不证明生产隔离。

## 门禁判定规则

`BROWSER-01` / `BROWSER-02` 对应 [T3.7 网络边界](../superpowers/specs/2026-10-04-t3.7-browser-navigation-network-boundary.md)和[提交审批契约](../superpowers/specs/2026-10-05-browser-submit-approval-contract.md)。提交贯穿测试使用真实 SQLite、worker、签名、Node journal 和进程内 HTTP，注入模型、DOM driver 及 socket；验证单次授权、并发消费、事务回滚、到期、撤销、跨 Run 拒绝、表单边界与秘密标记排除。TLS 测试仅访问隔离的 loopback fixture。这些证据不代表真实浏览器 adapter、进程网络隔离、审批 UI 或外部站点验收；browser/network/submit 产品开关仍默认关闭。

`tools/testing/pytest_journeys.py` 读取矩阵并检查 **实际收集和执行结果**：

1. 每个必需 selector 至少对应一个已收集的用例。删除/改名/`-k` 排除用例会造成覆盖不完整。
2. 所有参数实例的 setup、call、teardown 必须通过。skip、xfail、xpass、参数部分跳过、直接选择单个参数 node ID、`-k` / `--deselect` 排除参数、清理失败均不算通过。`--lf/--last-failed` 会在正常筛选钩子之前裁剪收集结果，因此与 `--require-journeys` 或 `--journey-report` 同用时明确拒绝；普通局部调试仍可使用它。
3. 任意测试本身失败仍使 CI 失败，即使矩阵中的条目碰巧全绿。
4. 浏览器未安装或配置错误不能悄悄换成无浏览器发布。
5. `journeys.json` 报告保存代码 revision、dirty 状态、矩阵 SHA-256、每个 selector 的收集/通过数和未通过 node ID；不保存 Cookie、验证码、提示词、provider 原始正文或数据库。
6. 报告里的外部检查永远初始化为 `not_run`，必须附加真正的发布验收证据，不能由本地绿色测试推导成功。

CI 在打包前运行门禁，测试失败或不完整时 `package` 不执行。CI 保留 JUnit、流程报告和浏览器截图 7 天。截图只拍临时 fixture 数据；默认不上传网络 trace、登录请求体或邮箱内容。

已发布版本的截图名称、视口、覆盖面及证据边界记录在
[UI 视觉证据](visual-evidence.md)；该页保留耐久 contact sheet 及逐字节核验的全分辨率原图，并标明来源 CI 与版本。

main 的 `deploy` 依赖 `test`、`launch-policy-windows` 与 `package`，通过受限入口部署同一次 CI 的前后端产物；PR 无部署权限。`tests/test_auto_deploy.py` 验证文件/数据库保留、部分失败回滚、任务竞态、归档与 SSH 协议、Nginx 恢复和 workflow 边界，属于 RELEASE-01 必需集合。部署步骤和真实检查边界见[自动部署手册](../../deploy/auto-deploy.md)。

备份 no-replace 发布、父目录 flush 不确定结果、明文/加密恢复和实际 SQLite DDL 回滚检测：

```sh
PYTHONPATH=. python3 -m pytest tests/test_platform_backup.py \
  tests/test_platform_backup_encrypted.py tests/test_review_store_backup_fsync.py \
  tests/test_auto_deploy.py tests/test_review_rollback.py \
  tests/test_deploy_pidfd.py tests/test_guardian_process_control.py \
  tests/test_release_layout.py -q
```

`publish_durability_unconfirmed` 表示目标已原子出现但父目录 `fsync` 失败；先 inspect 现有目标再决定后续动作。自动部署将数据库版本/领域标记与实际 `sqlite_master` 持久对象一并比较；检测到结构变化才把失败库保留到 `failed-databases/` 并恢复部署前快照。该回滚实现位于独立预装 receiver，应用包不会自动升级宿主 receiver。

交付时按[证据核验步骤](test-chains.md#核验-ci-证据)对照最新提交、CI checkout revision、矩阵摘要和 JUnit；Linux CI 要求零跳过。macOS 的 `/proc`/stop-resume 平台跳过须记录原因，由 Linux CI 补齐，不能据此放宽其他用例。

## 故障定位与修复要求

先定位失败层，保留最小证据，再修改：

| 症状 | 首查证据 | 禁止的处理 |
|---|---|---|
| 页面无法登录/退回登录 | `/api/operator/session`、账号接口状态、Cookie/Origin 配置 | 用假 token 或 DEV_OPERATOR 跳过实际账号 |
| 页面显示就绪但没有回复 | Run state、events、provider 请求次数、HTTP status | 把 202 当完成；只看进程健康 |
| 上游 400/502 | 严格对端捕获的请求协议、网关 request ID | 只添加重试掩盖非法 payload |
| 工具已执行但没有最终回复 | 第二轮模型请求、tool-call ID、工具结果、最终消息 | 只验证首轮请求 |
| 刷新后丢失/重复消息 | 持久化 messages/runs/events、游标与幂等键 | 从页面内存推断持久化成功 |
| unknown / 网络超时 | 原 Run/command 的证据与副作用边界 | 自动重放未知结果 |
| 邮件未到 | 发送记录、投递返回和真实收件箱 | 把 provider 接收等同于邮箱收到 |

新缺陷必须变成可复现的测试：先用坏输入或临时移除修复使该用例失败，再恢复修复重新验证。只检查源码包含某个字符串的测试适合约束发布结构，不能作为用户功能的唯一证据。

## 维护矩阵

新增/删除功能时同时更新矩阵、用例、本文或外部验收文档。不要为了让门禁变绿删除失败 selector 或降低预期。改变协议时同步审查独立对端的规范依据；模型 fixture 是协议验证器，不是模型能力评测。外部服务失败必须保留具体失败项和 Run ID；它不应被误归为本地自动化已经验证。


### 集中设置流程 SETTINGS-01

`/settings` 的桌面／手机浏览器流程验证模型源URL与Key保存、刷新读回、旧版本冲突、VM节点与目录工作区登记、默认值选择、返回缓存助手后的目录刷新和需要重启的系统参数。后端补验实际Worker/readiness共用Key解析、Key轮换后旧快照恢复、owner隔离、并发写入和平台未启用时的配置入口。过程使用临时数据库、文件及浏览器，无外部模型调用、云VM生命周期操作或生产配置修改。具体selector见 [journeys.json](journeys.json) 的 SETTINGS-01。

`test_settings_overlay_motion` 在真实 Chrome 中暂停入场动画，验证桌面／手机快速关闭时透明度与位置连续；新分类和缓存分类切换不重播浮层／遮罩入场。另验缓存浮层重新打开、深色遮罩、减少动态效果和退出途中导航不会被旧定时器带回助手页。原有保存流程验证关闭按钮和 Esc 退场后返回缓存助手。

`test_settings_overlay_interactions` 覆盖桌面／手机点击遮罩退场、内容和边缘点击不关闭、跨边缘拖选／往返拖动／右键／取消手势不误关、草稿保留与原入口焦点恢复（移动端隐藏入口回退到导航按钮）。分类切换保留键盘焦点，搜索支持清空、Esc优先清查询、Enter进入首个结果、方向键及Home/End，并忽略输入法组合按键。另验滚动区域隔离、44px手机点击目标、原生焦点约束、深色／减少动态效果和直达设置页关闭；最近会话操作菜单只展开一个，外点／焦点移出／Esc／Tab收起，键盘可操作且Esc优先关闭菜单而不是抽屉。

本轮变更范围、失败证据与验证状态见[设置交互优化计划](../superpowers/plans/2026-11-20-settings-interactions.md)。

MCP 与 Fleet 身份登记浏览器场景同时验证嵌套弹窗操作；MCP 夹具明确检查填写表单、Esc 取消和子窗口关闭后外层设置仍打开。超出设置边界的子弹窗点击不得被当作外层遮罩点击。

设置异步生命周期回归使用真实 Flask/SQLite，并由浏览器暂扣已经提交的响应：节点接入文件在切换分类、关闭设置、替换编辑器、切换配置所属账号以及设置页／助手工作面板缓存淘汰后仍交付一次，退出账号并销毁身份后不得交付。下载文件再经真实 Node 安装器加载，检查新凭据可认证且旧凭据已撤销；不重复生成凭据。系统参数与 hosts 分别覆盖保存成功／失败的迟到响应，验证新草稿和当前提示不被覆盖，并继续保存读回。集成页验证显式重试、重新进入分类、加载中多次切换和后台完成后恢复，健康子面板不得重复挂载。修复范围与证据见[设置生命周期修复计划](../superpowers/plans/2026-11-19-settings-lifecycle-fixes.md)。
