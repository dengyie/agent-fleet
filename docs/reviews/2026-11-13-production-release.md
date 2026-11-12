# Review Summary

- 范围：当前分支相对main `301e259`的Workflow、执行/恢复、输入/产物、配置、发布链路及自动摘要增量。核查调用链、类型、事务、权限、前端状态和测试，不以旧CI替代当前版本。
- 合并建议：下列复现问题已修复，进入完整同版CI；正式合并还需Linux/browser、Windows门禁和实际receiver核对。
- 最重要的三个问题：领域reader升级遗漏于回滚判断；provider重试破坏摘要一次请求；旧终态item解锁不确定的新请求。
- 架构与扩展性：保持`hub -> fleet_core <- tools`，Workflow复用原Run/Command/Node；生成和来源记录使用原SQLite事务，发布复用摘要CAS。Native/外部消费者完整消费schema2版本，没有新队列、框架或运行依赖。
- 测试：22模块458passed/2passing subtests，部署回滚48passed，集合独立记录。覆盖真实桌面/390px页面、原provider/worker、检查点、owner隔离、CAS、备份和第129条历史损坏。完整当前CI及生产验收待实际结果。
- 废弃内容：仅移除确认无读取者的变量、重复更新及未使用导入，不保留兼容空壳。

返回[开发账](../platform/development-completion.md)、[Workflow入口](../platform/workflow-platform/README.md)及[摘要计划](../superpowers/plans/2026-11-13-model-summary-generation.md)。

# Findings

## [P1] 领域reader升级后回滚无法启动旧代码（已修复）

* 文件与位置：`deploy/auto_release.py`的数据库兼容性判断。
* 所属维度：可靠性、兼容、数据恢复。
* 问题：仅比较基础库版本，遗漏独立领域schema/reader。
* 触发条件：基础版本仍为3、Workflow reader15升到16，随后公网验证失败。
* 实际影响：旧代码恢复后仍拒绝新reader，原故障模拟为`rollback_failed`。
* 根本原因：多个兼容性权威被简化为一个基础版本。
* 最小修改方案：比较基础与六个已知领域schema/reader，排除运行数据；恢复兼容备份时保留失败库和新增写入。
* 需要补充的测试：实际回滚/旧代码启动/失败库留存、首次摘要和MCP领域创建、运行元数据变化不恢复数据库；新增与原48项已通过。
* 是否阻塞合并：修复前阻塞，部署前须同步宿主receiver。

## [P1] 一次摘要Run可以隐式请求模型两次（已修复）

* 文件与位置：`workflow_summary_generation_repository.py::create`。
* 所属维度：正确性、幂等、成本。
* 问题：一次Native step未约束provider HTTP重试。
* 触发条件：目录配置`max_retries=1`且首次HTTP503。
* 实际影响：原ProviderFactory/transport记录两次请求。
* 根本原因：模型step与真实请求数不相同。
* 最小修改方案：生成Run固定OpenAI-compatible snapshot使用`max_retries=0`并纳入digest，保留普通目录设置。
* 需要补充的测试：retryable response只请求一次、pending/response恢复不重发；已通过。
* 是否阻塞合并：修复前阻塞。

## [P1] 旧结果解锁不确定的新请求（已修复）

* 文件与位置：`workflow-summary-generation.js`的submit/`sync`。
* 所属维度：异步状态、重复执行。
* 问题：新pending指针仍与旧terminal item共同驱动按钮。
* 触发条件：已有发布结果，第二次提交已接受但浏览器收到503。
* 实际影响：可以换新token重复创建模型任务。
* 根本原因：请求身份切换未清空旧结果。
* 最小修改方案：保存新指针时清空item/result，仅恢复原token。
* 需要补充的测试：真实页面接受回执改503、跨工作区恢复同Run且POST不增加；桌面/手机已通过。
* 是否阻塞合并：修复前阻塞。

## [P2] 可展示正文不等于可发布版本（已修复）

* 文件与位置：生成仓库`_value`、摘要仓库`publish_generated`及HTTP错误映射。
* 所属维度：边界、错误转换。
* 问题：候选遗漏版本元数据预算；空结果返回通用400。
* 触发条件：8000-byte正文加provenance超过8192，或模型仅返回空白。
* 实际影响：页面提供无法完成的发布操作，错误与候选不一致。
* 根本原因：展示和发布的有效性标准不同。
* 最小修改方案：展示前验证完整版本，发布再次核对实际revision/time，使用稳定超限/空结果错误且不截断。
* 需要补充的测试：候选/发布一致拒绝并保留原Run；已通过。
* 是否阻塞合并：现已修复。

## [P2] 内部会话污染普通列表（已修复）

* 文件与位置：`platform_db_conversations.py::list_conversations`。
* 所属维度：数据展示、职责。
* 问题：生成输入JSON进入普通收件箱及来源选择。
* 触发条件：创建生成请求后读取列表。
* 实际影响：内部执行对象被当作用户会话展示。
* 根本原因：列表未识别生成Run权威。
* 最小修改方案：通过runs/生成记录的索引关系排除，专用API仍可恢复，不按名字隐藏。
* 需要补充的测试：活动/归档列表排除内部会话，同前缀普通会话可见；已通过。
* 是否阻塞合并：现已修复。

## [P2] 较早成功来源无法选择（已修复）

* 文件与位置：`workflow-summary-generation.js::loadTurns`。
* 所属维度：功能、分页。
* 问题：只读取最近20turns。
* 触发条件：所需Run在较早页面。
* 实际影响：已有成功结果无法生成摘要。
* 根本原因：未消费原`before_sequence`游标。
* 最小修改方案：手动加载较早页面并保持选择，切换来源/工作区重置游标。
* 需要补充的测试：22-turn真对话选择最早Run且加载不丢已选项；已通过。
* 是否阻塞合并：现已修复。

## [P2] 手机详情横向溢出（已修复）

* 文件与位置：`integrations.css`的摘要详情网格。
* 所属维度：实际可用性。
* 问题：长SHA撑宽auto列，详情子元素达到438px。
* 触发条件：390px手机发布后查看详情。
* 实际影响：操作区超出视口。
* 根本原因：隐式列保留min-content宽度。
* 最小修改方案：使用`minmax(0,1fr)`和标识换行，fieldset允许收缩。
* 需要补充的测试：真实发布/恢复页面无横向溢出并留截图；已通过。
* 是否阻塞合并：现已修复。

# Delete List

* `workflow-summaries.js`的`pendingPublish`：只有赋值、无读取，已删除。
* release layout重复`allowed.update`：前一条已加入相同条目，已删除。
* 回滚测试未使用的`Path`导入：静态检查确认无调用，已删除。
* 旧内部`schema_version` helper：两个调用迁移到完整fingerprint，没有兼容包装。

没有确认应删除的历史迁移或公共兼容API。

# Final Verdict

已确认缺陷完成根因修复。剩余最小路径为固定源码、公开短历史整树核对、完整同版CI、实际receiver同步、main正式自动部署、线上真实模型/任务/产物/恢复验收。不能用本地子集、healthz或历史绿色检查代替；无需求支撑的扩展不据此添加框架。
