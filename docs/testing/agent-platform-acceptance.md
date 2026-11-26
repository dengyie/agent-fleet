# macOS / Windows Agent 适配验收

开发边界与扩展入口见 [Agent 分层架构](../agent-capability-architecture.md)。

本矩阵将本地执行、原生会话续聊、桌面桥和模型接管分别验证。`generic` 仅观察，不提供通用执行或桌面点击能力。

## 能力与安装边界

| Agent | macOS | Windows | 原生续聊 / 采集 | 平台目录模型接管 |
|---|---|---|---|---|
| Google Antigravity (`agy`) | CLI 1.5.14；T3-style ACP runtime shipped in signed client s31; real model call pending | CLI 1.5.18 installed and SHA-512 verified; current Fleet process discovery awaits normal process refresh | `agy acp`，原生精确 `session/resume`；消息增量、工具、原生权限、后台结算 | 仅使用原生协商的模型/模式；不声明第三方目录接管 |
| Codex | 已安装 CLI | 已安装原生 exe | JSON stdout；同一 thread ID | 支持 Responses 兼容配置 |
| Claude Code | 已安装 CLI | 已安装原生 exe | stream-json；同一 session ID | 支持 Anthropic 兼容配置 |
| Pi | 已安装 CLI | 已安装 Node 包 | print/json；精确 session ID/path | 保留本机 provider/model，不声明目录接管 |
| ZCode | 桌面包内 Node CLI | 桌面包内 Node CLI | 精确 sess ID；CLI 只读 SQLite；桌面桥读取消息页，并在 idle 时从精确会话只读 SQLite 有界补齐已完成工具链 | 保留原 provider，不声明目录接管 |
| Hermes | 已安装 CLI，需显式配置 | 当前验收机没有 CLI | 精确 ID/cwd 验证；只读 SQLite；任务私有配置固定 cwd | 保留原 provider，不声明目录接管 |

Windows Hermes 的缺失是安装状态，不记为通过。存在 `.hermes/skills` 不足以证明 CLI 可执行。不会为了验收修改全局模型默认值或自动下载大软件。

## 实现契约

- Windows 默认 npm 命令只解析已知安装布局中的原生 exe 或 Node 脚本，指令始终作为单个 argv；不经 cmd.exe。自定义命令保留其原义，危险 batch 输入继续拒绝。
- Pi 只投影完整消息，忽略 thinking 和增量事件；`tool_execution_end` 与 `message_end(toolResult)` 同时出现时只采集后者。
- 原生续聊身份、Node 脚本前缀、provider/model/policy 参数、工作目录与增量游标仅存节点私有记录；不能以最新会话猜测原会话。
- ZCode/Hermes SQLite 只读，先核对 ID 和精确 cwd；读取有行数、字节数、字段大小和时间上限。异常保留旧游标并记录 capture gap。
- ZCode 桌面消息分页继续提供用户/助手文本；idle 时最多读取 256 条精确 session 的 SQLite part 行，只有 completed/error 工具部分进入 `tool_call`/`tool_result`。回归覆盖分页、重放去重、隐藏 reasoning 排除和路径脱敏；工具历史不是实时流。
- 各 Agent 的私有 `thinking`/`reasoning` 不是公开会话内容；采集可见回答、结构化工具调用/结果和公开状态事件，不上传隐藏推理链。
- ZCode/Hermes 原生 CLI 续聊仅对持有 `native_resume` 能力的 Runner 自有会话开放；普通采集元数据、接管会话不据此获得续聊权限。ZCode 桌面桥保留其独立能力路径。
- Hermes 在任务私有 home 快照 provider/auth，固定工具 cwd；不修改用户原始配置。任意附加 JSON 文件不能证明 Hermes 可续聊。
- 不持久化 ephemeral 会话的续聊能力；上游失败、未知投递与未安装状态均保留真实结果，不能自动重建替代任务。

## 自动化与真实验收

Linux 完整发布门禁继续执行真实 HTTP/SQLite、浏览器与 journey 验证。Windows gate 增补新适配与续聊测试；新增 macOS Agent gate，生产部署依赖三端门禁。

新增 `test_cross_platform_agents.py` 使用真实子进程、SQLite、加密 spool 与 Hub HTTP 接收链路，验证 Pi/ZCode/Hermes 工具事件唯一入库和原生身份恢复。其他回归覆盖 Windows argv、Unicode、进程发现、探测、配置、模型绑定和续聊竞争。

真实模型验收使用唯一 FIRST/FOLLOW 标记：FIRST 必须实际读取 UTF-8 文件；新 Supervisor 从持久记录恢复后，FOLLOW 在相同 native ID 回忆原口令且不调用工具；文件字节不变。证据保存在节点私有 `~/.cache/agent-fleet/verification/cross-platform-agents-20261121`，不把凭据或私有配置提交到仓库。Mac Pi/ZCode/Hermes、Windows Pi/ZCode 已执行；agy 仅完成协议 fixture、Supervisor 与 Hub/SQLite 验收，尚未执行真实模型调用；最终生产验收以部署后的独立回执为准，单元测试不代填外部模型通过。

Windows 测试用短 `--basetemp`，避免未启用 Win32 长路径的系统将测试临时目录的长文件名拒绝误归因于会话逻辑；不更改用户机器全局注册表。POSIX 权限位断言仅在 POSIX 执行，Windows 仍执行私有目录下的写入、恢复和身份验证。


### agy 新增边界（2026-11-23）

新开发依据固定 T3 Code commit 的 ACP 实现，默认 `agy acp`；使用独立字面协议
peer、真实 worker/Supervisor、RunnerCapture、Hub HTTP/SQLite 和签名控制客户端
验证完整权限回答。覆盖 Unicode/旧 stdio 编码、断线、认证/恢复失败、帧限制、
stderr 饱和、取消、提前 prompt 结果和权限/ACK 并发。原生协商允许列表内的
模型/模式可在 worker 发 prompt 前选择，且私有续聊记录保留该选择。

Windows 结果文件读取复用已有 pinned NT handle reader，拒绝 reparse/ADS 和
读取时替换；接受当前用户、SYSTEM 或 Administrators 为所有者的正常私有目录，并将
OWNER RIGHTS ACE 仅映射到经验证的所有者；其它 SID 的危险写入权限仍拒绝。macOS/POSIX
使用 no-follow 目录描述符。Windows 的 POSIX Hub command-store 回归清楚标记平台跳过，
不以弱化运行时文件权限检查规避测试。新增专项进入三端 CI；CI 通过和真实安装/模型验收
是不同证据，不能互相替代。

尚未执行新 ACP 对真实 Agy 模型的调用、Windows Agy 登录/授权、旧桌面历史导入和
usage/费用投影。Windows `home-win` 的官方 AMD64 CLI 1.5.18 已安装于用户级
`%LOCALAPPDATA%\agy\bin`，其 SHA-512 与官方 manifest 相符；未登录、未发起模型调用。
Fleet 的常驻任务未重启，需待其正常进程刷新后复核 AGY 发现状态。运行中 steering、跨空闲轮次的常驻 ACP host 尚未开放；当前
连接保持到本轮原生请求/后台工具结算，后续轮次精确恢复同一 native session。
浏览器可以替代操作界面，VM 仍需 Agent 引擎、登录状态和 Fleet 节点。


### PR120 production record (2026-11-24)

PR [#120](https://github.com/dengyie/agent-fleet/pull/120) merged as
`c0b5d071e4e3540cf54b212cb44e8efe77126e98`; main CI run
[45258361764](https://github.com/dengyie/agent-fleet/actions/runs/45258361764)
passed test, Windows, macOS, package and deploy jobs. macOS and Windows nodes now
run the signed 199-file client `c0b5d071e4e3-s31` (sequence 31), with their
previous s30 releases retained. No vendor binary was installed or model called
as part of this release. The Windows ACP peer exercises the adapter contract; it
does not establish Windows Agy installation or authentication.
