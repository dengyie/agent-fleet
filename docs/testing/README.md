# 全流程测试与质量门禁

这份文档是测试入口。可执行范围定义在 [journeys.json](journeys.json)；外部验收见 [release-acceptance.md](release-acceptance.md)。测试用例、门禁和文档必须随功能一起更新。

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
PYTHONPATH=. .venv/bin/python -m pytest tests/test_platform_provider.py tests/test_platform_provider_canary.py -q
PYTHONPATH=. .venv/bin/python -m pytest tests/test_account_smtp_transport.py tests/test_journey_gate.py -q
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
| GATE-01 | 全流程门禁自身 | contract |

## 新增贯穿测试如何工作

- `tests/support/strict_provider.py` 是独立的 HTTP 协议对端，验证工具名、JSON Schema、历史 tool-call ID、工具结果及认证。它不导入产品 serializer 或工具定义，因而能拒绝本次事故中的错误请求。
- `tests/support/full_flow.py` 启动临时 Hub 和真正的 `start_platform_run_scheduler`。模型凭据是一次性 fixture 值。数据库、工作区、下载内容和邮件记录全部在 pytest 临时目录，未接生产。
- `tests/test_full_flow.py` 从真实 HTTP 登录开始，验证邀请、注册、密码重置、停用、跨用户404、最终回复、产物字节/哈希、重复提交、重新创建 app 后恢复、取消和故障持久化。
- `tests/test_full_flow_browser.py` 不替换模型工厂；页面关闭后释放已阻塞的模型请求，再新开页面验证后台完成。四次工具调用必须留下真实事件，下载文件必须与实际写入内容一致。502 后刷新必须仍显示错误，不能丢掉上一条成功回复。
- `tests/test_account_smtp_transport.py` 进行实际 TLS/STARTTLS、SMTP AUTH 和 MIME 投递；验证不可信证书被拒绝、SMTP DATA 失败不被当作成功或自动重试。测试 CA 只在该 fixture 内受信任。

## 门禁判定规则

`tools/testing/pytest_journeys.py` 读取矩阵并检查 **实际收集和执行结果**：

1. 每个必需 selector 至少对应一个已收集的用例。删除/改名/`-k` 排除用例会造成覆盖不完整。
2. 所有参数实例的 setup、call、teardown 必须通过。skip、xfail、xpass、参数部分跳过、清理失败均不算通过。
3. 任意测试本身失败仍使 CI 失败，即使矩阵中的条目碰巧全绿。
4. 浏览器未安装或配置错误不能悄悄换成无浏览器发布。
5. `journeys.json` 报告保存代码 revision、dirty 状态、矩阵 SHA-256、每个 selector 的收集/通过数和未通过 node ID；不保存 Cookie、验证码、提示词、provider 原始正文或数据库。
6. 报告里的外部检查永远初始化为 `not_run`，必须附加真正的发布验收证据，不能由本地绿色测试推导成功。

CI 在打包前运行门禁，测试失败或不完整时 `package` 不执行。CI 保留 JUnit、流程报告和浏览器截图 7 天。截图只拍临时 fixture 数据；默认不上传网络 trace、登录请求体或邮箱内容。

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
