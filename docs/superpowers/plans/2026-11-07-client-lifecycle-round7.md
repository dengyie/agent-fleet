# 节点客户端生命周期与协议协商（第七/八轮）实施方案

> 状态：draft，待确认。参考对象：本机 agentone（aione v0.2.151）的工程设计；
> 不参考其 MITM / 账号池 / 订阅流量接管能力。基线 main = `28a6c10`。

## 0. 现状证据（决定了优先级）

| 问题 | 证据 |
|---|---|
| 客户端升级全手工，每轮都要做 | KB「agent-fleet 部署与运维」r1–r6 每轮都有「三个 plist 改指 `client-releases/<sha>` + 备份 + bootout/bootstrap」；bootstrap 曾报 `5: Input/output error` 需重试 |
| 客户端制品没有定义 | `client-releases/<sha>/` 是整树 git archive（14M，含 `hub/ tests/ frontend/ hosts.yaml fleet-gates.conf`）；`CLIENT_ORIGIN.json` 只有 `{revision, source}`，无签名、无校验 |
| 客户端实际闭包很小 | `tools/ fleet_core/` 只依赖 `connectors agent_profiles report_schema session_schema platform_schema` + `requirements.txt` |
| runner 被停时会中断任务 | `tools/agent-runner.py` 无 SIGTERM 处理（只捕获 KeyboardInterrupt :639）；launchd stop → 进行中的 attempt 直接被杀，靠租约过期重派 |
| 无单实例保护 | runner / control-client 没有进程锁；手工 `--once` 调试与 LaunchAgent 并跑会双认领 |
| 锁不可诊断 | `spool.py` `.owner.lock` / `.machine-quota.lock`、`muse.py` `*.lock` 只有 flock，看不出持有者 |
| 进程身份分散 | `process_group.process_start_identity`（Linux /proc、Win creation time、mac 走 `ps lstart` 秒级）、discovery `started_at`、容器 pidfd 各自实现 |
| 节点协议无版本 | `/api/commands/poll` 只带 `runner_id`；ingest 无 client 版本；r5 spool v2 回退、r6 删 alias 都靠「保留一版」猜兼容 |
| 已有可复用的密码学基础 | Ed25519：hub `load_supervisor_signing_key`、节点 `runner_config._load_supervisor_public_key`；`cryptography>=42` 已在依赖中；`auto_release.tree_digest` 已有树摘要 |

## 1. 目标与非目标

**目标（第七轮）**：节点客户端「一条命令升级、可验证、可恢复、可回退」，消灭每轮手工改 plist。
**目标（第八轮）**：hub↔节点协议版本 + 能力协商；节点上报实际运行代码摘要，验收自动化。
**非目标**：Windows 节点自升级（第七轮只做 macOS LaunchAgent + Linux systemd --user，Windows 保持手工，第九轮评估）；hub 远程强制推送（先做 notify，不默认 auto）；日志加密；诊断自动上传（明确不做，保持 opt-in）。

## 2. 第七轮工作包

依赖顺序：WP0 → WP1 → WP2 → WP3 → WP4。每个 WP 一个提交，最后一个 PR。

### WP0 统一身份原语 `fleet_core/identity.py`

- `ProcessIdentity(pid: int, start_marker: str)`
  - `current()`、`of(pid) -> ProcessIdentity | None`、`is_alive_same() -> bool | None`（None=无法判断，调用方按「视为存活」保守处理）。
  - start_marker 格式统一 `<os>:<value>`：`linux:<starttime ticks>`（现有）、`windows:<creation filetime>`（现有）、**`macos:<sec>:<usec>`**（新：ctypes `sysctl(KERN_PROC_PID)` 读 `kp_proc.p_starttime`，失败回退现有 `ps lstart`，回退值加前缀 `macos-ps:` 以免与精确值误比）。
  - `process_group.process_start_identity` 改为薄包装，保持返回值兼容（现有 adoption/discovery 存量数据不受影响 —— mac 旧值仍走 `ps` 分支，新值只用于新写入的 owner/journal）。
- `FileIdentity(dev, ino, size, mtime_ns, sha256)` + `DigestReceipts`（JSON 缓存，key=stat 元组，stat 不变不重算 sha256；对齐 agentone `executable-digest-receipts.json`）。
- 测试：pid 复用模拟（不同 start_marker 判不同）、mac 精确/回退前缀不互等、receipts 在 mtime/size/inode 变化时失效。

### WP1 带持有者信息的锁 `fleet_core/locking.py`

- `OwnedLock(path, purpose)`：flock/msvcrt 取锁成功后原子写 `<path>.owner.json`
  `{schemaVersion:1, purpose, pid, startMarker, nonce, acquiredAt, lock:{dev,ino}}`；释放时先删 owner 再解锁。
- `inspect(path) -> Absent | Held(owner) | StaleOwner(owner)`：试锁成功但 owner 文件存在 = 上一代持有者已死（flock 随进程释放），仅元数据残留。
- `reclaim(path)`：**只清理 StaleOwner**（锁可取 且 owner 进程 identity 不再存在）；Held 一律拒绝。不提供强制解锁。
- 迁移：`spool.acquire_owner_lock` / `.machine-quota.lock` / `MuseBridge._store` 改用 OwnedLock（锁文件名不变，兼容旧进程并存：旧进程不写 owner，inspect 显示 `Held(owner=unknown)`）。
- **新增单实例锁**：`~/.local/state/agent-fleet/locks/{runner,control-client,updater}.lock`，runner/control-client 启动即取，取不到 exit 0 并打印持有者（launchd KeepAlive 下不会抖动：第二实例只有手工启动才会出现）。
- 测试：并发取锁、持有者被 kill 后 inspect=StaleOwner、reclaim 拒绝 Held、Windows msvcrt 路径进 `launch-policy-windows` job。

### WP2 签名的客户端发布制品

**制品**：`deploy/package-client-release.sh <out> <commit> <sequence>`
- 内容 = 客户端闭包：`tools/ fleet_core/ connectors/ agent_profiles.py report_schema.py session_schema.py platform_schema.py requirements.txt` + `CLIENT_RELEASE.json`。
- 可复现：`git archive` → 统一 mtime=提交时间、owner 0/0、排序、`gzip -n`。同一 commit 两次打包字节一致（CI 断言）。
- `CLIENT_RELEASE.json`：`{schemaVersion:1, version:<commit>, sequence:<int>, channel:"stable", files:{path: sha256}}`。

**清单**：`client-manifest.json` + `client-manifest.sig`
```json
{"schemaVersion":1,"channel":"stable","sequence":42,"version":"<commit>",
 "publishedAt":"…Z","expiresAt":"publishedAt+30d",
 "artifact":{"name":"agent-fleet-client-<commit>.tgz","size":123,"sha256":"…"},
 "requires":{"python":">=3.11","protocol":1},"keyId":"client-release-2026-11"}
```
- 签名 = Ed25519(canonical JSON：`sort_keys, separators=(',',':'), ensure_ascii=False`)。
- `sequence` = 新 CI 步骤从「上一个已发布清单的 sequence + 1」得出（存 hub 侧），不用 run_number（换 workflow 会重置）。
- **签名密钥**（决策点 D1）：新密钥 `CLIENT_RELEASE_SIGNING_KEY`，放 GitHub `production` environment，只在 deploy job 可见；**与 hub 的 supervisor/platform 签名密钥分离** —— hub 被攻破也无法向节点下发可执行代码。节点 pin 公钥（`~/.config/agent-fleet/client-release-public-keys.json`，支持多 keyId 以便轮换）。
- 现 `package` job 不碰凭据的约束保持：package 只产出未签名制品，签名在 deploy job 内完成。

**分发**：随后端 release 一起送到 hub
- release bundle 增加 `client/`（tgz + manifest + sig）；`auto_release.py` MANAGED 加 `client` 并在 `validate_stage` 校验签名可被 pin 公钥验证。
- ⚠️ **涉及宿主 receiver 变更**（决策点 D3）：按 KB 约定，receiver 不随 release 更新，合并后需单独对齐 `/usr/local/lib/agent-fleet-deploy/auto_release.py`（备份 `*.bak-<date>-r7`），属运维操作需单独同意。
- hub 路由（`hub/http/client_release_routes.py`，`@require_runner`）：
  - `GET /api/client/releases/<channel>/manifest` → manifest + sig（不缓存）
  - `GET /api/client/releases/artifacts/<sha256>` → 制品（按内容寻址，immutable 缓存）
  - hub 不做任何签名，只是搬运；校验全在节点。

### WP3 节点更新器 `tools/client_update/` + CLI `tools/fleet-client.py`

**目录布局**（一次性迁移后不再改 plist）
```
~/.local/share/agent-fleet/client/
  releases/<version>/          # 只读解包（0555），含 CLIENT_RELEASE.json
  current -> releases/<version>   # 原子切换：symlink 写临时名后 rename
  previous -> releases/<version>
  state/installed.json         # {version, sequence, channel, keyId, artifactSha256, installedAt}
  state/manifest-observation.json  # {sequence, version, publishedAt, expiresAt, manifestSha256}
  transactions/<txid>.json
```
plist / systemd unit 指向 `client/current/tools/...`，以后升级 = 切 symlink + 重启服务。

**`update` 状态机**（每步先写 journal 再执行；journal 原子写 + fsync）
1. `fetched`：拉 manifest+sig → 验签（pin keyId）→ `sequence > observed.sequence`（防回滚；相等且 version 相同=已最新）→ `now < expiresAt`（防重放旧清单）→ `requires` 满足。
2. `staged`：下载制品 → size/sha256 校验 → 安全解包到 `releases/.staging-<txid>`（拒绝绝对路径、`..`、symlink/hardlink、设备文件；复用 `auto_release.safe_extract` 规则）→ 逐文件对 `CLIENT_RELEASE.json.files` 校验 → `python -m compileall -q` + import 冒烟（`tools.runner_config`、`tools.supervisor.control_client`）→ rename 为 `releases/<version>`。
3. `draining`：向 runner 发 drain（见下），等待进行中 attempt 结束（上限 = 租约时长，超时不强杀，update 中止并保持旧版本）。
4. `switched`：`previous`←旧 current；`current`←新版本（原子 rename）。
5. `restarted`：`launchctl kickstart -k gui/<uid>/<label>`（不再 bootout/bootstrap，规避 error 5）；systemd 用 `systemctl --user restart`。
6. `verified`：60s 内三项全部满足：probe kickstart 后 ingest 返回 200；runner/control-client 单实例锁持有者的 start_marker 为新进程；新进程上报的 client.version == 目标版本（WP4）。
7. `committed`：写 installed.json / manifest-observation.json；保留最近 3 个版本，其余 gc。
- 任一步失败：到 `switched` 之前 → 丢弃 staging，什么都不动；`switched` 之后 → 切回 previous + 重启 + 再验证，journal 记 `rolled_back`。
- `recover`：读未终结 journal，按上面规则前滚或回滚；只动 `client/` 下自己记录过的路径，不认识的文件原样保留，不 kill 任何进程（对齐 agentone `profile recover` 语义）。

**runner drain + SIGTERM**（修现有缺陷）
- runner 安装 SIGTERM 处理：停止拉新任务，当前 attempt 继续跑完并回传 result，然后 exit 0；二次 SIGTERM 或超过租约才放弃。
- drain 请求 = 文件 `client/state/drain-<label>`（含 txid）；runner 每轮 poll 前检查。control-client 同理（停 poll，等 SessionBridge flush）。
- 原约束「重载前确认无子进程」由 drain 机制保证并写入 journal 证据。

**CLI**（`python3 client/current/tools/fleet-client.py <cmd>`，中英文输出、默认安静，`--json`）
- `status`：installed vs 各服务实际运行版本（单实例锁 owner + 进程 identity）、最新可用 sequence、是否落后。
- `check` / `update [--dry-run] [--channel]` / `recover` / `rollback`（本地切回 previous，不联网；会在 observation 记录「人工回退」，下次 update 仍只接受更大的 sequence）。
- `locks [--reclaim-stale]`：列出所有 OwnedLock 状态。
- `migrate-layout`：一次性把现有 plist 改指 `client/current`（plist 先备份 `.agent-fleet-backup-<date>-r7`，`plutil -lint` 通过才替换，确认无子进程后 bootout/bootstrap 一次）。

**触发方式**（决策点 D2）
- 第七轮：仅本地手动 `fleet-client update`；hub UI 显示「有新版本」。
- 第八轮可选：`client.update` 走现有已签名的平台命令通道，由 control-client 拉起**脱离自身进程组**的 updater（避免重启 control-client 时把 updater 一起杀掉）；节点策略 `manual | notify | auto`，默认 `notify`。

### WP4 版本可见性（为验收和第八轮铺路）

- 新增 `fleet_core/client_headers.py::client_headers()`，收敛现有 4 处各写各的请求头（`agent-self-report.py:77`、`agent-runner.py:148`、`control_client.py:781`、`control_client_cli.py:113`，UA 都硬编码 `/1.0`），统一输出 `User-Agent: agent-fleet-<role>/<commit>` + `X-Fleet-Client: version=<commit>; seq=<n>; channel=stable`（来自 `CLIENT_RELEASE.json`，开发树运行时 `version=dev`）。
- hub：`require_runner` / `require_ingest_token` 解析后写 `machines.client_version/client_sequence/client_seen_at`（SQLite 迁移 + 回滚脚本）；`/api/status` 机器摘要带上；前端机器卡片显示版本，落后时标黄。
- `report_schema` 允许列表加 `client` 字段（只允许 version/sequence/channel 三个键）。

## 3. 第八轮：协议协商与运行时自证

### WP5 `fleet_core/protocol.py`

- `PROTOCOL_VERSION = 1`；`CAPABILITIES = frozenset({...})`，命名 `<feature>_v<N>`，首批：`spool_v2`、`runner_drain_v1`、`client_update_v1`、`owned_locks_v1`、`platform_command_sig_v1`、`adoption_source_control_v1`。
- 请求头 `X-Fleet-Protocol: 1; caps=a,b,c`（由 `client_headers()` 统一注入）；hub 解析到 `g.client_caps`，并落 `machines.client_caps`。
- 服务端规则：
  - 需要某能力的命令只发给声明了该能力的节点；否则返回结构化 `unsupported_capability`，前端提示「节点版本过低」。
  - `protocol < MIN_SUPPORTED` → 426 + `{minProtocol, upgrade:"fleet-client update"}`，节点日志给出明确升级提示。
  - **删除接口/升级格式的数据门槛**：`hub/application/compat_service.py` 提供「过去 N 天内仍声明/使用旧能力的活跃节点」查询；删 alias、停 v1 格式前跑这个查询，替代「保留一版再删」的猜测。

### WP6 运行时代码自证

- probe 上报 `runtime: {releaseVersion, treeDigest, pythonExeSha256}`：treeDigest 对 `client/current` 闭包计算（DigestReceipts 缓存，stat 不变不重算，probe 120s 周期开销可忽略）。
- hub 对比 manifest 中记录的 tree digest：不一致 → 机器状态 `client_tampered`/`client_dev`，前端标红。验收从「人工 ls client-releases + 看 plist」变成看一个字段。

## 4. 第九轮候选（待评估，不在本次范围）

- task-runner 每任务独立配置槽（agentone `slot-N` + 基线摘要检测漂移）。
- schedule：`preview`（cron 展示后 N 次）、`run-now`（不改周期）。
- Windows 节点自升级（计划任务 + `current.txt` 指针）。

## 5. 测试与验收

**单测/集成**（新增约 120 例，全部进 CI）
- 签名：篡改 manifest 任一字节、错误 keyId、过期、sequence 回退、sequence 相等版本不同、制品 size/sha 不符 → 全部拒绝且不改动 `client/`。
- 解包：路径穿越、symlink、hardlink、超大文件、文件清单不符。
- 崩溃注入：在状态机每一步后 `os._exit`，再跑 `recover`，断言终态只有「旧版本完好」或「新版本已验证」两种。
- drain：进行中 attempt 收到 SIGTERM 后 result 照常回传；二次 SIGTERM 行为。
- 锁：OwnedLock 跨进程、StaleOwner 回收、Windows msvcrt。
- 制品可复现：同 commit 打两次 sha256 相同。
- 协议：缺头的旧节点仍可用（protocol 视为 0、caps 为空，第八轮之前不拒绝）。

**线上验收（mac-local）**
1. 合并部署后 hub `GET /api/client/releases/stable/manifest` 200，签名可验。
2. 宿主 receiver 对齐（单独同意后执行）。
3. `fleet-client migrate-layout`（唯一一次改 plist）→ 三服务常驻、probe ingest 200。
4. 再合并一个小改动 → `fleet-client update` → journal 走到 committed；UI 显示新版本；runner 期间有任务时验证 drain 不丢结果。
5. `fleet-client rollback` 演练一次再 update 回来。
6. KB 更新：部署手册把「手工改 plist」替换为 `fleet-client update`；路由表指向新 SOP。

## 6. 需要你拍板的决策点

| # | 问题 | 推荐 |
|---|---|---|
| D1 | 客户端发布签名密钥放哪 | 新独立 Ed25519 密钥，放 GitHub `production` environment，仅 deploy job 可见；与 hub 签名密钥分离 |
| D2 | 更新触发方式 | 第七轮只做本地手动 `fleet-client update` + hub 显示可更新；远程触发放第八轮且默认 `notify` |
| D3 | receiver 改动 | 同意在合并后单独对齐 `auto_release.py`（备份 + 逐字节比对 main），沿用 r6 流程 |
| D4 | 节点范围 | 第七轮 macOS + Linux(systemd --user)；Windows 保持手工 |
| D5 | 一次性迁移 | 同意 `migrate-layout` 改一次 plist 指向 `client/current`，此后不再改 |

## 6.1 已定决策与实现偏差（2026-11-07 落地）

用户指示「签名密钥不换，开发迭代速度优先」，实际实现与上表推荐的差异：

- **D1 → 复用 supervisor Ed25519 key**：清单签名带独立域前缀 `agent-fleet/client-manifest/v1\n`，
  与 supervisor 命令签名不可互换；节点用 runner.yaml `supervisor.public_key_file` 验签。无 key 时清单接口 503 fail-closed。
- **WP2 → hub 自打包，不改 CI / receiver**：`ClientReleaseService` 从 hub 自身代码树取客户端闭包，
  生成可复现 tar.gz（固定 mtime/uid、排序成员），序号持久化在 `<root>/var/client-releases/state.json`，
  闭包 digest 变化才 +1（hub-only 改动不触发全舰队重启）；同 commit 脏树版本名追加 `-s<seq>`。
  因此 D3 不需要 receiver 改动，部署后自动生效。
- **WP4 → 无独立 `client_headers.py`**：头部与上报字段在 `tools/client_update/runtime.py`；hub 侧只做
  `report_schema.sanitize_client` 白名单 + `/api/status.client_release`，前端机器卡片显示版本徽标，落后标黄。
- **锁的 `__enter__` 可重入**：`with OwnedLock(...).acquire(timeout=...)` 合法。
- **已知限制**：更新器复用 runner 凭据和 runner.yaml（需要 projects 白名单）；纯 probe 节点暂不能自更新，第八轮处理。

## 7. 工作量估计

| WP | 主要文件 | 估计 |
|---|---|---|
| WP0 | `fleet_core/identity.py`、`process_group.py` | 0.5 天 |
| WP1 | `fleet_core/locking.py`、`spool.py`、`muse.py`、runner/control-client 入口 | 1 天 |
| WP2 | `deploy/package-client-release.sh`、`ci.yml`、`auto_release.py`、`hub/http/client_release_routes.py` | 1.5 天 |
| WP3 | `tools/client_update/*`、`tools/fleet-client.py`、`agent-runner.py` drain | 2.5 天 |
| WP4 | `fleet_core/client_headers.py` + 4 处调用点、hub 认证层、迁移、前端机器卡片 | 1 天 |
| WP5–6（第八轮） | `fleet_core/protocol.py`、`compat_service.py`、probe | 2 天 |
