# VPS agent 对话采集与反馈

第一版以可见、可回看、能验证的完整对话为目标。任务日志摘要不再作为会话正文。Hub 继续使用既有 operator/runner 认证。

## 数据链路

- 普通 runner 和 managed runner 在摘要截断前接收 CLI 输出，写入本地加密 spool，经 `/api/runner-session-events` 上传到既有会话库。上传线程与 lease heartbeat 分离。
- `source_record` 保存源事件所有字段，不做预览用的路径/值替换。长记录按顺序分片，`is_complete=true` 表示最后一片。相邻片的 text 拼接还原记录；JSON 可能重新序列化，但字段值保留。普通消息和工具卡片仍是规范化预览，源记录是完整性依据。
- Codex exec 的 item 消息、命令执行、MCP 调用、文件修改、网页查询具有对应结构化事件。其它源字段（包括模型、用量、错误或附件）可在源记录中读取，不声称 CLI 未提供的数据已被采集。
- 上传失败保留未确认 spool，后续轮询重传。源记录无法落盘时不能静默成功；runner 中止并报告采集失败。Hub 通过 sequence 确认去重。
- 任务与会话按 machine + attempt_id 关联；普通会话也保留 attempt_id，但不因此虚标为可控制。

## 历史会话同步

Hub 必须启用 `AGENT_FLEET_SESSION_REPOSITORIES_ENABLED=1`。使用同版本的 Hub 和节点代码；旧 Hub 不认识 source_record，不能作为完整采集的接收端。

在 VPS 通过已有 runner.yaml 配置 Hub 地址、机器名、runner credential 文件、项目白名单。用独立同步进程读取用户指定的 JSONL 目录，不占用任务 runner 的执行循环：

```sh
python -m tools.session.sync --config /path/to/runner.yaml \
  --source codex=/root/.codex/sessions \
  --source claude_code=/root/.claude/projects
```

`--once` 单轮同步，默认每 3 秒扫描一次。首次从头分批读取，后续按文件的持久化 byte offset 续读。Codex、Claude 和 Pi 的已知记录可规范化展示；其它 JSONL 源仍保存源记录。该入口不是 Gemini JSON 快照或 OpenCode SQLite 的读取器。

- 原始会话文件需仍存在；无法找回源端已删除、未落盘或未输出的内容。上传失败保留 checkpoint/spool 并报告 pending，单轮命令返回非零；恢复网络后重放。
- 单条 native JSONL 上限 16 MiB；超过或读取出错会报告同步错误，不前移本轮 checkpoint。未实现二进制媒体下载、跨文件 inode 轮转去重或无限保留。
- spool/Hub 既有配额仍生效。`source_record` 不因超配额而被静默当作完整消息；应检查采集错误和 pending 上传日志。

## Hub 查询与页面

`GET /api/sessions/<id>/events?limit=100&after_sequence=<n>` 获取 n 之后的事件。一直读取至空页即可取回全部已保存历史。

会话页提供前后翻页、长消息全文展开、工具完整参数/结果和源记录展开。API 客户端不再截断消息正文。任务页可跳转关联会话。

控制台侧栏的活动历史与归档历史分别读取 owner-scoped 列表。标题通过 PATCH /api/platform/v1/conversations/<id> 修改，归档/恢复使用 /archive 与 /restore。永久删除只能从归档列表执行，并要求操作者在侧栏确认；主助手没有永久删除工具。接口只删除不存在未结束 Run 或未完成/结果未知远端命令的对话。旧命令即使缺少 owner_id，也通过所属 Run 的 owner 与对话关联。删除在单个 SQLite 事务中清理消息、Run、事件、旧任务链接、浏览器与执行窗口记录，以及平台命令、outbox、reconciliation 和 post-check 记录；工作区产物与用量台账继续按各自资源所有权保留。侧栏计算节点区域可用鼠标、触控或键盘分隔条调整高度，尺寸保存在本地浏览器。

## 追加消息

`POST /api/sessions/<id>/messages`，JSON `{ "text": "继续检查测试失败" }`，复用既有签名 supervisor 通道。需要会话已受管、节点 ControlClient 可达，并启用 supervisor 和 append-user-turn。返回 202 仅代表入队，通过 control receipt 和随后对话事件确认执行及回复。

- Codex 续聊使用 `codex exec resume --json <native-id> <text>`。
- Supervisor 保存 thread.started 返回的原生 ID；正常完成标为 completed，与主动终止区分，正常完成后允许下一轮。
- VPS 已有会话需要先纳管，单独同步历史不等于取得可控制的活动进程。原生身份、工作目录或源会话已不存在时不新建一个伪装成原会话的进程。
- 普通 runner 的临时任务仍清理 worktree；查看这些任务的完整对话不代表能在已清理目录继续执行。长驻/纳管会话的续聊单独验收。

## Agent 状态、问题卡片与接力

受管会话的 public_session_dto 暴露 current_state、state_source 和
state_confidence。候选状态来自 Hook、structured stream、native transcript、
PTY 和 Supervisor；合并优先级为 Supervisor、Hook、structured stream、
native transcript、PTY。序号较旧的事件不能覆盖新状态。PTY 静默只保留
unknown 或低置信度 idle。非受管会话不提升为结构化状态，仍保持
best_effort。完整状态变化保留在事件时间线中。

只有 probe 声明 interactive_questions 能力后，interaction_request 才能产生
可回答的问题卡片。回答使用
POST /api/sessions/<id>/messages，请求包含 request_id、client_token、
selected 和/或 text；Hub 校验问题状态、选项及重复提交，并通过现有签名
Supervisor append_user_turn 入队。接口返回 202 表示已入队，实际执行需继续
查看 control receipt 和后续事件。不得直接写 PTY stdin。网络结果不明确时，
使用同一答案和 client token 重试。

会话页可以预览并创建独立任务接力：

- GET /api/sessions/<id>/handoff 只生成确定性脱敏预览，不创建任务。
- POST /api/sessions/<id>/handoff 接收目标机器、Agent、项目、可选补充指令和
  client_token，成功后返回新 task_id、attempt_id、handoff_id。
- 接力快照只汇集有界状态、目标、相对文件路径、测试结果和事件引用；路径越界、
  symlink、密钥文件以及 raw transcript 不进入预览。非受管会话不能创建接力。
- 接力创建须通过现有 task 白名单、人工确认闸和身份边界；重复 token 返回原回执，
  同 token 不同请求返回 409。源会话保持原状态。

## 脱敏 transcript 搜索

operator 搜索入口为 GET /api/session-search，兼容路径为
GET /api/v1/session-search。默认 gate
AGENT_FLEET_SESSION_SEARCH_ENABLED=0，并依赖会话仓库已启用。索引从已持久化的
脱敏事件生成，只纳入有界消息和工具文本；source_record、加密 raw、内部思考和
未脱敏输入不入索引。FTS5 用于常规检索，中文双字查询使用 bigram 辅助表；初始化
失败时只关闭搜索，不影响 transcript 确认。

查询长度为 2–128 个字符，每页最多 50 条，snippet 最多 400 字符；筛选支持机器、
项目、Agent 和 UTC 时间，游标分页保持稳定顺序。结果返回会话、事件序号、高亮及
筛选维度，点击结果会打开会话并定位对应事件。索引写入失败只返回固定诊断，不回滚
已确认的 transcript。历史回填不在本版本范围内；索引按现有 ingest 增量建立。

## 用量看板

operator 用量 API 为 GET /api/platform/v1/usage，按 UTC 日期、项目、机器、Agent
和模型筛选或分组。查询最多覆盖 90 天、最多生成 1000 个桶，成本使用 Decimal
字符串表示。费用来源固定为 provider、configured 或 unknown；Token 不完整、
缺少计价或旧记录无法证明完整时保留 unknown，不以零冒充实际成本。聚合按既有
reservation / usage event 去重，流式 transcript 不重复计费。页面中的配置价格是估算，
不作为账单数据。

## 验证依据

`tests/test_runner_conversation.py` 覆盖完整行、Codex item、长 Unicode 源记录、源字段保留、断线重传去重、原生历史/增量同步、机器绑定、普通任务会话关联、受管会话追加消息和完成后原生身份保留。

真实浏览器测试验证 205 条事件翻页与 5000 字符正文。平台运行条件见 [平台部署说明](platform-availability.md)。
