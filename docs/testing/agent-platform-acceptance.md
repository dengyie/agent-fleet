# macOS / Windows Agent 适配验收

本矩阵将本地执行、原生会话续聊、桌面桥和模型接管分别验证。`generic` 仅观察，不提供通用执行或桌面点击能力。

## 能力与安装边界

| Agent | macOS | Windows | 原生续聊 / 采集 | 平台目录模型接管 |
|---|---|---|---|---|
| Codex | 已安装 CLI | 已安装原生 exe | JSON stdout；同一 thread ID | 支持 Responses 兼容配置 |
| Claude Code | 已安装 CLI | 已安装原生 exe | stream-json；同一 session ID | 支持 Anthropic 兼容配置 |
| Pi | 已安装 CLI | 已安装 Node 包 | print/json；精确 session ID/path | 保留本机 provider/model，不声明目录接管 |
| ZCode | 桌面包内 Node CLI | 桌面包内 Node CLI | 精确 sess ID；只读 SQLite 消息与工具；桌面桥独立 | 保留原 provider，不声明目录接管 |
| Hermes | 已安装 CLI，需显式配置 | 当前验收机没有 CLI | 精确 ID/cwd 验证；只读 SQLite；任务私有配置固定 cwd | 保留原 provider，不声明目录接管 |

Windows Hermes 的缺失是安装状态，不记为通过。存在 `.hermes/skills` 不足以证明 CLI 可执行。不会为了验收修改全局模型默认值或自动下载大软件。

## 实现契约

- Windows 默认 npm 命令只解析已知安装布局中的原生 exe 或 Node 脚本，指令始终作为单个 argv；不经 cmd.exe。自定义命令保留其原义，危险 batch 输入继续拒绝。
- Pi 只投影完整消息，忽略 thinking 和增量事件；`tool_execution_end` 与 `message_end(toolResult)` 同时出现时只采集后者。
- 原生续聊身份、Node 脚本前缀、provider/model/policy 参数、工作目录与增量游标仅存节点私有记录；不能以最新会话猜测原会话。
- ZCode/Hermes SQLite 只读，先核对 ID 和精确 cwd；读取有行数、字节数、字段大小和时间上限。异常保留旧游标并记录 capture gap。
- ZCode/Hermes 原生 CLI 续聊仅对持有 `native_resume` 能力的 Runner 自有会话开放；普通采集元数据、接管会话不据此获得续聊权限。ZCode 桌面桥保留其独立能力路径。
- Hermes 在任务私有 home 快照 provider/auth，固定工具 cwd；不修改用户原始配置。任意附加 JSON 文件不能证明 Hermes 可续聊。
- 不持久化 ephemeral 会话的续聊能力；上游失败、未知投递与未安装状态均保留真实结果，不能自动重建替代任务。

## 自动化与真实验收

Linux 完整发布门禁继续执行真实 HTTP/SQLite、浏览器与 journey 验证。Windows gate 增补新适配与续聊测试；新增 macOS Agent gate，生产部署依赖三端门禁。

新增 `test_cross_platform_agents.py` 使用真实子进程、SQLite、加密 spool 与 Hub HTTP 接收链路，验证 Pi/ZCode/Hermes 工具事件唯一入库和原生身份恢复。其他回归覆盖 Windows argv、Unicode、进程发现、探测、配置、模型绑定和续聊竞争。

真实模型验收使用唯一 FIRST/FOLLOW 标记：FIRST 必须实际读取 UTF-8 文件；新 Supervisor 从持久记录恢复后，FOLLOW 在相同 native ID 回忆原口令且不调用工具；文件字节不变。证据保存在节点私有 `~/.cache/agent-fleet/verification/cross-platform-agents-20261121`，不把凭据或私有配置提交到仓库。Mac Pi/ZCode/Hermes、Windows Pi/ZCode 已执行；最终生产验收以部署后的独立回执为准，单元测试不代填外部模型通过。

Windows 测试用短 `--basetemp`，避免未启用 Win32 长路径的系统将测试临时目录的长文件名拒绝误归因于会话逻辑；不更改用户机器全局注册表。POSIX 权限位断言仅在 POSIX 执行，Windows 仍执行私有目录下的写入、恢复和身份验证。
