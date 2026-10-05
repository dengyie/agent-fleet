# 发布后的外部验收

请求级模型、五项 Token、耗时、费用来源、重试与交互验收详见[请求元数据测试矩阵](request-metadata.md)。

本地 [流程矩阵](journeys.json) 全绿后仍须验证外部边界。本页定义可重复的验收步骤和必需证据；环境对应的部署目录、凭据位置和回滚操作以该环境运维手册为准，不能复制历史机器路径直接执行。

## MODEL-LIVE

`tools.platform.acceptance_check` 使用真正的账号登录 Cookie。它会为选中的每个模型新建一个带“发布验收”标题的对话，并且每个模型只提交一次任务。专用 `POST /api/platform/v1/conversations/<id>/acceptance-turns` 入口将只允许 `workspace.list` 的策略固定到 Run 快照；Worker 只声明该工具，本地和远程 Broker 在执行或派发命令前拒绝其他工具。任务要求列举指定工作区顶层内容；验收结果还会检查所有实际工具结果是否都是成功的 `workspace.list`。

在专用验收账号和明确指定的工作区执行。只读权限由服务端执行，不依赖模型遵守提示词。验收入口不接受扩大权限的覆盖项，也不能用普通任务的幂等键复用其 Run。旧版服务没有该入口时，CLI 失败退出，不回退到普通任务入口。文件名也可能包含敏感信息，仍应使用专用验收目录。

凭据放权限 `0600` 的本地 JSON 文件，格式为 `{"username":"测试账号","password":"测试密码"}`，或使用 `email` 字段。不要把密码写进命令行、仓库、截图或报告。默认验证 HTTPS；仅 loopback 可使用 HTTP，跳转不会携带登录凭据自动跟随。

```bash
PYTHONPATH=. python -m tools.platform.acceptance_check \
  --endpoint https://fleet.example.com \
  --credentials-file /private/path/acceptance-account.json \
  --workspace acceptance \
  --all-models \
  --expected-release FULL_RELEASE_SHA \
  --report /private/path/evidence/model-acceptance.json
```

只验证明确指定的模型时重复 `--model PROFILE_ID`，不要同时传 `--all-models`。`--timeout` 默认每模型 150 秒，范围 1–600 秒；同一截止时间覆盖创建对话、提交、状态轮询、事件读取和对话恢复。DNS 等待、逐地址 TCP 连接、TLS 握手、响应头和响应体读取均使用剩余预算，缓慢传输不能重置预算；截止后返回成功终态仍视为未确认。与 provider 共用有界 HTTP 截止机制；同一 host/port 的阻塞 DNS 查询共享一个后台解析线程，全进程最多 8 个查询，等待容量也计入预算。超时后的 DNS 结果不能建立连接或发送请求；连续验收失败不会不断新增线程。验收客户端仍直连配置的 origin，不使用环境代理。登录、目录预检和最终退出校验独立于每模型预算。选项全部显式提供；该工具不从源码读取固定管理员密码，不推断生产域名，也不配置模型、用户或工作区。

每个模型必须同时满足：

1. release manifest 与指定版本相符；账密登录、服务端会话校验和 readiness 成功。
2. 指定模型/工作区确实在当前账号目录中。
3. 创建 Run 后轮询终态为 `succeeded`；202 接收或 `running` 均不算通过。
4. 真实 `workspace.list` 的 tool_result 为 succeeded；没有其他工具调用结果。
5. 最终回复非空，重新读取对话时 Run、结果和终态一致。
6. 退出后，用刚才的旧 Cookie 访问会话接口必须返回 401。

任意模型失败，整体进程退出码为 1，并逐模型输出 `status/error/run_state/conversation_id/run_id`（以已经获得的信息为准）。报告不含回复全文、文件列表、密码或 Cookie。超时记录 `error=acceptance_timeout`、`outcome=unconfirmed`，保留已知 Run ID；提交响应丢失时保留对话 ID 与 `client_token` 以便查询定位。`unknown` 和超时不自动重发；超时不代表 Run 已取消或失败。完成后保留报告和对话证据，按指定验收数据保留策略清理。

### 不确定结果的只读定位

原验收报告保留为失败/未确认，不因后台稍后完成而改写成当时通过。按下列顺序查询，另存带时间的定位结果：

1. 已有 `run_id`：使用同一验收账号打开对应对话，或 GET `/api/platform/v1/runs/<run_id>` 和 `/events`，确认当前终态、工具结果和最终回复。
2. 只有 `conversation_id` 和 `client_token`：GET `/api/platform/v1/conversations/<conversation_id>`，在 `messages` 中查找相同 `client_token`。CLI 为每个模型创建新对话且只提交一次，因此仅有一条匹配的用户消息和一个 Run 时，可以唯一定位该 Run。公开 Run 对象不包含 `trigger_message_id`，不要依赖该内部字段。
3. 零匹配表示尚未确认提交；多个 Run 表示该对话另有操作，不能猜测对应关系。保留证据继续核查，不重新调用验收命令来替代查询。
4. 没有对话 ID 时只能记录创建结果未确认，结合服务端请求日志定位；不要编造 Run ID。`unknown` 需要依据原命令/事件检查副作用，不能自动重放。
5. 确认后退出账号。仅保存模型 ID、对话/Run ID、状态、检查时间和有无回复/工具证据，不复制文件列表、回复正文或 Cookie。

这条链路由 `tests/test_acceptance_flow.py::test_accepted_response_timeout_recovers_unique_run_by_token` 验证：真实 Hub 已保存 Run 后阻塞响应，CLI 截止返回未确认；GET 找回唯一 Run，新 Hub 完成原任务，没有第二次提交。其余慢响应、DNS/TCP/TLS 回归及 CI 报告核验见[测试链路文档](test-chains.md)。

## MAIL-LIVE

SMTP/one-mail 对端测试验证协议，不能证明真实邮箱收到了邮件。发布时使用指定的验收邮箱与两个独立浏览器会话：

1. 管理员从账号页建立邀请；记录邀请操作成功时间和验收账号标识，不记录验证码。
2. 无邀请邮箱请求注册应有统一公开响应，但不得真的收到可注册验证码。
3. 受邀邮箱请求注册码；必须在邮箱中实际看到邮件，核对收件人、注册用途和有效时间。
4. 错误验证码拒绝；正确码注册成功；同一码重复使用失败。
5. 两个浏览器会话用账密登录；个人资料修改后重新载入仍保留。
6. 修改密码后两个旧会话均失效，新密码可登录，旧密码不可登录。
7. 实际接收找回密码邮件，重置后旧会话/旧密码失效；验证码不可复用。
8. 管理员停用账号后现有会话和新登录均拒绝；测试结束保留脱敏结果、邮件 Message-ID 或送达时间，删除临时密码和验证码。

收码失败标记 `blocked` 并保留发送状态；不要把发送 API 的 200 当成收件成功，不要对不确定投递自动换服务重复发信。CI 的 SMTP 测试验证 TLS、STARTTLS、认证、MIME、证书校验、拒绝及不自动重试；邮箱的反垃圾和投递链路属于本项。

## NODE-LIVE

只对已启用的实际执行能力验收，不为测试擅自打开生产 gate：

1. 在目标节点运行真实 Probe，核对节点身份、版本、能力、心跳和授权工作区。
2. 在一次性工作区提交只读任务，检查 node command → poll → journal → 执行 → receipt → Run 终态、最终回复和事件链。
3. 在一次性文件路径执行经批准的写入/产物任务，下载字节及 SHA-256 必须与文件一致；重复投递不能再次产生副作用。
4. 断开页面重连后恢复结果；撤销节点凭据后新 poll/receipt 拒绝。
5. 在隔离环境模拟进程崩溃/网络中断；已执行但回执未知的命令必须保持 unknown，不能自动重放。
6. 如启用执行窗口，验证真实终端/浏览器附件的连接、单写入租约、关闭与权限撤销。仅窗口控制 API 测试不证明 PTY 可用。

未安装、未配置、未启用的能力记 `not_run`，列出 gate 或环境原因。报告明确节点版本和能力，不把另一节点通过复制为全节点通过。

## DEPLOY-LIVE

main 的 CI 在测试与打包成功后可按[自动部署手册](../../deploy/auto-deploy.md)完成代码/前端切换、账号探针与失败回滚；`check` 模式只验证部署前条件。自动探针没有执行下面的真实模型、邮件、远程主机与隔离回滚演练，不能将它们代填为通过。

- 使用 CI 产生的前后端产物，核对 RELEASE_ORIGIN、静态 manifest 和内容摘要；运行时数据库、凭据和配置保留。
- 上线前停写并通过 SQLite backup API 保存一致快照；隔离恢复副本验证 integrity/FK、账号、对话和产物一致性。
- 检查 `/healthz` 无凭据可用；匿名功能 API 401；普通用户不能调用管理员 API；合法账号登录后可用。
- 公网 TLS、静态模块 MIME/缓存和反向代理头配置正确；新浏览器上下文验证登录页及成功对话恢复。
- 只有一个受控 Hub/guardian；存活检查不依赖管理员登录态，不会因 401 重启为缺配置的进程。
- 模型验收按 MODEL-LIVE 的每个已启用模型执行；邮件、节点按 MAIL-LIVE/NODE-LIVE 留下独立结论。
- 在隔离环境演练回滚并核对版本与数据。生产失败时按环境手册回滚代码，不能用恢复旧数据库掩盖新用户数据。

## 发布结论格式

必须附四类证据，不能只写“测试全绿”：

| 项目 | 必需内容 |
|---|---|
| 本地/CI | commit、工作区是否dirty、矩阵摘要、每个流程结果、JUnit和截图位置 |
| 模型 | 每个 profile_id 的 Run ID、工具结果、最终回复存在、恢复结果、失败项 |
| 邮件/节点 | 实际环境、检查时间、独立通过/未执行/阻塞原因 |
| 发布 | 前后端版本、校验、备份/回滚证据、真实页面恢复 |

有未执行的外部项时，描述为“CI 流程通过，以下外部项尚未验收”。出现失败时明确哪些能力不可交付；不要把 `not_run` 改名成通过来凑全覆盖。
