# Model summary generation contract

返回[Workflow 入口](../../platform/workflow-platform/README.md)、[实施计划](../plans/2026-11-13-model-summary-generation.md)及[原摘要合同 §5.6](2026-10-20-workflow-execution-contract.md#56-显式版本摘要上下文2026-11-11)。本合同扩展原摘要权威；不是已经完成或上线的声明。

## User flow and authority

管理员在摘要管理页选择工作区、来源对话及1–16个已成功的运行，明确选择启用模型，输入摘要ID/标题并生成候选。来源必须属于同一owner/workspace/conversation；完整用户输入和完整成功回复作为参考资料，一次规范输入最多49152 UTF-8 bytes，过大直接拒绝，不用预览或截断代替。

服务端在原SQLite写事务里检查来源、工作区/会话、模型和摘要head的expected_epoch，保存来源选择/摘要目的/完整输入的hash、固定provider配置及凭据引用，并创建独立对话、user message和原queued Run。原Run队列/worker负责模型调用；HTTP操作、只读检查、构造service与Hub启动不调用模型。每个生成Run仅一次模型步、无工具、固定本地provider、原取消/lease/checkpoint/unknown恢复语义；不调用MCP/文件写入/任意CLI，不使用另一队列或线程。

页面显示queued/running/succeeded/failed/unknown/cancelled，重载通过run_id或原client_token查回。响应不确定保留相同请求和token，不自动换token或重发模型调用。用户查看完整候选后明确发布；发布只接受既有生成Run标识，内容由持久成功结果取出，不从客户端接受任意“模型生成”正文。人工编辑后的内容继续走原operator发布入口。未知/失败/取消/仍在执行的Run不能发布；source_kind不等于验证或人工接受。

## Durable generation and source closure

生成请求使用原owner/client_token幂等，固定workspace/summary/title/expected_epoch/model_profile/source_run_ids及规范摘要。相同请求返回原Run；内容变化冲突。接收和发布均在原Workflow admission open下进行；已有回执查回优先，workspace或模型后来停用不重建原请求。queued开始模型I/O前重查当前workspace、来源对话与模型启用、原Run lease、原生成绑定和完整输入hash。

摘要domain旧schema1/reader1及三个原表保持兼容。首次生成才在同一事务创建`workflow_summary_generations`和`workflow_summary_generation_sources`，把摘要meta提升schema2/reader2，并通过max把Workflow minimum reader提高至16。两个新表只记录不可变元数据、原Run引用和有序来源引用，完整正文留在原message。外键和显式guard保护生成及来源Run/对话，不允许原retention或归档删除破坏恢复和来源证据。构造service或空只读列表不建表，不升级reader。

snapshot/历史/备份验证包含原生成记录、Run/message/config的完整绑定、规范输入hash和来源引用闭包，按rowid每批128条有界检查；即使Workflow gate关闭也执行。缺表、schema/reader倒退、错误scope、损坏/超大JSON、丢失来源、非规范body或配置漂移拒绝。明文及加密恢复继续frozen，保留旧Run的unknown语义。

## Generated summary version

原operator摘要的schema1 canonical bytes、hash、API和Native/外部私有请求保持原样。生成摘要采用schema2和`source_kind=model`，保留全部原字段并增加唯一字段`generation`：

```json
{
  "run_id": "summary-run-example",
  "source_run_ids": ["source-run-example"],
  "source_sha256": "64 lowercase hex characters",
  "model_profile_id": "model-a",
  "provider_digest": "64 lowercase hex characters"
}
```

上述字段只能来自原生成绑定，provider_digest覆盖固定provider/model/config/capabilities/secret引用且不公开其值。完整生成版本规范字节仍最多8192；模型回复超出剩余空间时拒绝发布并显示可理解的错误，不截断或用不完整hash覆盖。一次生成只发布一次不可变版本；重试返回原版本。新版本发布仍检查原expected_epoch和head enabled状态；并发人工版本或停用会产生冲突，不覆盖用户后来写入。

Native/外部选择仍是精确summary ID/revision/hash，读取schema1和2都把完整版本纳入原8192-byte逻辑预算；二进制及其他context范围不由生成能力扩展。普通Workflow/Run DTO、错误、事件和repr不附私有来源正文或模型配置；专用owner摘要API才显示完整候选与来源引用。

## Required evidence

真实SQLite红绿验证必须覆盖创建与Run原子回滚、来源scope/状态/大小、同token重复及冲突、固定模型/输入、实际worker一请求/零工具、取消、ready/pending恢复、生成版本来源不伪造、首次/第二次发布及CAS、reader/schema/历史损坏、retention保护、旧operator精确bytes、Native/外部消费以及明文/加密frozen备份。真实浏览器覆盖选择/生成/查回/发布/历史、错误与身份/工作区切换。正常Hub装配和WindowsCI选择新后端模块；真实商业模型和生产试点另留实际证据，不由受控HTTP或CI代填。
