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

## 追加消息

`POST /api/sessions/<id>/messages`，JSON `{ "text": "继续检查测试失败" }`，复用既有签名 supervisor 通道。需要会话已受管、节点 ControlClient 可达，并启用 supervisor 和 append-user-turn。返回 202 仅代表入队，通过 control receipt 和随后对话事件确认执行及回复。

- Codex 续聊使用 `codex exec resume --json <native-id> <text>`。
- Supervisor 保存 thread.started 返回的原生 ID；正常完成标为 completed，与主动终止区分，正常完成后允许下一轮。
- VPS 已有会话需要先纳管，单独同步历史不等于取得可控制的活动进程。原生身份、工作目录或源会话已不存在时不新建一个伪装成原会话的进程。
- 普通 runner 的临时任务仍清理 worktree；查看这些任务的完整对话不代表能在已清理目录继续执行。长驻/纳管会话的续聊单独验收。

## 验证依据

`tests/test_runner_conversation.py` 覆盖完整行、Codex item、长 Unicode 源记录、源字段保留、断线重传去重、原生历史/增量同步、机器绑定、普通任务会话关联、受管会话追加消息和完成后原生身份保留。

真实浏览器测试验证 205 条事件翻页与 5000 字符正文。平台运行条件见 [平台部署说明](platform-availability.md)。
