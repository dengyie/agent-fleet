# 每次请求元数据：契约、测试与验收

本页属于[全流程测试入口](README.md)，自动执行范围见[journeys.json](journeys.json)的 USAGE-01、MODEL-02、RUN-02、UI-01；上线按[发布验收](release-acceptance.md)执行。例子中的数量和价格均为测试数据，不是生产定价。

## 数据与界面契约

| 字段/行为 | 精确定义 | 缺失/异常处理 |
|---|---|---|
| 请求身份 | Run 内 `attempt:step:request_index`；step 是模型工具循环，request_index 是该步第几次 HTTP 尝试 | 重试逐条显示，不覆盖失败记录；同一 Run 仍禁止不确定结果自动重放 |
| 模型 | 摘要优先响应中的 model，未返回时显示本次实际请求模型；详情分别标明两者 | 使用冻结配置；修改目录或下次模型选择不会改写历史 |
| 输入 Token | API `input_tokens` 为含缓存输入；紧凑条仅在两项缓存细分均已知时显示扣除缓存后的输入 | 细分缺失时显示含缓存总输入，悬停标签说明口径；详情始终显示总输入 |
| 缓存读取 / 缓存写入 | 输入的子集，分别显示 | 未提供为 `—`；不能用 0 补齐；负数/小数/布尔/字符串/非有限数/超限值拒为未知 |
| 推理 Token | 输出总量的子集 | 未提供为 `—`；大于输出总量时未知 |
| 输出 Token | API `output_tokens` 含推理；紧凑条在推理已知时显示可见输出 `output - reasoning` | 推理未知时标签说明包含推理，不把全部输出称为可见输出 |
| 总 Token | 输入总量 + 输出总量；不重复添加缓存/推理 | 输入/输出不完整时只保留合法的上游总量；不凭文本长度补算 |
| 请求耗时 | provider dispatch 前到完整读取/解析响应后的 `monotonic` 毫秒 | 不包含排队、工具、退避等待；请求中显示 `≈` 实时估算；刷新从服务端 elapsed 恢复，不依赖客户端时钟；缺失结束事件不伪造完成时间 |
| 时间戳 | 请求开始的 UTC epoch，页面显示本地 HH:mm:ss，详情含本地完整日期与 ISO UTC | 与请求结束时间不同；耗时不由系统墙钟相减 |
| 费用 | 优先 `usage.cost` 的 USD 报告值；否则按冻结的配置单价估算，详情显示数量 × 每百万单价 | 缺失为“费用未提供”，真实 0 可显示；估算带 `≈` 和来源，不能冒充最终账单 |
| 价格拆分 | 缓存单独计费，推理随输出计费 | 缺失缓存细分且单价与普通输入不同则不估算；相同单价可合并到输入，详情注明“含同价未拆分缓存”，不造零值 |
| 状态与错误 | HTTP状态、结束原因、响应ID、流式参数、网络超时与请求序号 | 失败/未知保留安全错误码，禁止复制原始错误正文、提示词、headers、URL凭据、secret_ref |
| 历史/取消/崩溃 | start/finish 复用 lease-fenced Run events；Run/Conversation API按 owner投影 | 旧历史没有记录则不造数据；终态中仅有start的记录显示unknown，允许真实请求成功但Run最终取消 |

当前 `usage.provider_requests` 的预算账本仍表示模型循环的逻辑步骤数；HTTP尝试数看 `requests.length`。二者不应混为一个统计。当前 provider 是同步完整响应读取；等待时实时的是耗时和请求状态，不声称已实现逐 Token 网络推送或首 Token 时间。取消会等待已在途网络请求退出，再由运行边界处理；不会把“已请求取消”当作上游已取消。

`provider_config.timeout_s`（默认 60 秒、范围 1–600 秒）是每次 HTTP 尝试的总网络截止时间，覆盖 DNS、逐地址 TCP、代理 CONNECT、TLS、响应头和响应体。持续到达的少量字节不能重置预算；截止中断 socket 并关闭响应与定时器，随后 Run 退出并释放调度槽。所有 3xx（包括相对跳转和 HTTPS 降级）返回 `redirect_rejected`，不能转发 Authorization 或自动重发。生产传输的超时/连接中断不自动重试，因为 POST 可能已被接受；明确 HTTP 429/暂时性错误仍按已有重试配置逐条记录。已接受响应的读取/解析失败始终不重放。

流式传输按可用字节增量读取，收到 SSE `[DONE]` 即关闭响应，不等待 HTTP EOF 或攒满缓冲区。DNS 使用与发布验收共用的有界解析器：同一 host/port 共享正在进行的查询，进程最多 8 个查询；容量耗尽时等待也计入截止时间。系统 `getaddrinfo` 无可移植取消 API，迟到线程只能结束 DNS 解析，不能发送网络请求。没有添加后台 HTTP 执行线程或无界任务队列。

JSON 与 SSE 的费用数字在协议解码时直接进入 `Decimal`，不能先经二进制浮点舍入；例如 `123456.123456123456` 必须原样保留。上游报告费用与配置费率最多 12 位小数、最大 1000000，越界在舍入前判断；对外金额使用十进制字符串。结构化工具参数中的十进制数转换为普通 JSON 数字；转换后非有限的值拒绝执行。

使用现有纯值模块中的 `RequestUsage`、`Pricing`、`RequestCost` 和 `RequestMetadata` DTO 描述字段；请求生命周期各阶段可有字段缺失，历史数据不补造。连接、读取、JSON、SSE 和密钥解析的异常转换保留显式 cause，仅在现有最外层诊断入口记录安全类型与位置；不能记录原始异常正文。

## 单价配置

管理员模型目录的 `provider_config.pricing` 支持严格的 USD 每百万 Token 配置。数字推荐用十进制字符串：

```json
{"currency":"USD","input":"1","output":"6","cache_read":"0.1","cache_write":"1.25"}
```

四个价格必填，必须有限、非负，币种仅 USD；额外键拒绝。这里仅为 fixture，不能直接当生产价格使用。`provider_config` 随 Run 接受而冻结，`requests[].cost.items` 保存计算结果，刷新不读取最新价格重算。价格来源与采集时间登记在运维手册；不要给第三方站点或其他网关模型套用同名价格。

## 自动化矩阵

| 维度 | 场景与断言 | 可执行证据 |
|---|---|---|
| 正常响应 | 独立 Chat Completions fixture 五项 Token、实际/请求模型、USD小额费用、耗时、时间戳准确 | `test_request_metadata_preserves_five_categories_and_provider_cost` |
| 零与缺失 | 显式0保持0；缺失不变0；部分usage、无cost | `test_unknown_usage_is_not_zero_and_each_retry_is_recorded`、`test_invalid_tokens_remain_unknown` |
| 金额精度 | Decimal计算、小额6位以上精度、0费用、非法单价、缺失细分、同价合并 | `test_decimal_pricing_and_missing_detail_are_explicit`、`test_equal_cache_rates_allow_cost_without_inventing_token_counts` |
| 协议金额精度 | JSON/SSE直接解析高精度数字、极小值及0；上限与小数位校验先于舍入；嵌套工具参数保持可JSON序列化 | `test_wire_cost_retains_exact_decimal_digits`、`test_wire_cost_limits_apply_before_any_rounding`、`test_structured_argument_numbers_remain_plain_json_values` |
| 异常根因 | 连接/读取的超时、OSError、URLError；JSON语法、SSE UTF-8、工具参数与巨大指数；密钥解析；显式cause可追踪且日志/事件无私密正文 | `tests/test_provider_contract_fidelity.py`；MODEL-02逐函数门禁 |
| 重定向与认证 | 301/302/303/307/308、HTTPS降级不向目标发送请求；只留一次失败请求记录 | `test_redirect_never_forwards_credentials_or_replays`、`test_https_downgrade_redirect_is_not_followed` |
| 总截止与资源 | 响应头/体滴流准时失败；TLS共享DNS后的剩余预算；未读取即关闭也释放socket/timer | `test_trickle_is_bounded_by_total_deadline_without_replay`、`test_tls_handshake_and_dns_share_deadline`、`test_unread_response_close_reclaims_socket_and_timer` |
| DNS/代理/TLS | DNS饱和最多固定查询数，迟到不能请求；HTTP代理及HTTPS CONNECT认证隔离；证书和主机名校验 | `test_dns_saturation_is_bounded_and_late_resolution_cannot_dispatch`、`test_system_proxy_and_connect_preserve_origin_credentials`、`test_tls_verification_and_shutdown` |
| SSE真实连接 | chunked响应发送DONE但不结束HTTP，客户端立即返回并关闭连接 | `test_chunked_done_finishes_before_http_eof_and_closes_socket` |
| worker闭环 | Broker→Factory→worker日志保留类型/位置而无私密正文；超时后释放全局槽，下一Run成功 | `test_worker_secret_failure_retains_safe_cause_chain`、`test_request_timeout_releases_scheduler_slot_and_next_run_executes` |
| HTTP重试 | 429后成功保存两次开始/结束，各自状态、序号、时间；未隐藏失败 | provider测试 + 真实浏览器测试 |
| 协议和资源 | SSE usage尾帧、UTF-8逐字节切分、末帧无空行、DONE后停止读取并关闭、非完整输出不冒充成功、异常不重放 | `test_sse_*`、`test_stream_failure_is_recorded_closed_and_never_retried`、`test_accepted_nonstream_read_failure_is_closed_and_never_retried` |
| 存储失败 | start持久化失败时不发请求，不因观察器失败触发网络重试 | `test_request_observer_failure_does_not_dispatch_or_replay` |
| 时间 | 墙钟只记时间戳，耗时由独立monotonic clock得到2988ms | `test_request_timing_uses_monotonic_clock` |
| 持久化/配置 | 实际worker → SQLite → Run/Conversation API → 重建app；模型与价格冻结；message/run链接准确 | `tests/test_request_metadata_api.py` |
| 权限/隐私 | 另一owner读取Run/Conversation为404；投影删除raw_body；输出无fixture密钥 | 同上；沿用 AUTH-04 Cookie/CSRF/角色门禁 |
| 查询/历史 | 一次Conversation读取只发一条请求事件查询；20个步骤保持完整，终态pending为unknown | `test_pending_terminal_unknown_and_projection_queries_are_batched` |
| 慢请求/租约 | 同步provider超过5秒TTL仍完成；心跳失败后禁止继续续约，终结线程回收 | `test_slow_provider_keeps_both_leases_alive_and_cleans_heartbeat`、`tests/test_run_lease_heartbeat.py` |
| UI性能 | 120条记录量测创建/更新耗时；无变化更新产生0次DOM修改；全终态时不运行耗时更新 | 同一真实浏览器用例的performanceEvidence |
| UI动态 | 真实Hub/worker/HTTP adapter生成请求记录；活跃数字更新；轮询保留summary节点/展开状态；不重复GET历史 | `test_request_metadata_in_real_browser` |
| UI完成/恢复 | 429、第二次成功、Markdown粗体、五数字47/100/10/14/0、$0.000131、模型/响应ID、刷新后毫秒完全相同 | 同上 |
| 移动/无障碍 | 390px无横向溢出；summary键盘Enter展开；SVG解析/图标尺寸；未知终态不显示请求中；暗色；prefers-reduced-motion无动画 | 同上；原UI-01覆盖焦点/弹层/滚动按钮 |
| XSS | model字段里的img/onerror仅为文本，无元素、无执行 | 同上；保留已有Markdown净化测试 |
| 既有流程 | 登录、提交token幂等、模型工具回合、取消、预算、产物、节点、账号/浏览器完整流程 | 全量27条journey门禁 |

## 运行与证据

```bash
FLEET_PLAYWRIGHT_MODULE=/tmp/fleet-browser/node_modules/playwright \
FLEET_BROWSER_CHANNEL=chromium FLEET_SCREENSHOTS=/tmp/fleet-test-evidence/browser \
PYTHONPATH=. .venv/bin/python -m pytest tests/test_request_metadata.py \
  tests/test_request_metadata_api.py tests/test_provider_contract_fidelity.py \
  tests/test_provider_transport_boundaries.py tests/test_http_transport.py \
  tests/test_acceptance_deadline.py tests/test_run_lease_heartbeat.py \
  tests/test_platform_run_worker.py tests/test_frontend_browser.py -q
```

发布前执行[入口](README.md)的完整 `--require-journeys` 命令。JUnit、journey报告、浏览器截图必须来自发布提交；本地专项不能代替完整Linux CI。正文不登记会自动过期的“全部通过”数字，实际结果见每次交付/CI报告。

## 线上验收与不能由CI证明的项

1. 核对部署提交、前后端清单、公网静态摘要、SQLite完整性及运行配置保留。
2. 真实账号提交一个正常工作任务，模型至少完成一次工具回合和最终回复；每次HTTP请求有独立行，记录模型、五类Token、elapsed、时间与费用来源。
3. 在请求仍运行时刷新；再在完成后刷新，检查持久化结束耗时一致。切换下次模型不改变历史。慢模型超过60秒仍不能出现本地lease_mismatch。
4. 从同一Run的API `requests`对照页面；费用与网关报告或冻结费率人工计算一致。缺字段显示缺失，失败请求不能显示为免费；不把网关内部重试当成本应用能观测的HTTP尝试。
5. 桌面/390px、暗色/亮色、键盘、减少动画、输入多行、滚动离底部、退出后旧Cookie401均核查。保持页面一分钟观察请求频率、主线程卡顿和控制台错误。
6. 超时/网络失败仅做隔离故障注入；生产已失败或unknown的Run保持原证据，不盲目重发。邮件、真实远程节点和其他关闭gate按发布矩阵标记not_run，不能伪称此次已验收。

自动化不证明第三方价格永远不变、供应商报告准确、所有网络下延迟有固定上限，也不证明Safari/Firefox和屏幕阅读器人工体验。已识别的1小时缓存、音视频/图片Token不套用统一文本估价；新供应商usage方言、非USD和多价格阶梯需要独立契约，不用兜底套用文本Token价格。
