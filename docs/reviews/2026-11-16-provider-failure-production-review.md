# Review Summary

- 基线：GitHub main `193d9b6c7b250a6e5cc74828e83009922d49a373`。检查了传输截止时间、提供方状态分类、SSE解码、observer、Native runtime、worker、租约/取消、请求统计和Run/Conversation历史及前端流式合并。
- 原版本存在4个确认问题，修复前不建议原样合并相关实现：1个P1、2个P2、1个P3。最重要的是流错误被当成功、明确503丢失重试资格，以及展示预算耗尽后失去取消检查点。
- 结构与架构：执行、持久化投影、服务接口和UI已有分层。本次缺陷位于边界契约，修在provider/observer及纯投影层，无需新增重试服务或改写助手页面。
- 扩展性：复用现有错误DTO、限频阶段信号和持久化事件。统一既有HTTP分类，避免正常错误正文和正文读取失败形成两套规则。
- 测试：新失败证据共19个参数实例，随后补充真实HTTP、worker、SQLite保留/重启及现有浏览器成功/流错误双分支。发布以最终精确提交CI原工件为准；本地验证不证明外部API站点已经修复。
- 删除：删除原`_request()`中被统一分类器替代的内联分支，没有确认可以删除的独立文件、依赖或旧数据。
- 范围：API站点内部渠道重试由另一位agent负责；不修改其仓库，不发生产模型请求，不修改设置/凭据，不重放历史Run，不新增模型执行限制。

# Findings

## [P1] SSE显式错误被忽略，部分答案或工具调用仍可提交

- 文件与位置：`hub/platform/providers/openai_compatible.py::OpenAICompatibleProvider._parse_sse`；下游`hub/platform/runtime/native.py::NativeAssistantRuntime.run`。
- 所属维度：正确性、可靠性、状态转换、工具副作用。
- 问题：解析器只处理choices/usage，遇到`{"error":...}`就跳过。随后DONE或已出现的finish_reason可以使部分文字返回final，也能使已累积合法参数的部分工具调用返回tool_call。
- 触发条件：HTTP200流先返回content或工具delta，随后返回显式错误帧，再以DONE/EOF结束；或错误出现在finish_reason之后。
- 实际影响：页面把未完成回答显示为成功；Native runtime可能执行未确认完整的写文件或命令调用。该风险不依赖UI展示方式。
- 根本原因：把HTTP200和流结束标记等同于业务成功，没有识别流内错误。
- 最小修改方案：SSE parser在消费结果字段前识别error，返回稳定`provider_stream_error`；保留白名单错误码、公开增量和关联编号，通过已有unknown路径结束；不自动重放已接受请求。
- 需要补充的测试：文字/工具×DONE/EOF/finish后错误、未知/异常错误结构、真实HTTP、worker零工具派发、历史清理/重启、浏览器流错误刷新恢复。
- 是否阻塞合并：修复前是；本轮已修复，发布仍须通过精确提交门禁。

## [P2] 明确503/429的错误正文读取失败后不再按配置重试

- 文件与位置：`hub/platform/providers/openai_compatible.py::UrllibTransport.request`、`_http_error`、`OpenAICompatibleProvider._request`。
- 所属维度：正确性、重试、可观测性。
- 问题：收到HTTP拒绝响应后，若读取可选错误正文超时或断连，原代码转成不重试的timeout/network_error。相同503因正文是否顺利读完而产生不同重试行为；401等也丢失auth_error分类。
- 触发条件：已收到429/503等拒绝状态和响应头，错误正文未在截止前完成或传输发生错误。
- 实际影响：虽然设置了重试次数，本轮直接退出；运维看到网络错误而非原HTTP拒绝分类。
- 根本原因：可选诊断正文读取结果覆盖已知HTTP语义，分类仅发生在成功读取正文之后。
- 最小修改方案：抽取原有HTTP分类分支供正常响应和错误正文读取失败共用；保留原cause、状态及关联编号。未收到拒绝响应或HTTP200正文中断仍不自动重放。
- 需要补充的测试：TimeoutError/OSError/HTTPException，400/401/403/429/503控制，真实socket正文截止后重试，重试耗尽与成功的worker持久历史。
- 是否阻塞合并：修复前是；本轮已修复，发布仍须通过精确提交门禁。

## [P2] 达到公共正文展示预算后不再观察取消

- 文件与位置：`hub/platform/providers/observation.py::TextObservation.append`；`hub/platform/runtime/native.py::observe_request`。
- 所属维度：可靠性、取消、资源生命周期、架构边界。
- 问题：正文已达到24576字节后，后续文字被裁成空串并立即return，不再产生阶段事件。runtime依靠observer回调检查流式取消，因此持续到达的后续文字失去检查点。
- 触发条件：大段回复超过展示预算、已经标记裁剪，用户随后取消，上游仍连续输出content。
- 实际影响：本可在下一正文心跳停止的读取继续进行，延迟到响应结束或网络截止；后续错误还可能使取消表现成unknown。
- 根本原因：将展示保留预算错误地用于抑制运行活性信号，使UI存储策略影响执行控制。
- 最小修改方案：即使正文不再保留，也继续调用已有的每秒至多一次responding活动信号；runtime照原observer路径观察取消并关闭响应，不引入新线程或轮询器。
- 需要补充的测试：先产生一次真实裁剪，再取消，在下一限频阶段检查点关闭流；断言保留正文、cancelled、单次HTTP和response关闭。
- 是否阻塞合并：修复前是；本轮已修复，发布仍须通过精确提交门禁。取消仍仅在下一流式检查点或在途网络退出时生效，不宣称能取消上游计算。

## [P3] 中断的长流已裁剪但缺少省略提示

- 文件与位置：`hub/platform/providers/observation.py::TextObservation.append`、`hub/domain/run_progress.py::project_progress`、`fleet_core/request_metadata.py::RequestMetadata`。
- 所属维度：交互正确性、数据保留契约、测试。
- 问题：observer在上游预裁剪后只传保留文本；若之后流中断，runtime没有完整assistant_response可补发truncated，下游无法判断文本已被省略。
- 触发条件：超过24KiB的ASCII/UTF-8正文后，连接中断或收到流内错误。
- 实际影响：用户无法区分完整的已收到文字与仅保留的片段；刷新和清理事件后继续缺少提示。
- 根本原因：预裁剪标记只在正常完成的响应事件中传递，异常路径不经过该事件。
- 最小修改方案：首次裁剪时flush已有公开文字，再发送一次带`truncated=true`的零正文增量；纯投影同时消费响应和增量的标记，复用原UI省略提示。
- 需要补充的测试：ASCII/UTF-8×断连/error，显式标记持久化，清理/重启恢复，以及既有浏览器省略提示与工具DOM身份断言。
- 是否阻塞合并：单独非阻塞；随本次根因修复交付。

# Delete List

- `OpenAICompatibleProvider._request`原内联HTTP状态分类分支：由同模块`_http_error`直接替代，供正常错误响应与诊断读取失败复用，避免规则漂移。无兼容包装。
- `_transport_error`的`status`参数：显式HTTP拒绝转到统一分类后，两个剩余调用都只处理没有拒绝状态的传输错误；移除失去调用的参数。
- `tests/test_provider_transport_boundaries.py`的未使用`socket`导入，以及`tests/test_request_metadata_api.py`的未使用`sqlite3`/`PlatformRepository`导入：同文件引用扫描和F401检查确认无使用，随触达测试清理。
- 没有证据充分的废弃文件、依赖、配置、历史记录或测试需要删除。

# Final Verdict

- 原基线需修复前三项后再合并。本轮四项均完成最小根因修复；是否可发布以精确提交CI及部署回执为准。
- 合并前必须满足：原失败回归、提供方/worker/取消/历史/前端分层回归、真实浏览器成功与错误分支，以及完整Linux和Windows发布门禁。
- 可后续处理：外部API站点内部重试修复和真实供应商长时容量验证；不由本仓库隔离测试推导外部稳定性。
- 推荐最小路径：保留既有架构，仅更新错误分类、流内错误识别、正文观察器和纯投影；不新增全局状态、重试层或兼容框架。
