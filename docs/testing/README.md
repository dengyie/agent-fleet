# 全流程测试与质量门禁

请求级模型、五项 Token、耗时、费用来源、重试与交互验收详见[请求元数据测试矩阵](request-metadata.md)。

尚未完成的真实浏览器、接管、凭据、Browserbase 与通知工作见[待确认实施决策](browser-notification-proposal.md)；该提案不是已批准规格或通过证明。

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

- `tests/test_platform_schedule_memory_validation.py` 通过真实 Flask/SQLite 验证字段类型、布尔启用值、Unicode、数值溢出和版本前置条件；非法输入返回稳定 400，且不新增或更新记录。合法布尔值和整数版本的兼容性同时检查。

- `tests/test_task_patch_bounds.py` 将 UTF-8 patch 从认证 runner POST 贯穿至 SQLite 和 operator GET，验证 Hub 再脱敏、100 KiB 内含截断标记、历史行读取边界、非法类型/Unicode 不写入，以及重复处理稳定性。

- `tests/test_task_summary_collection.py` 和 `tests/test_task_summary_contract.py` 验证 20 KiB 文件读取与完整 JSON 存储边界、文件替换/增长时的身份检查和描述符关闭，以及真实 runner HTTP 的非法摘要拒绝。计数必须是非负且不超过 JavaScript 安全整数范围的整数；耗时必须有限且非负。失败名称最多 20 条、每条 200 字符，同时受完整 JSON 的 UTF-8 字节预算限制。缺失文件保持可选，存在但损坏的摘要返回有界错误，不静默当作无测试结果。

- `tests/test_worktree_diff_streaming.py` 用真实 Git 和临时进程验证大补丁采集的 Python 内存上界、stdout/stderr 同时排空、UTF-8 截断、非零退出码，以及管道保持打开或提前关闭时的超时终止和回收。字节限额包含截断标记；此处验证的是采集进程内存，不代表限制 Git 子进程的内部内存。

- `tests/test_workspace_output_bounds.py` 用实际文件和本地进程验证工作区 64 KiB 读取/输出限额在采集时生效，保留非零退出码、stderr、超时和截断信息，并检查无效 UTF-8 替代字符不会突破字节预算。Git、directory 和 sandbox 共用 `tools/bounded_process.py` 的非阻塞管道读取及进程组回收；配置 launcher 的测试只是临时对端，不证明生产隔离。

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

main 的 `deploy` 依赖 `test` 与 `package`，通过受限入口部署同一次 CI 的前后端产物；PR 无部署权限。`tests/test_auto_deploy.py` 验证文件/数据库保留、部分失败回滚、任务竞态、归档与 SSH 协议、Nginx 恢复和 workflow 边界，属于 RELEASE-01 必需集合。部署步骤和真实检查边界见[自动部署手册](../../deploy/auto-deploy.md)。

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
