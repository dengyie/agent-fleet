# 产物流转、节点能力与工具接入契约

日期：2026-10-31。状态：F3 源码包与证据基础、F4.1 Hub V2 分片发布与恢复、F4.2 本机 POSIX Node 收发与统一容量、F4.3 能力目录/原子 admission、Node 报告/本地复核、Hub 固定绑定派发、协调器自动选点/输入目标固定，以及 capability-bound unknown/isolated workspace digest postcheck 的本机 POSIX/loopback 子集已落盘并通过聚焦回归；transfer/capability gate 默认关闭。Linux 全仓与测试沙箱、Windows 产物收发及真实 Windows→Hub→Linux 源包交接/独立固定验证已有各自证据；固定 workspace 能力 postcheck 的 Windows/Linux 12 项原生矩阵已通过；其余跨平台路径、完整 WF-04/05 和 F5 真实外部验收继续开放。依赖[工作流执行契约](2026-10-20-workflow-execution-contract.md)，实际证据见[实施进度](../../platform/workflow-platform/implementation-progress.md)。

## 1. 当前文件边界

ArtifactStore 当前使用不可预测 artifact_id、owner/workspace 和 SHA-256。hash 用于完整性校验；artifact_id 并非内容寻址 ID。相同 hash 不能证明跨用户可访问。

当前 put_file 读取完整文件后存储，单文件 10 MiB；put_bytes 是 PNG 专用入口。既有文本预览最多 64 KiB，下载走同源受鉴权接口。Node 具备截图上传路径，但没有通用跨节点任意产物协议。

新增传输使用独立流式入口，不通过放大 PNG 接口限制或把文件塞进 tool result 实现。旧小文件和截图合同保持原格式与限制；新能力在独立 gate 下验证。

## 2. 文件及来源模型

ArtifactManifestV2 保留现有 artifact_id、owner_id、workspace_id、name、size、sha256、content_type、created_at，并新增 schema_version=2 和 provenance_ref。

provenance_ref 指向 platform.db 中的 workflow_artifact_refs，包含：

| 字段 | 作用 |
|---|---|
| workflow_id / step_key / generation | 文件是哪一步的哪一代输出 |
| producer_binding | Run/Task/Command/外部任务的逻辑标识 |
| output_key | 合同中的输出名称 |
| input_subject_digest | 生成该输出时使用的输入 |
| source_revision | 如适用，记录 repository_id、head_commit、tree_id |
| validator_evidence_refs | 与当前产物一致的验证证据 |

manifest 和来源在公开 DTO 中按 owner/workspace 裁剪，不返回机器绝对路径或凭据。文件发布后不可覆盖；同名新文件产生新 artifact_id，并显式改变 output binding。

F4.1 的 `input_subject_digest` 取原步骤已冻结的 `ResolvedContract.digest`，覆盖合同和解析后的输入；它与交付校验使用的源码 `subject_digest` 分属不同事实，不能互相替代。当前 V2 manifest 保存 workflow/step/generation/output、Command producer binding 和该输入摘要，并与同事务的 `workflow_artifact_refs` 核对。源码 revision 和 validator evidence 仍由原交付域保存；通用上传不解析文件来补造源码版本或验证通过事实。

Node 接收时先检查大小和完整 hash，再在预配置 inputs root 内原子发布。下游输入是 artifact_id + sha256，不是“到某台机器读取这个任意路径”。

## 3. 流式上传与崩溃协议

新增 ArtifactTransferService 与 artifact_transfers / artifact_transfer_parts 表。传输与生成它的签名 Command、节点、owner/workspace、workflow/step/generation、output_key 绑定。

~~~text
Node 获准的产物导出命令
  → 申请 reservation（固定 artifact_id / transfer_id / 预期 hash 和大小）
  → 逐个上传固定 offset 的 chunk
  → commit：校验分片集合、整文件大小/hash、命令身份和当前授权
  → 固定 artifact_id 发布文件
  → 事务写 ready manifest/provenance/output binding
  → 返回受限 ArtifactRef
~~~

状态：reserved → uploading → verifying → publishing → ready；失败进入 failed、quarantined 或 unknown。只有 ready 文件可被下游或下载 API 看到。

幂等键由 command_id + output_key 派生。同一键的大小、hash、作用域或 generation 改变返回 transfer_conflict。artifact_id 在 reservation 时固定，重启不得换 ID 再发布一份。

每个 part 绑定 offset、长度、SHA-256，服务端实际计算 hash。只接受按 chunk_size 对齐的 offset，末块可短；同 offset 同内容返回原回执，不同内容 409。拒绝重叠、缺块、越界、过量分片、非法 Content-Length 和超过总截止的慢速输入。

### 3.1 Node HTTP 入口与实施状态

沿用现有 Node 机器绑定认证和固定签名命令，不接收 operator 凭据替 Node 上传。前四个入口已在 F4.1 实现，输入领取在 F4.2 接收子集中实现；均须显式启用 `platform_artifact_transfer_enabled`，且依赖 Workflow domain gate 与 Command signing key，缺签名配置时拒绝装配。关闭 dispatch gate、请求 pause 或进入 drain 只阻止后续 admission，不能关闭仍有效的在途文件回传；原 Run 取消、步骤 active deadline 已过、frozen 或授权撤销仍拒绝新发布。请求不允许覆盖 owner/workspace/node/generation，它们从原 command 绑定取得。上传各请求须携带 `X-Platform-Command-ID` 与 `X-Platform-Worker-ID`，服务端核验真实签名、参数摘要、机器身份、当前 worker lease、dispatch marker、generation 和固定输出合同。

| 方法与路径 | 请求/响应边界 | 状态 |
|---|---|---|
| POST /api/platform/v1/node/artifact-transfers | command_id、output_key、name、size、sha256；返回固定 transfer_id/artifact_id、chunk_size、UTC ISO-8601 expires_at | F4.1 已实现，默认关闭 |
| GET /api/platform/v1/node/artifact-transfers/{id} | 仅原节点；有界状态/part hash 与可重传 offset，不返回宿主路径 | F4.1 已实现，默认关闭 |
| PUT /api/platform/v1/node/artifact-transfers/{id}/parts/{offset} | 原始 bytes、精确 Content-Length 和 X-Content-SHA256；正文前授权/限长，再计算 hash | F4.1 已实现，默认关闭 |
| POST /api/platform/v1/node/artifact-transfers/{id}/commit | command_id、固定 size/hash；同内容查回同一个 ArtifactRef | F4.1 已实现，默认关闭 |
| GET /api/platform/v1/node/artifact-inputs/{grant_id}/content | 仅绑定节点/命令领取的原文件 bytes；初版断传重下，不接受任意 URL、path 或 Range | F4.2 接收子集已实现，默认关闭 |

只有分片入口将单请求上限放宽至 1 MiB，其余接口保持原全局限制。应用每读取至多 64 KiB，前后都检查 reservation、Command/worker lease 与步骤 active deadline 的有效截止；断连或越界返回统一 JSON 错误。现有 Flask/SQLite 仍为同步架构，WSGI 单次阻塞读取的底层超时须由服务器/代理配置约束并单独验收；本机用例不证明生产慢连接控制。

接收完成通过原 Node Command receipt 返回 artifact_id/sha256/size 与本地 input_alias 回执。grant TTL 必须覆盖该命令有效期且有绝对截止；重复合法接收使用 journal 查回。传输 token/鉴权材料不进入 URL 查询参数和模型上下文。

错误沿用 400/404/409/413/429/503 的稳定语义；不可访问与不存在统一 404。commit 只发布文件，不直接把生产步骤改成 succeeded，生产步骤仍须核对原 Command/Task 的完整输出。

数据库和文件系统不是同一事务。publishing 阶段持久记录最终 ID 和 hash：

- 文件 rename 前崩溃：重启核查 stage，继续同一 ID 或明确失败。
- rename 后 DB ready 前崩溃：核查最终内容与 hash，只完成元数据发布，不能复制第二份文件。
- DB 与文件内容不一致：转 quarantined，不能把缺失内容作为 ready。
- 重复 commit：返回已经确认的同一 artifact；输入不一致拒绝。

文件与 SQLite 快照复用现有 snapshot barrier，增加对 V2/publishing 的识别。GC/备份不能跳过新引用面；遇未知 schema、结构损坏或扫描上限时停止清理并报告。

F4.1 逐片流式写入私有 `.publication`，不额外生成完整 assembled 副本；内容与 manifest fsync 后，以固定 artifact_id 排他 rename。文件先保持隐藏，DB ready 与 output reference 同事务提交，再持久翻转文件可见性。三个崩溃窗口均由恢复核对同一 ID、大小/hash、来源和引用；缺失或不一致内容隔离，不返回 ready 文件。下载核验后复用同一描述符分块响应，预览仍保持原 64 KiB 上限。

备份即使 gate 关闭也识别独立 transfer schema；存在任何未收敛为 ready 的 transfer 时返回 `artifact_transfer_incomplete`，不静默丢失上传中的 staging。仅 ready 的 V2 文件及完整来源/引用闭包可以备份，明文和加密恢复均保持 Workflow frozen。ready transfer 遗留的上传 scratch 不复制到恢复目标，恢复副本将其 staging/publication 记账清零；源目录和源库不变。尚未清理的失败/未知传输须先明确核对，不能绕过检查生成“完整备份”。

## 4. 下载、接收和本地目录

Node 先领取固定 artifact receive Command。Hub 为该 command 记录到指定 node/step/generation 的 transfer grant；grant_id 是引用，不是 bearer 凭据。Node 的请求仍须使用自己的机器绑定凭据，原文凭据不能进入模型上下文或 URL。

只允许 Hub 已登记的 origin 和受鉴权路径。Node 不接受外部任意 download URL，不携带凭据跟随重定向。

目标以 input_alias 表达，由 Node 本地配置映射到专用工作区。文件名由传输 ID 与允许后缀生成；不允许宿主绝对路径、..、覆盖现有文件、symlink、junction/reparse 跳转。

POSIX 和 Windows 分别验证文件描述符/句柄与目录边界。平台无法可靠执行所需保护时返回 capability_unavailable，不能把 POSIX 的 O_NOFOLLOW 当作 Windows 已验收的证据。

下载先写 staging，流式计算大小/hash，确认后原子发布并持久化 receive receipt。下游只有在目标 Node 确认接收后才能 ready。一次完整接收后重启，复用本地 journal 和固定目标，不重新安装或执行文件。

通用文件服务只搬运 bytes，不默认执行 APK、脚本、安装包或 HTML。安装与测试是后续有授权的独立能力。

### 4.1 已实现的接收子集与恢复边界

本节约束实际需要 Node 本地输入的目标。固定 Hub 评审、固定测试及显式完整文本 Native 消费者按[执行契约 §5.5](2026-10-20-workflow-execution-contract.md#55-hub-本地消费者与-node-输入位置)从真实 Run target 派生精确登记，复用 Hub 原 reader；缺登记仍要求 Node receipt。协调器与 admission 共用登记索引，已固定 Node 的当前 generation 不允许改为本地消费绕过签收。登记不授予新执行、解包或工具权限。

Hub 通过管理员装配的 `workflow_node_inputs` 登记 owner/workspace/node 的 ref_key → input_alias/后缀。启用 transfer gate 后，协调器在原输入冻结事务中写固定 `artifact.receive` 签名 Command 与 outbox；复用 `platform_commands` 保存 grant 和权威回执，不增加另一套 Hub 状态库。grant 由 owner/workflow/step/generation/node/ref_key 派生，命令参数绑定原 artifact_id/sha256/size 与 resolved digest。缺登记时保留 `waiting_node/input_alias_unregistered`，接收中为 `waiting_node/inputs_receiving`；当前代全部原回执验证通过后才转 ready，直接 admission 同样受约束。回执查询为一次有界批量读取及 O(n) 字典核对。

Node 显式配置 `NodeRuntimeConfig.artifact_inputs`、独立的 `artifact_journal_path` 和默认 64 MiB/硬上限 256 MiB。Node 原 Command journal 不负责重放接收；独立 SQLite 账本保存原签名 envelope、目录/文件 inode、固定 receipt、下载次数及 Hub 确认。状态 `receiving → publishing → ready`，不一致进入 `reconciliation`。断传按原 grant 重下，最多三次；第三次已完整下载的 publishing 仍可恢复原子发布。完成后重启一次有界查询读取待确认记录，逐个核对原文件，再重传同一回执；Hub 已落库但响应丢失不重新下载。未知 schema、非对象命令、文件/账本篡改均失败关闭。

每次下载只请求登记的 Hub origin 和固定路径，不跟随任何重定向。Hub/Node 以至多 64 KiB 分块，校验精确大小及完整 hash；Node 逐级打开无链接目录，验证 owner、权限、inode 和目录未被替换，使用排他 rename 发布同一个目标。接收回执不包含绝对路径。Hub 在每块前后复核原机器凭据指纹、Command/worker 租约、当前 generation、workspace/node、取消、frozen 与等待截止；指纹只在认证内存中传递，不进入公开 DTO/URL/模型。回执事务也复查原凭据，拒绝认证后发生的禁用或轮换。密码派生仅在入口认证时执行。

macOS/POSIX 证据覆盖四个真实进程退出窗口、目录替换/链接/覆盖攻击、12 MiB 接收时 Python 追踪分配峰值小于 2 MiB、真实 loopback HTTP 及源码包 tree/blob/固定 pytest 验证；Linux 冻结全仓门禁已通过。这些不是 RSS 或两台产物节点交接证据。Windows 接收边界及真实平台结果见下节；接收不自动解压或执行文件，源码解包/测试复用 F3 的独立授权操作。

#### Windows 接收边界（2026-10-30）

Windows adapter 使用标准库 ctypes，不引入包、服务或独立状态库。仅接受固定本地 NTFS drive 的绝对路径；拒绝 UNC/device namespace、ADS、保留设备名、控制字符、尾部点/空格和模糊组件，深度最多 128。逐级固定父目录原生句柄，不共享 DELETE；NtCreateFile 只相对固定父句柄打开，要求不跟随 reparse，并验证类型/volume serial/128-bit file ID/单硬链接。junction 与其他 reparse、目录替换或不支持的文件系统失败关闭。

最终输入目录、槽位目录与文件要求当前用户为 owner；非当前用户、SYSTEM 或 Administrators 的写授权均拒绝，包含继承/仅继承 ACE；空 DACL 指针、未知 ACE 类型或超过 256 项失败关闭。现有配置目录 ACL 不会被自动修改；新建 staging/锁文件从创建时携带私有 DACL。每次 Windows manifest 计算重新核验配置根和槽位根，首次实际执行仍重新打开/验证。

staging 排他创建并记录原 volume/file ID，分块 hash/精确长度核验后 flush。同一源句柄以 FileRenameInformation、ReplaceIfExists=false 相对目标根发布，前后 FlushFileBuffers，最后重新验证同一文件与完整字节再保存 ready。Windows 不伪造 POSIX directory fsync；进程退出/重启证据不宣称断电持久性，恢复找不到原文件或内容不符进入 reconciliation。journal schema/原命令/三次尝试/回执字节不变，128-bit ID 仅在 SQLite 私有文本中保存。

每 grant 和固定两槽通过 LockFileEx 非阻塞内核锁，关闭/进程退出释放；收发槽位名称继续原 Command journal 派生，不按接收账本扩出新容量。Windows 只在实际适配器和配置根保护可用时声明 `artifact.receive`；`artifact.export` 必须独立登记输出并满足下节的源句柄保护，接收配置不能推导导出支持。

真实 home-win Windows 11/NTFS 已验证路径/ACL、junction/硬链接、无覆盖发布、四个真实进程退出窗口、原回执恢复与 12 MiB 分块内存约束。另由原 NodeRuntime 经临时 SSH 回环转发调用原 Hub HTTP：丢失首个回执响应后重启，仅重传原回执，保持一次下载，Hub 原事务确认后才 ready。接收测试命令、冻结源码及最终结果见[Windows 接收计划](../plans/2026-10-30-windows-artifact-receive.md)；导出属于下节独立实施范围，两 Node 源包交接及完整 WF-04 仍开放。

Hub 上传与下载共用每节点 2/全局 4 的 admission：同一 SQLite 写事务聚合活跃上传并探测固定的下载内核槽，再持久预留上传或取得下载锁。上传的 reserved/uploading/verifying/publishing/unknown 均占位；下载不增加租约表，响应关闭或进程退出自然释放锁。持有下载流期间不持数据库写锁。一次检查为有索引的聚合与六个非阻塞锁探测；同一 owner/node 联合身份计数。NodeRuntime 的导出与接收使用原 Command journal 路径定位同一个本地两槽池，独立收发 journal 不各自扩出两个槽。低于默认值的 Hub 限制同样合并执行。

### 4.2 固定 Node 导出与本机交接

`artifact.export` 必须使用原签名 envelope，参数固定为 `output_key`，并携带原 Run/resource 绑定。Node 通过 `NodeRuntimeConfig.artifact_outputs` 登记 `(workspace_id, output_key, OutputFile)`；OutputFile 只包含管理员配置的绝对 root、受限相对路径和公开文件名。命令不能选择路径、URL、程序或新能力。逐级无链接打开目录，核对 owner/权限；仅接受普通、单硬链接且不可被其他用户写入的文件。固定 fd 的 device/inode/size/mtime/ctime/mode/owner/link count 与 root 身份在 hash、每片读取和 commit 前复查，路径替换或内容变化进入 reconciliation。

导出先以 64 KiB 计算整文件 hash，再逐个发送最多 1 MiB 分片；恢复先按原 transfer_id 查询有界 part 清单，以 offset 字典 O(1) 核对和跳过已确认片。完整文件处理 O(n)，内存受分片和最多 256 个 part 元数据限制。所有 reservation/status/PUT/commit 及导出成功回执使用不跟随重定向的传输；JSON 响应最多 64 KiB，单次请求受超时、取消及原命令/两小时总截止限制。Hub 继续权威检查原 worker lease 和 reservation TTL，分片成功沿用原受限续租；Node 不创建新命令或延长原执行授权。

独立 `artifact_export_journal_path` 默认使用 `.exports.db`，不能与 Command 或 receive journal 重叠。账本保存原签名命令、配置摘要、源文件身份/大小/hash、原 transfer/artifact ID 和固定 receipt，不保存机器凭据。状态为 prepared → reserved → committing → ready；准备/上传最多三次自动尝试，committing 仅在原截止内查询并重提同一 commit。丢失 reservation 响应按 command/output 幂等键查回；丢失 part 响应先查清单；丢失 commit 或回执响应不创建第二份文件。临时失败保留原 Command 非终态供独立账本恢复；源文件丢失/变化、损坏命令/截止或超过尝试上限停止自动重放并进入 reconciliation；未知 schema 拒绝装配，保留原库。

成功 receipt 仅为 `output_key` 和受限的 `artifact_id/sha256/size`。HTTP commit/status 恢复在首次发布 ready 的同一写事务复查原认证凭据，拒绝入口认证之后的禁用/轮换；已完成文件不因旧请求失效而撤回。Hub 在回执写事务再次复查原认证凭据仍有效，并要求它与原命令已经 ready 的 transfer 一致；伪造成功或改变终态回执均拒绝。ready 账本可重传同一回执，不要求源文件继续存在。commit/Command 成功均不直接完成生产步骤，完整 Run 输出及原 Workflow 对账仍独立执行。

本机证据包括四个真实进程退出窗口、三类丢响应、源文件/账本篡改、12 MiB 流式分配，以及两个独立身份/凭据/目录/journal 的真实 loopback HTTP：Node A 导出 Git 源包，Hub 写原 output binding，Node B 对固定输入确认完整 hash 后才 ready，随后以独立 F3 操作核对 tree/blob 并运行固定 pytest。该链没有自动安装、解压或执行下载内容；明确的源码验证步骤属于独立授权。Linux 原严格全仓与原发布核验已通过；Windows 接收真实句柄/reparse 验证见 §4.1。两台获准真实机器的源包交接已有下节独立实测；完整 WF-04 继续开放，transfer gate 默认关闭。

#### Windows 固定源导出边界（2026-10-30）

复用接收适配器的本地 NTFS/绝对 root/逐级父句柄保护。输出相对路径使用 `/`，最多 32 组件/512 UTF-8 bytes，每个组件应用 Windows 精确名称规则；ADS、UNC/device、drive-relative、保留设备名、点/空组件、尾部点/空格及损坏 Unicode 均在实际源读取前拒绝。管理员 root、相对路径和公开 name 来自 OutputFile，签名命令仍只能选 output_key。

嵌套目录仅相对固定父句柄打开，全部 retained handles 拒绝 DELETE sharing；每个输出目录核验 owner/DACL。源文件使用 FILE_SHARE_READ，拒绝已存在的写句柄/可写映射并阻止后续普通写入、替换和 rename；读同一原生句柄，不以路径重新打开后替换数据。源必须普通、单硬链接且 owner 为当前 token 用户，写权限策略沿用 §4.1。Windows 不放宽为 Administrators owner，也不自动修补管理员登记源的 ACL。

原 source_identity JSON 保存 decimal-text volume serial/128-bit file ID/size/单链接、LastWriteTime/ChangeTime 的整数纳秒值、原 attributes、owner SID，以及 descriptor control/ACL revision/有序 ACE type/flags/mask/SID 的 canonical SHA-256。纳秒值由原 FILETIME ticks 整数乘 100，仅用于相等核对，不经过浮点或本地时区转换。POSIX identity 字段与旧 journal 内容不变；Windows 新增 ACL 字段位于现有私有 JSON，不增加 journal schema 或公开 DTO 字段。权限变得更宽或只读 ACE 改变也会使原 identity 失配。hash、每片读取与 commit 前同时核验固定源和当前路径/root 身份，任何不符进入原 reconciliation。

Windows `.available` 每次重验配置输出 root 和槽位 root，只作声明准入；原 export/recover 仍进入现有错误/账本范围核验选定源，已开始的传输遇不安全 root 进入 reconciliation。ready outbox 查回原 receipt 不要求源仍存在；丢 reservation/part/commit/receipt 响应仍复用原 ID/offset/receipt、原尝试上限与截止。不增加任务、源复制或远程程序执行。

home-win 最终测试已验证源句柄写/rename 和可写映射拒绝、ACL 变化、固定字节、12 MiB 流式分配、同两槽与四个实际进程退出。独立原 NodeRuntime→原临时 POSIX Hub HTTP 通过 part/receipt 丢响应及重启原 ID/回执，原 output binding 和完整 Hub 字节核验保持；本地 disk-sink 测试只证明协议，不充当 Hub 实测。同一 886 文件冻结 revision `6ba4384adde2a01f44dd96b8ec9c85c2e9a2d839` 已通过 Windows 原生合并 131 项、原 Hub HTTP 2 项、双 Python 关联各 295 项及 Linux 原严格全仓 5785 passed/197 passing subtests/零 skip/42 journeys；远端与本地原 verifier 均通过，44 PNG 与证据复制摘要完整核验。[Windows 导出计划](../plans/2026-10-30-windows-artifact-export.md)保存任务和证据，真实两产物 Node 源包交接见下节独立证据；完整 WF-04 继续开放。

#### 真实两机源包交接（2026-10-30）

[两机计划](../plans/2026-10-30-two-host-artifact-handoff.md#actual-frozen-verification--2026-10-30)的独立 opt-in 夹具已在 home-win Windows producer → 原临时 Hub → gz-cvm Linux receiver 跑通。冻结 revision `8f63ddd660c07ddf9d55123132b2b26d2b4f2296` 的 890 个源码摘要在两机运行前后相同；控制端 1 passed，随后独立 Linux sandbox 的固定 pytest 2 passed，均无 failure/error/skip。

原 SourceExporter 仅导出固定提交，合成凭据文件及未提交/未跟踪字节未入包；原签名 export 丢 part 响应后以原 ID 补查，仅一次 PUT，原 output binding 和 Hub 完整 bytes 正确。原签名 receive 仅下载一次，丢回执响应后重传同一回执两次；错误节点 404，Hub 在原 input reconciliation 后才 ready。接收操作不解包/执行；另行明确授权的验证重开已固定文件、核对原 repository/tree/blob，并由既有 FixedTestRunner 执行。三端保留字节、报告/JUnit 和八个远端复制文件的 SHA-256 已独立复核。

该夹具只增加测试与证据，不改产品协议、状态库或权限。它没有完成整个 Workflow，也不代填 required WF-04、NODE-LIVE 或其他真实模型/CLI/生产/外部检查；既有 5785-pass 全仓只证明前一导出冻结，不能与本次计数拼接。所有 gate 默认关闭。

## 5. 限制、配额与保留

以下为新协议的初始配置上限，属于设计值，实施时压测：

| 项目 | 初值 |
|---|---|
| 旧 put_file/PNG 上传 | 保持 10 MiB |
| 新流式 artifact | 默认 64 MiB；配置硬上限 256 MiB |
| chunk_size | 1 MiB，最多 256 parts |
| 每节点同时传输 | 2；服务端全局 4 |
| Workspace artifact 总配额 | 默认 1 GiB；同时计算 ready、实际 staging 和预留 |
| 每流程新增文件预算 | 默认 256 MiB，不能超过 workspace 剩余额度 |
| reservation TTL | 30 分钟；活跃传输受限续租，总时长不超过 2 小时 |
| 单次状态/part 清单 | 最多 256 项；不返回文件正文 |

reservation 预留整文件容量，避免并发越额。失败/过期时，只能释放未使用的预留；磁盘实际 staging bytes 仍计费，直到完成明确清理。不能因为状态失败就把仍占盘的文件当成零。

F4.1 的 workspace 核账包括旧 ready 文件、V2 ready 文件、保留的 staging、活跃 reservation，以及整文件发布副本的额外预留；同一上传已占用的分片容量与其 reservation 取较大者，避免重复计算同一份字节。流程 256 MiB 预算按逻辑文件计，额外临时副本仍受 workspace 实际占盘约束。并发与额度的检查/预留使用同一 SQLite 写事务；单传输文件锁非阻塞获取，竞争返回 429。Hub 文件句柄与锁保护面向 POSIX；Windows Node 导出源与本地槽位保护按 §4.2 独立验证，Node 平台支持不推导 Windows Hub 上传存储已验收。

新清理默认 dry-run，列出单个候选及引用关系；具体文件删除按操作者现有逐项确认规则执行。不能在本轮文档任务中触发任何 GC。

大文件传输不等于软件包下载授权。超过操作者阈值的软件、程序、二进制依赖下载继续遵循其明确批准规则；流程预算或工具发现不能替代批准。

二进制保持 download-only。扩展图片/媒体预览时另设固定 MIME、尺寸/解码预算和隔离策略；不在 operator origin 执行不可信 HTML。

## 6. 节点能力目录

新增 CapabilityDefinition（管理员登记）与 NodeOffer（节点上报）。保留旧 capabilities 布尔映射用于兼容；不能改变其现有含义并要求旧节点立刻升级。

CapabilityDefinition：

~~~json
{
  "capability_id": "coding.review",
  "revision": 1,
  "adapter_id": "cli-review-v1",
  "input_schema_ref": "code-review-input-v1",
  "output_schema_ref": "code-review-output-v1",
  "risk": "workspace_read",
  "retry_class": "reconcile_before_retry",
  "allowed_owner_roles": ["operator"],
  "max_active_seconds": 1800,
  "result_limit_bytes": 65536
}
~~~

schema_ref 在登记时解析为固定 schema 并记录 hash；运行不能随目录更新偷偷改成另一个接口。示例 capability_id 是拟议目录项，不是现有工具名称。

NodeOffer 包含 node_id、offer_revision、实现能力/revision、OS、architecture、软件版本、槽位、可用内存/磁盘、observed_at、received_at 和健康状态。源自受鉴权报告，报告内容仍需 schema/大小校验。

新鲜度以 Hub 接收时钟 received_at 计算，Node 自报 observed_at 仅作诊断；未来时间不能延长有效期。启动后尚未收到报告的节点不参与新选择。

固定出口、受限许可证、专有数据位置等约束由管理员登记的 alias 表达。节点自报“trusted”或“read_only”不能增加可用权限。

### 6.1 确定性选择

按顺序过滤：

1. 当前 owner 可见、启用且允许该模板/工作区的节点。
2. 满足平台、软件/能力 revision、输入可达和数据驻留规则。
3. 有明确且未过期的健康报告；新选择默认 freshness 不超过 120 秒。
4. 本地策略与 Hub 策略均允许动作。
5. 有持久并发槽与足够已声明资源。

过滤后按管理员 priority、已占用槽位、node_id 排序。无候选返回 waiting_node 和有界候选排除原因；没有心跳不能推断节点可用。已有 browser 的 300 秒失联规则保持原合同，新选节点阈值单独配置。

SQLite capability_reservations 在 admission 事务中预留槽位。已有 ResourceLeaseManager 是进程内资源租约，不能直接当作跨进程持久节点容量锁。

`target_key` 是物理资源的全局键；固定 target 的旧 admission 与能力选点都必须计入该目标的 `held` 和 `isolated` reservation，不能按 owner 分割后凭空增加容量。`released` 不占槽。隔离只保留未决执行的资源屏障，不等于已确认停止或完成。

目标绑定记录 node_id、offer_revision、capability revision 和 input digest。Node 执行前重新验证软件/策略；能力已变化返回 capability_changed。已派发步骤不自动换机；新一代迁移要求旧副作用已清楚、输入已接收、数据策略允许。

### 6.2 F4.3 目录、报告与显式 admission 子集

本机已实现 `hub/domain/capability.py`、`hub/application/capability_router.py`、`hub/infrastructure/capability_schema.py` 与 `capability_repository.py`。`platform_capability_routing_enabled` 默认 false，依赖 Workflow domain；不隐含启用 dispatch、transfer 或任意 Node 动作。管理员通过可信装配参数 `workflow_capability_definitions` / `workflow_capability_nodes` 登记能力定义和 NodePolicy；没有 operator/模型可写的目录入口。

CapabilityDefinition 在登记时接收实际 schema bytes，固定规范 JSON、schema hash 和完整定义摘要；同 id/revision 不同内容拒绝。单 input/output schema 最多 32 KiB、16 层，完整定义最多 64 KiB，禁止外部 schema 引用；目录最多 4096 个版本。risk、retry_class、owner role、时间/结果限制由管理员定义，不能由 Node 注解扩权。定义快照/hash 在构造时固定，候选循环不重复解析 schema。

NodePolicy 登记 owner/node、workspace/template revision allowlist、允许的能力 revision/risk、priority、slot_limit、input/residency alias。报告只提供实现事实与本地允许的能力，路由取二者交集。软件版本采用管理员指定的精确字符串匹配，不猜测 SemVer 兼容；选择同一能力所有兼容 revision 中通过双端策略的最高版本。

`POST /api/platform/v1/nodes/capability-offers` 复用现有 Node 机器凭据；owner/node 从认证取得，body 不接受覆盖字段、trusted/readOnly 注解、路径或执行请求。严格解析原始 JSON，拒绝重复键/未知字段/非有限数；正文最多 64 KiB。请求固定字段为：

```json
{
  "schema_version": 1,
  "offer_revision": 1,
  "capabilities": [{"id": "coding.review", "revision": 1}],
  "local_allowed_capabilities": [{"id": "coding.review", "revision": 1}],
  "os": "linux",
  "architecture": "x86_64",
  "software": {"python": "3.10.21"},
  "slots": 2,
  "memory_bytes": 1073741824,
  "disk_bytes": 10737418240,
  "observed_at": "2026-10-23T00:00:00Z",
  "health": "healthy"
}
```

报告能力最多 128 个 revision、软件最多 64 项；slots=0..1024，health 为 healthy/degraded/unhealthy，OS 为 linux/darwin/windows。资源均为精确整数字节，单项不超过 1 PiB；revision 为 1..9007199254740991。observed_at 必须显式 UTC `Z`，只供诊断。HTTP 响应只返回本节点 node_id、offer_revision、Hub UTC received_at。认证后在写事务再次复核原凭据指纹，禁用/轮换拒绝；不改变原节点 capabilities 布尔表的含义。

相同 offer revision 可刷新内存/磁盘、健康与诊断时间；能力、local policy、平台、软件或 slots 变化必须提升 revision。旧 revision 或同 revision 异配置返回 409，Hub 接收时钟回退的更新同样拒绝。启动时读取 SQLite 的全局报告序号，只让之后收到的受鉴权报告参与新选择；多进程中由另一个 Hub 进程接收的新报告同样有效，相同 wall-clock 时间也不复用启动前的旧报告。新鲜度仍按 received_at 计算，120 秒恰好有效，超过或晚于当前 Hub 时钟均排除；不改变 browser 的 300 秒规则。

`select_target` 是无 I/O 的候选提案，按 §6.1 顺序检查并以 `(priority, held_slots, node_id)` 取最小值。候选最多 256，超限拒绝；无候选返回 waiting_node、排除总数及至多 32 项稳定原因。跨 owner 的原因不返回其 node_id。输入/驻留只接受管理员 alias，不将报告中的自述当作授权。槽位包括 held/isolated 和旧固定 target 的 reservation；旧记录缺资源用量证据时，有资源需求的新 admission 返回 resource_evidence_missing，不能按零字节计算。

`CapabilityRepository.admit` 是显式持久 port：在一个 SQLite 写事务里复核当前 claim/epoch/revision、owner/workspace/template、当前 generation、原冻结合同的 capability/adapter/input digest/active budget，重新选择并调用原 `WorkflowRepository.admit_step`，同事务写 intent、预算、原槽位、固定 binding 和事件。绑定保存 node/offer/定义/策略/input/requirements 摘要及资源字节。已有 input target 只检查原节点；相同 generation 的合法重试返回原 intent 和 binding，不因新报告自动换机。重复请求只接受原 admission 或当前 revision，异输入/无关 revision 拒绝；原 pause/cancel/drain/frozen 和租约 fencing 继续生效。未知执行不能因协调器租约到期释放占用。

目录使用独立 capability schema=1，并仅对包含该域的库提升 Workflow minimum reader 至 2；旧库仍为 1。gate 关闭的 reader、备份与 GC 也识别新表；未知/残缺 schema、损坏摘要或与节点/intent/预算不一致的 binding 拒绝备份。明文和加密恢复保留目录、原 binding/reservation，Workflow 保持 frozen，新实例仍需启动后的新报告。新表不包含 artifact 引用或机器凭据。

目录切片的显式 admission 由真实 SQLite/HTTP 与双进程竞争测试覆盖。NodeRuntime 的报告发送和本地执行前复核由 §6.3 实现，Hub 固定绑定的可信注入及首次派发复核由 §6.4 实现，后续协调器自动选点与输入目标固定本机子集由 §6.5 接线。不能只凭目录切片启用自动路由或声明完整 WF-05；不存在新增的第二套执行引擎。

### 6.3 Node 报告发送与本地执行前复核子集

`NodeRuntimeConfig.capability_config` 默认 None；显式提供 `NodeCapabilityConfig` 才装配报告/复核服务，并要求有效 Command 公钥。管理员为每个 `(capability_id, revision)` 登记完整不可变 `CapabilityDefinition` 和固定动作集合；本地 allowlist 单独登记。一个能力的所有动作都必须在当前 Node 实际可用才报告该实现，allowlist 与这些实现取交集。没有 sandbox launcher 的 workspace.exec、不可用 browser/Muse 不作为实际能力报告；这不证明已配置 launcher 的生产隔离已经验收。旧布尔 manifest/heartbeat 不变，不增加线程或状态库。

默认 observer 只读取当前 Python 解释器版本、OS、架构和工作区卷可用磁盘，不执行额外程序或安装依赖。标准库不能跨平台、按容器边界可靠测量可用内存，默认报告 **0 字节**，因此不能满足非零内存需求。额外软件/资源只能来自管理员注入的类型化 `CapabilityObserver`；不得把模型文本或配置里的“已安装”当作实际探测。每次报告及首次执行前都重新调用 observer；硬件/软件状态不保存在服务实例的请求字段中。

`NodeRuntime.report_capabilities()` 调用原受鉴权报告入口。启用新配置的 `poll_once()` 在原 poll 前发送一次；失败以稳定 `capability_report_error` 附着返回值，并继续原在途命令/回执生命周期。每次报告最多 15 秒，使用单调时钟覆盖采样和网络，在采样后及响应后复核停止/截止；HTTP 不重试、不跟随重定向，响应最多 64 KiB。仅允许固定 Hub origin，拒绝 URL 凭据、query/fragment、路径、非法端口与控制字符。机器凭据仅置于原请求头。receipt 必须精确匹配原 node/revision 和显式 UTC `Z` 时间；错误保留原异常链，公开结果不包含凭据或宿主路径。

offer revision 由管理员显式管理，重启不能临时换 revision 绕过冲突。软件/平台/槽位/本地允许能力变化但未提升 revision 时，Hub 继续返回原 `offer_revision_conflict`；该失败不替换旧 binding。资源/健康刷新仍沿用 §6.2，不能把 Node 诊断时钟用于新鲜度。

能力 Command 上下文固定保存在 `arguments._fleet_capability`：

```json
{
  "schema_version": 1,
  "capability_id": "workspace.files",
  "adapter_id": "workspace-adapter",
  "binding": {
    "node_id": "node-a",
    "offer_revision": 1,
    "offer_digest": "1111111111111111111111111111111111111111111111111111111111111111",
    "policy_digest": "2222222222222222222222222222222222222222222222222222222222222222",
    "capabilities": [{"id": "workspace.files", "revision": 1, "definition_digest": "3333333333333333333333333333333333333333333333333333333333333333"}],
    "input_digest": "4444444444444444444444444444444444444444444444444444444444444444",
    "requirements_digest": "5555555555555555555555555555555555555555555555555555555555555555",
    "memory_bytes": 0,
    "disk_bytes": 0
  }
}
```

此示例展示有效字段格式，运行时摘要必须来自原固定值。`CapabilityExecutionContext.bind` 是纯领域准备函数：只允许原 node、有效 Run 标识与 tool./reconcile. 动作族，实际动作仍受 Node 登记白名单限制，拒绝覆盖已有保留字段；更改参数后清除旧签名，必须重新签名。原 args_hash 和 Ed25519 已覆盖全部 arguments，因此没有新增未签名顶层字段、数据库列或第二种签名。单次动作由 binding 中明确的 capability_id 提供，不从动作名称猜测逻辑能力或风险。

原 NodeClient 先验签/节点/截止，再查原 journal；首次执行由 NodeToolExecutor 在动作之前核对上下文。比对当前 offer revision/config digest、所有固定定义 digest、adapter、所选动作、retry_class、本地 allowlist、健康/非零槽位和资源；软件、定义、策略或能力变化返回 `capability_changed`，无副作用。校验后才移除保留参数交给原固定 executor。未装配新服务的 Node 遇此参数返回 `capability_unavailable`；无公钥的旧 NodeClient 不接受携带此参数的命令。

已保存的完成/失败/unknown Command 只查回原 journal，不因新配置重跑或撤回旧事实。receive/export 继续其独立签名和崩溃恢复协议，**本子集不为这两类动作接受能力上下文或声明 capability 路由支持**。

本机实际 HTTP 用例从受鉴权 Node 报告、原显式 admission 的 binding，经过测试中明确的可信签名准备、原 Command poll/receipt 和本地文件读取；同 revision 软件变化使报告冲突，并在执行前拒绝旧绑定。这不是现有协调器自动选点或 Hub 已验证任意注入上下文的证据。上述最初的测试准备现已由 §6.4 的可信 Hub preparation 替代；模型、operator 请求体和普通 tool arguments 不能自行创建上下文。自动选点协调器的后续本机接线见 §6.5；gate 维持默认关闭，完整 WF-05 保持开放。

### 6.4 Hub 固定能力 Run 与 Command 派发子集

`hub/infrastructure/capability_dispatch.py` 连接原显式 admission、RunStepExecutor 和 CommandDeliveryService。`workflow_capability_actions` 是管理员装配参数，元素为不可变 `RegisteredCapabilityAction(adapter_id, CatalogRef(capability_id, revision), action)`；无 operator/模型写入入口。动作必须属于原 Node workspace/browser/web_agent 固定集合或 `reconcile.workspace.digest`，每次动作必须明确且唯一匹配本步骤固定能力之一。原 RemoteToolBroker retry 规则统一提供判定，目录定义、动作和命令的 retry_class 必须精确相等，浏览器/web_agent 仍为 manual_only。

本节普通工具子集由 `native_run` 的 remote Node Run 承载：注册 target_key 与 config 的 execution_node_id 必须同为原 binding.node_id。独立远程固定测试的 `validation + remote_test` 是 §8.4 的显式扩展，不授予一般 validation 任意工具权限。CLI Task、Hub 本地 test/review、service postcheck 的资源作用域以及 receive/export 能力路由仍独立；本节的 unknown/isolated workspace digest postcheck 已在后文以 descriptor-backed 本机子集贯通，不能据此冒充完整 WF-05；固定 workspace digest 的实际 Windows/Linux 范围由本节后文单独记录。原协议继续独立有效。

新 Run 在原创建事务读取固定 binding、requirements/定义/输入摘要、原预算和槽位，核对当前 generation、原截止、工作流控制、节点授权、报告/软件/策略及能力 revision。复核只查询原节点；容量聚合只扣除本步骤自己的 held 预约，再加回原需求，保留其他 held/isolated/未知资源占用，不能要求第二个空槽或迁移同代执行。Run 配置增加受信任 `workflow_capability={binding_digest}`，并参与原 workflow_dispatch.config_digest；请求不能设置该标记。已提交 Run 的 lookup 查回原固定值，不因后来的软件/策略变化再创建。

Command preparation 在原 enqueue 写事务从 Run 反查 workflow_dispatches.token 和固定 binding；配置标记缺失/损坏不能使原 binding 退回普通命令。核对 owner/workspace/node/Run、原消息内容与配置摘要、当前 generation、预算/槽位、仍有效的 Run lease 和工作流控制，然后才注入 §6.3 上下文、清除旧签名并以原 Ed25519 密钥重新签名。即使旧签名 gate 关闭，能力命令仍须签名。普通 tool/operator 参数、预签名 caller 对象中的 `_fleet_capability` 一律拒绝；注入后仍受 64 KiB 参数上限，expires_at 最长为原步骤 active_deadline。

浏览器 submit 的审批消费、能力上下文、签名、Command 和 outbox 在同一事务提交；通用 enqueue 也必须查到精确命令/owner/workspace/node/Run/session/selector 对应的已消费审批，不能用伪造 approval_id 绕过。重复 idempotency_key 先比较原工具参数、retry 与作用域，查回原完整签名 Command，不再次准备或消费审批。已完成/failed/unknown 的结果优先查回，不因后续能力变化重复执行。

每次首次 Node lease 在原 claim 事务再次核对同一作用域、绑定、报告、策略与预约。pause、drain、frozen 或临时关闭 capability gate 暂缓 queued 命令；取消、失代、失效 Run lease、损坏摘要、能力/动作登记变化等在零次投递时保存确定失败，不冒称远端 unknown。重启后的旧报告不满足新鲜度，当前子集按 capability_changed 拒绝首次投递，要求在下一次新命令准备前取得启动后的新报告。已 leased/accepted/running 命令继续原 receipt/unknown 协议；不能追溯撤回其事实，也不据此释放 Workflow 槽位。

应用在 capability gate 打开时装配同一 preparation 到原 delivery、Command repository 和已登记的 RunStepExecutor；不启动 worker。gate-off reader 识别原 binding，不能生成无绑定回退。真实 loopback HTTP 用例已从原 Node 报告、显式 admission、原 Run、可信 Command preparation/signing、poll/receipt 到 Node 本地读取，并验证软件变化后拒绝执行；fixture 不是外部机器或生产隔离证据。

实现和检查顺序见[Hub 派发计划](../plans/2026-10-23-hub-capability-command-dispatch.md)。自动选点协调器与输入 target 固定的后续本机接线见 §6.5。

能力 workspace postcheck 只接受已经持久化的 `workspace_digest` descriptor：原命令必须是 owner-scoped、unknown 的 `tool.workspace.write`，其 path、内容摘要、node、resource、Run、binding、generation 和 payload hash 必须逐项回链到当前 unknown Workflow/Step/dispatch。原 reservation 只能保持 `held` 或 `isolated`；postcheck 不新建 admission、预算、generation、Run 或 reservation，也不迁移 target。它必须使用 Workflow contract/binding 中预先声明的独立 `workspace.digest` capability，其动作固定为 `reconcile.workspace.digest`、retry 固定为 `read_only`，Hub 与 Node 都重新验证当前 definition、action registration、offer、policy、credential、allowlist、资源和新鲜度。Hub 仅把该 helper context 注入派生的 `{path}` check；匹配、失配或读失败都只是当前状态 evidence，原 Command、Run、Workflow state 与 reservation 不被改写或释放。descriptor 缺失/篡改、原状态不符、helper 未登记、旧报告或 released reservation 一律拒绝。本机 POSIX/loopback 证据已覆盖上述 slice；原固定 Windows/Linux 矩阵已有后文独立证据，其余跨平台/外部验收仍开放，完整 F4.3/WF-05 未完成，所有 gate 默认关闭。

postcheck 的终态 evidence 与唯一 audit 必须在原 SQLite 事务一起提交；任一步存储失败均保留 pending，重试只消费已保存回执。历史终态缺少 audit 时，从原保存的 state/result/evidence id 补齐，不重新读取文件、排命令或改写原状态/时间戳。已有 audit 必须逐项匹配原 owner、command、actor、outcome、source 与 evidence reference，冲突不覆盖；完整终态查回不取得写事务。相同持久化修复覆盖原 workspace/service inspect/service logs postcheck，不扩大 capability service 路由。实现与独立本机证据见[原子审计计划](../plans/2026-10-30-postcheck-atomic-audit.md)。

已有完整终态和 audit 也不能豁免原命令核验：每次查回均要求原 owner-scoped Command 仍为 `unknown`。归属改变或原命令不存在返回 `command_not_found`/404，原状态不再 unknown 返回 `reconcile_requires_unknown`/409；拒绝时不改 descriptor、audit、helper 或原 Command，不重放 I/O。完整终态分支复用一次原主键索引查询，保持零写事务；历史缺 audit 的恢复仍在原事务复核。三个原 postcheck kinds 的最小修复及十二种边界见[终态保护计划](../plans/2026-10-31-postcheck-terminal-guard.md)。

原能力 helper 的固定 Windows/Linux workspace digest 矩阵在 Windows 与 Linux 实际 Node 上各覆盖 held/isolated × matched/mismatch/read_failed，共 12 项全部通过；原写入、独立只读 digest、未知终态、generation/binding 及预约保持，经两端原 journal 和 Hub SQLite 核验。冻结源码、完整范围与剩余 WF-05 限制见[Native frozen verification](../plans/2026-10-30-capability-platform-postcheck.md#native-frozen-verification)；此试点不是完整 Workflow/生产验收。

后续 Node poll 凭据修复补足首次 claim 的认证边界：HTTP 将鉴权时的机器凭据指纹传入原 claim 写事务，在过期 lease 回收和 queued→leased 之前复核 owner/node/当前凭据。认证后轮换、凭据禁用或节点禁用返回 409/`command_credential_revoked`；该请求不改 Command、attempt、outbox 或旧 lease。旧 token 随后重新认证仍按原合同返回 403。内部未传指纹的原调用兼容；HTTP 不从 body 读取指纹。事务异常回滚并保留原因链，详见[poll 修复计划](../plans/2026-10-23-node-poll-credential-fencing.md)。

### 6.5 协调器自动选点与输入目标固定子集

`RegisteredCapabilityRoute` 是管理员通过原 `workflow_steps` 装配的不可变注册，固定 definition_digest、step、adapter/policy revision、SelectionRequirements 筛选约束和预算预留。它不携带可由用户选择的节点或容量；owner/workspace/template/角色、平台/软件、输入/驻留 alias 与资源需求来自可信注册。实际 input_digest、active budget 由当前原合同派生；合同能力集合必须与登记一致，definition/adapter/policy/作用域冲突拒绝，不从请求或模型推断约束。

原 WorkflowCoordinator 增加可选数据库 I/O port `capability_admission`，复用原动态前驱输出解析、有界 tick、claim/epoch/revision 和 dispatch 生命周期。capability gate-on 且具备有效 Command signing key 才装配 `CapabilityWorkflowAdmission`；无 port/匹配 executor/输入登记时保存等待原因，不回退静态 target。候选只包括 owner/workspace/adapter/policy 精确匹配的原生 remote Node `RunStepExecutor.target`，且配置中的 execution_node_id 等于物理 target；CLI Task、Hub 本地 review/test 和其他 executor 不作为 Node 实现。没有新线程、状态库或执行器。

没有文件输入时先冻结原 resolved contract，再在后续 tick 用原 `CapabilityRepository.admit` 事务选择、预留和保存 binding。输入包含 ArtifactRef 或 CodeRevisionRef.source_bundle 时，先在事务外核对原 scoped artifact 元数据；候选还必须具有完整匹配 ref_key 的 `RegisteredNodeInputs`，每个输入 alias 都被登记的筛选约束允许。第一次在同一短写事务重新核验 claim/当前代/合同/目录/凭据启用/报告/策略/资源，再选择节点、固定 input_target 和原 resolved_digest、签名并入队原 receive Command/outbox。任何写失败全部回滚；没有输入签收前不产生 intent、预算/槽位预约、能力 binding 或 Run。

输入目标一旦固定，后续 tick 只检查该节点。优先级、报告、软件、容量或目标登记改变均不能把本代输入或执行搬到另一台机器。所有原 receive 回执通过签名/作用域/内容核验后，先提交 ready 投影；后续 tick 在同一 admission 事务再次核验原节点和容量，复用原 intent/预算/槽位/binding 原子性。等待接收时不预留执行槽，容量已被其他流程占用则继续等待，不超售。同一物理 target 的旧 held/isolated 占用仍计入。

能力目录与输入 repository 只增加原数据库事务复用及有界候选过滤；外层同库/foreign_keys/事务校验、savepoint 和失败回滚保持原合同。注册查询 O(1)，候选及报告项数均有上限；输入候选匹配 O(nk)，n≤256、每节点别名 k≤64，未引入无界嵌套或逐节点数据库查询。已 sending/bound/unknown 的恢复先查原记录，路由登记被撤销不会创建替代 Run。

本机 SQLite/HTTP 与两个真实进程验证上述闭环，见[协调器计划](../plans/2026-10-23-capability-coordinator-input-pinning.md)。receive/export 仍按 §3–4 独立协议运行；本切片未把它们声明为 capability-bound 动作。能力 postcheck 的 workspace digest slice 已通过 §6.4 固定原生矩阵。后续[自动生命周期探针](../plans/2026-11-01-capability-subprocess-lifecycle.md#verified-native-automatic-lifecycle--2026-11-01)在相同 902 文件冻结上，由原协调器/Run worker 驱动实际 Windows/Linux 固定 workspace write 与独立 read_only digest，8 项全部通过；原 unknown/held 与成功后缺产物的交付状态保持。此显式试点仅关闭固定动作的自动生命周期子集，完整跨节点代码交付、其余平台/资源路径、生产隔离和 F5 仍开放，完整 F4.3/WF-05 未完成，所有 gate 默认关闭。

## 7. 工具接入与 MCP

### 7.1 Fleet 调用 MCP

新增 MCPConnectorAdapter，放在 ToolBroker 的固定权限边界后。第一版只支持管理员固定 endpoint 的只读 HTTP MCP 服务；stdio、动态 URL、自动安装包、浏览器登录与任意插件执行不属于第一版。

登记流程：

1. 管理员登记 connector_id、受限 transport/endpoint、SecretRef、owner/workspace scope、最大工具数。
2. discover 读取有界 tools/list；最多 100 个工具、总 schema 256 KiB、单 schema 32 KiB、递归深度 16，不自动抓取外部 schema 引用。
3. 显式选取工具并固定其 input/output schema、版本/hash、risk、retry_class、时间/结果上限。
4. 形成可信目录项；实际 Run 只暴露所需子集，默认最多 20 个模型工具。
5. 调用前校验输入和当前策略；结果先校验/限长/脱敏再交给模型。

MCP schema/description/annotations 都是外部数据。目录风险由 Fleet 管理，不根据 server 的 readOnlyHint 自动免除审批。工具变化生成待确认 revision，旧运行不能自动执行新版本。

网络沿用精确 origin、DNS 到连接绑定、TLS 和拒绝凭据重定向的既有模式。私网 connector 只能通过管理员登记的精确 alias 和专用策略访问；不能关闭全局 SSRF 检查来接入。

遵守实际协商的协议版本，记录 server capabilities；不把某个 server 的请求格式当作整个 MCP 协议。原始令牌留在 SecretBroker。任何 elicitation/回调请求先映射为用户可见待处理事项，不能自动授权或传递宿主凭据。

~~~text
Native Run → ToolBroker policy → registered MCP tool
           → MCPConnectorAdapter → configured server
           → bounded structured result / ArtifactRef
~~~

MCP 服务不具有 Fleet workflow/审批数据库写权限。调用超时依据已登记 retry_class 进入 failed/unknown；不存在通用“失败自动再调一次”。

### 7.2 外部 Agent adapter

统一逻辑接口，但能力按真实实现声明：

~~~text
submit(dispatch_token, resolved_contract) -> ExternalTaskRef
inspect(external_task_ref) -> ExternalTaskStatus
request_cancel(external_task_ref) -> CancelReceipt
export_outputs(external_task_ref, output_specs) -> list[ArtifactRef]
~~~

能力字段至少包括 submit_idempotent、lookup_by_token、status_evidence、cancel_supported、cancel_confirmable、output_export_supported。未支持的方法返回 unsupported，不以 UI 统一按钮掩盖差异。

外部状态映射：

| 事实 | Fleet 解释 |
|---|---|
| 消息开始发送但未确认 | dispatching；中断后无法核对则 unknown |
| 接收确认、有 task/session 标识 | waiting_external |
| Agent 自报完成 | execution succeeded 的候选；保留 agent_reported 来源 |
| 当前任务的结构化产物已导出且校验通过 | 可进入独立验证 |
| 停止请求发出 | cancelling；没有确证不能写 cancelled |
| 云机休眠/账号失联 | 有界等待，截止后 unknown；不换 task ID 重发 |

现有 web_agent.status/submit/read 提供可复用的受控委托合同。Muse 原生工作区具有独立账号/会话和审批域；新 adapter 的幂等、取消与导出需逐项真实验证，不能仅凭原生 API 有发送功能就声明全部支持。

### 7.3 外部 Agent 调用 Fleet

F5 后再提供窄权限 Fleet MCP 出口，第一版限定：

- workflow.create_from_template：仅允许预授权模板、工作区、预算和当前身份。
- workflow.get：读取该集成身份创建或明确获准的任务。
- workflow.list_artifacts：返回有界元数据；下载仍走单独作用域校验。

集成凭据与 ingest/runner/supervisor/operator 凭据分离。出口不包含 approve、acceptance、任意 shell、凭据读取、策略修改、节点注册或云机创建。外部工具调用必须带稳定 client_token，并绑定调用身份与模板参数 hash。

### 7.4 F5 本机实现边界（2026-10-24）

本节保留 2026-10-24 首批集成的历史范围；后续 Run 授权固定和外部 Workflow 派发分别以 §7.5、§7.6 为准。下述“尚未完成”描述的是该首批切片，不覆盖后续实现。

本机 slice 固定 stateless Streamable HTTP MCP `2026-07-28`，采用 `server/discover`、`tools/list` 和 `tools/call`；协议版本 metadata 与 HTTP version/method/name 头逐项一致。JSON/SSE 每次调用只有一个 HTTP attempt，总读取量 256 KiB、截止最长 30 秒；SSE 支持通知、多行 data 和跨读取的 CR/LF/UTF-8，收到匹配 ID 的最终响应即关闭自有连接，不等待 EOF。拒绝 server-to-client callback，不授权或重放任何回调。私网 alias 仅将精确 HTTPS endpoint 绑定到单个明确私网 IP，DNS 结果在连接前逐项校验并直接用于 socket；无代理与凭据重定向。

connector config 只接受管理员登记的 `read_only` risk/retry。目录刷新在网络 I/O 外保留当前快照；不同待确认 schema 使用不同单调 revision，确认旧 revision 不能批准新内容，并发刷新不能覆盖已确认快照。调用使用固定快照和输入/输出 schema hash；原生本机 Run 通过 ToolBroker 的 owner/workspace、明确白名单与资源 lease 校验，新增 MCP 工具只占原有 20 个模型工具总额度的剩余部分。远端 Node 未接 MCP，默认关闭时不改变原固定工具行为。

Fleet 出口复用原 Workflow application/repository；在写入前验证模板、工作区、身份 scope 和总预算。client_token 的 namespace 由稳定 owner/integration 身份派生，原 workflow_runs 中的 token、workspace、template 在同一读取事务内校验；重建身份对象后可以查询原工作流，同 token 不同有效参数仍冲突。三个工具仅返回既有 DTO/有界产物元数据，下载继续独立授权。

Muse adapter 仅包装既有受控 bridge，声明的幂等/lookup 依赖该 bridge 的稳定 task 事实；其他任务的回执不能成为当前任务成功证据。取消/产物导出 unsupported，接收与 Agent 自报完成分离。目录与身份登记仍由管理员在进程内注入，外部 Agent Workflow 派发、多进程目录持久化、集成配置 UI 和真实 MCP/Muse 试点尚未完成。三个 F5 gate 默认关闭；本机证据不替代完整 F5 或生产验收。具体计划和检查范围见[F5 集成计划](../plans/2026-10-24-f5-integrations.md)。

### 7.5 Run 工具授权与定义固定

§7.1 的“旧运行不能自动执行新版本”适用于排队、worker 重建和目录进程重启。新的本地原生 Run 在原提交事务保存私有 `mcp_tools` 快照：最多 20 个 public tool name，每项仅有精确正整数 `revision` 与 `definition_digest`。摘要覆盖管理员固定的 connector 配置、作用域、risk/retry/时间/结果上限、协议/server 版本，以及工具 input/output schema 和描述摘要；只存摘要，不把 endpoint、schema、描述或 SecretRef/令牌原文加入这个快照。同一 SecretRef 背后的凭据轮换不改变工具授权；更换 SecretRef 标识会改变摘要。

worker 只能暴露和调用原快照中的子集；catalog revision 和完整摘要必须均匹配。目录重启后再次从 revision 1 开始不构成相同定义的证明。缺少 pin、空快照、提交时 gate-off、remote Node 或 acceptance Run 均不从后来的目录获取 MCP 权限；变更或撤销的原选中项在模型请求前失败关闭，调用边界再复核一次。原提交 token 的重复请求先查回原配置，不重新固定新目录。模型和 operator overrides 不能提供 pins；公共 Run DTO 不返回它们。

Workflow 的本地 Native Run 继续使用已登记 target 的不可变 config_snapshot；管理员可在登记时固定相同 pins，执行/恢复不能从可变目录补齐。没有 pin 的既有 target 保持无 MCP 权限。这里不新增持久目录、第二套执行器、worker 或管理端点。修复与证据按[Run 固定计划](../plans/2026-10-25-mcp-run-pinning.md)推进，完整 F5 与真实外部验收仍开放。

### 7.6 外部 Agent Workflow 派发与原任务查回

外部派发继续使用 §5.2 的 `StepExecutor`、原 token、预算/槽位、停止截止与显式 reconciliation；不增加队列或第二套执行状态机。管理员通过 `RegisteredWorkflowStep` / `RegisteredWorkflowPlan.external_target` 固定 owner/workspace/target、adapter/policy revision、agent_id、非秘密配置摘要和六项能力。原 admission 在同一个 SQLite 事务保存此快照、原 intent 和预约；排队后更换注册不能改写已提交快照。未固定外部 target 的 external_agent 步骤不能发送。

新增的 `workflow_external_schema_meta` / `workflow_external_bindings` 只保存兼容性 metadata、固定注册和 I/O 事实：`send_started_at` 在首次外部 I/O 前提交，`task_snapshot` 保存原 `ExternalTaskRef`。它们不是新调度状态。外部表启用时 Workflow minimum_reader_version=3；reader 3 独立识别 capability reader 2 与外部 metadata，gate-off、备份和恢复均须识别并校验。备份按 rowid 每批最多 128 条核对所有历史注册、token、payload、任务引用、外键和摘要，恢复继续 frozen；它不宣称备份了外部账号或 Muse 独立 journal。journal 缺失/配置改变无法证明原任务不存在，保留 unknown 和原预约，不能凭恢复库换任务重发。

`ExternalAgentAdapter.lookup(dispatch_token, resolved_contract)` 明确区分“确认无原任务”的 `None` 与查询错误。恢复 sending 总是先查询；查询错误不得当作不存在；仅在接收端声明幂等且支持 lookup 时，才允许用完全相同 token/payload 再调用 submit。初次提交也以 `send_started_at` 的短事务 CAS 区分首次与重入；不具备幂等/lookup 的 adapter 在不确定窗口永久停止自动重放。绑定保存成功后仅 inspect；绑定前崩溃可从原 journal 查回。每次外部 I/O 前核对固定注册与实际 adapter 配置；配置漂移不能把旧 token 发给新账号、journal、scope 或实现 revision。查回既有事实优先于新发送权限，取消/冻结/禁用 workspace 不隐藏原任务。

Muse 的显式 lookup 只读原 journal，核对 task_id、scope 和原 prompt 摘要，不触碰 UI、不把普通 read 的 task_not_found 当作未发送证明。Workflow 使用完整 canonical resolved JSON 作为 prompt，上限 8192 字符，不能截断 objective/约束。Muse Workflow 注册需要管理员明确的 account_ref / transport_ref，并先显式 `MuseBridge.initialize()` 固定 journal 身份；配置摘要覆盖这些标识、workspace、bridge journal 的绝对路径、持久 journal identity 和 adapter 协议版本。身份读取不创建数据库；原库缺失、相同路径换库或 identity 丢失均不能证明未发送。一个 journal 仍只属于一个明确附着的账号；管理员标识和本机 fixture 不证明真实账号身份或权限已验收。

Muse controlled bridge 的 journal task_id 固定等于原 dispatch token。submit 返回其他 task_id 时必须以 uncertain 的 `external_task_mismatch` 拒绝，不保存该任务引用、不把它解释为原任务不存在、不重放发送；原 Workflow 进入 unknown 并保留预约。后续 inspect/cancel/export 同样只接受 `agent_id=muse`、`external_id=dispatch_token`、`session_id=None` 的引用，在 bridge I/O 前拒绝旧的错误绑定或伪造引用；不能借 fallback 查询其他任务。该约束只针对 Muse journal，不限制其他 adapter 的独立 task/session 标识，不重写历史记录、schema、配置摘要或预算。显式恢复仍只查原 token/journal，unknown 的受控 reconciliation 规则保持不变。修复实施见[Muse 任务身份计划](../plans/2026-10-26-muse-external-task-identity.md)。

本机派发首版接受无文件/上下文输入、无 Node capability_requirements、无 output_specs 的 external_agent 合同；带这些要求时在发送前明确拒绝 `external_contract_unsupported`，不能忽略、把回复文本伪装成 ArtifactRef 或调用尚未实现的 exporter。真正的结构化产物交接仍按 §7.2 独立开发和验收。等待输入映射为 `waiting_external` / `external_input_required`，不能伪造 Fleet approval。接受、运行和 Agent 自报终态的有界 observation 持久保存 `evidence_source`；`agent_reported` 从不生成独立 validation evidence、输出或 acceptance。费用缺少可信实际用量时继续 unknown。

状态与取消回执必须同时精确匹配原 `ExternalTaskRef` 的 `external_id` 和 `session_id`；仅有 session 的任务也必须返回该 session，缺失或其他 session 不能因 `external_id=None` 相等而被接受。两个字段均存在时不能只核对其中一个。取消先查原终态，requested 与 confirmed 继续分离；只有 adapter 声明 cancel_confirmable 且返回当前任务的 confirmed receipt 才能写 cancelled。Muse cancel/export 仍 unsupported，协调器仅检查原任务，截止后 unknown 并保留资源。`platform_external_agents_enabled=false` 阻止新 admission/submit，包括此前已经 admitted 的 intent；已登记的原任务允许受控 lookup/inspect/停止核对，构造不执行外部 I/O 或启动线程。同步边界使用有界 transport 和 `run_owned_io`；异步 dispatch/stop/reconcile 使用显式持有 task 的 `wait_owned`，只向自有任务发送一次取消，并在重复取消和 timeout 竞争中等待收尾、保留 worker 原始异常链，兼容 Python 3.10。查询后的再次发送须重新核对原 lease/control/授权/预算/预约/截止，不能沿用查询前的授权。

本节的本机受控实现已有双 Python 关联回归、真实 SQLite/journal/协调器/备份、HTTP DTO 与前端解析，以及完整浏览器门禁证据。WF-06 的 128 selectors / 261 cases 全部通过；全仓 4836 passed、5 项平台 skip，严格门禁仍因缺少 Linux 必需证据退出 1。没有真实 MCP/Muse 调用，外部文件/产物接线与跨进程目录管理仍开放，不能据此认定外部试点、完整 F5 或生产验收完成。实施见[外部派发计划](../plans/2026-10-25-external-agent-workflow-dispatch.md)，完整证据与边界见[实施进度](../../platform/workflow-platform/implementation-progress.md)。

### 7.7 Fleet 集成身份持久化与撤销

§7.3 的三工具身份必须在共享 Workflow SQLite 中持久登记，不能把启动时的进程内对象作为请求实时授权源。登记由管理员可信装配执行，不增加外部可写管理端点。`IntegrationIdentity` 是无 I/O 的不可变快照：owner/integration ID、模板/工作区 allowlist、三个既有 scope 的子集、产物/Token/请求上限，以及 Bearer 的 SHA-256 摘要。令牌保持 32–512 个可打印 ASCII 字符；每种 allowlist 输入最多 128 项且不得重复，每项为有效引用，scope 可以为空。产物上限 1–100、Token 上限 0–2000000、请求上限 0–1000；整数拒绝 bool/float。快照为规范 UTF-8 JSON、最多 64 KiB，definition_digest 固定全部登记字段；不保存令牌原文。

`workflow_integration_schema_meta` / `workflow_integration_identities` 使用独立 schema 1；存在该域时 Workflow minimum reader 为 4，capability reader 2、external reader 3 的识别保持独立。token_digest 具有全局唯一索引，认证只点查一条记录；scope/模板/工作区使用有界集合。相同 owner/integration ID 仅允许完整快照相同的幂等登记；不同内容或另一身份重复的 token_digest 返回冲突。撤销由管理员以 owner/id 和 expected definition_digest 显式执行，重复操作保留首次撤销时刻，不能用启动登记或旧对象重新激活。变更授权/凭据需登记新身份并撤销原身份；不隐含转移旧身份的 Workflow 归属。

认证返回完整校验的不可变身份，创建/查询仍须在原 Workflow 事务内核对持久身份与快照摘要。创建在返回既有幂等回执的读取事务和新建 Workflow 的写事务中都检查；模板、工作区、owner、namespace 和总预算在写入前再次验证。撤销提交早于 admission 写事务时禁止创建；读取以同一授权/详情事务的快照为线性化点。撤销不改写、取消或重放既有 Workflow/Run/Task，管理员仍可用原 operator 权限核对和停止。

默认 gate 不变；关闭 gate 不创建身份表或执行认证 I/O，但已存在 schema 必须被 reader/备份识别。启动对象只作幂等登记；无注入对象的新进程也能认证原有效身份。运行时认证/授权与启动均检查完整 Workflow 兼容 schema，不能在另一进程提高 minimum reader 后继续使用旧 reader。未知、残缺 schema 或损坏/非规范快照失败关闭，错误保留 cause，公开响应不泄露摘要、快照或令牌。

启动历史校验在 schema 短写事务结束后使用独立 WAL 只读事务，不占 SQLite writer。备份即使 gate 关闭也以 rowid keyset 每批最多 128 条检查全部身份，包括非正 rowid 和已撤销记录。明文与加密恢复均保持 Workflow frozen，并禁用恢复副本所有集成凭据，避免旧备份复活备份后撤销的令牌；源库不变，namespace/历史保留，重复登记不能解除恢复禁用。新身份需管理员另行登记，不自动继承旧身份归属。本切片不含持久 connector catalog、配置 UI 或真实试点；见[身份持久化计划](../plans/2026-10-25-fleet-integration-identities.md)。

### 7.8 MCP 目录持久化与跨进程审批

本节落实 §7.1 的管理员目录与 §7.5 的固定 Run 权限，不增加外部管理端点。生产装配由管理员提供 `mcp_connectors` 运行时适配器，启动仅幂等登记固定配置，不做 discovery/network/secret resolution。目录存储使用同一 platform.db 的独立 `mcp_catalog_*` schema 1，与 Workflow gate 无依赖；只有创建目录域才将平台 reader 门槛从 3 提高至 4，默认关闭的新库仍为 3。gate-off 启动和备份同样识别已存在的目录；未知、残缺 metadata、表或快照失败关闭。

connector_id 全库唯一，最多 128 个历史登记（含撤销）；相同 ID 只允许完整固定配置相同的幂等登记，不能通过启动对象修改 endpoint、SecretRef、owner/workspace、工具选择或限额。配置最多 8 KiB；发现快照最多 512 KiB，并继续遵守 100/20 工具、256 KiB 总 schema、单 schema 32 KiB 与深度 16 的原限制。只有 SecretRef 被保存，运行时 transport/private alias 和 SecretBroker 由可信进程显式附着，不序列化或自动创建私网策略；没有对应适配器的记录不暴露工具。

首次发现沿用可信登记下的首次准入；后续所有发现内容变化（协议/server 身份、capabilities、选中工具的描述、schema、annotations）均生成持久待审版本。工具排序不构成变化。revision 在同一 connector ID 下严格单调且不超过 JSON 安全整数；重复相同发现不增长，返回旧内容也不能复用旧 revision。审批只接受当前待审精确 revision，另一进程刷新、审批或撤销使过期 CAS 失败。网络发现放在事务外，短写事务核对 epoch；旧批准快照在等待审批时继续生效，不能被新描述或 mutable dict 污染。

每次 Run 固定、模型工具声明与实际调用都从同一读取快照核对当前持久目录，再比较原 Run 的 revision/definition_digest；跨进程审批或撤销不依赖进程缓存失效通知。调用以授权读取为线性化点，之后仅执行该已固定快照的一次网络调用，撤销不声称中断已开始的调用或重写其结果。返回的 discovery、schema 与 DTO 均为独立拷贝。进程本地目录仅保留给显式注入的受信任测试/嵌入式调用，不作为默认生产装配的持久授权源。

管理员撤销要求固定配置摘要，永久保留记录且不可由重复登记重启激活。恢复副本禁用全部已有 connector；源库不变，重复登记、刷新和审批都不能解除禁用，需要新 ID 的管理员登记。明文/加密备份按 rowid keyset 每批最多 128 条校验全部记录（包括撤销和非正 rowid），快照必须 canonical UTF-8、hash 与索引字段一致。此切片不含配置 UI、外部输入/产物接线或真实服务试点。

### 7.9 MCP 目录管理界面与管理员 HTTP 边界

本节在 §7.8 的持久目录之上增加本机可验收的管理入口。可信配置仍负责不可变登记、运行时适配器附着及有界 discovery/refresh；HTTP/UI 不能登记 URL、修改 scope/SecretRef、解析密钥、附着 transport、触发网络发现或调用工具。配置变更继续使用新 connector ID。界面提供目录查看、完整待审定义审阅、精确版本批准和不可逆撤销；这只是集成配置 UI 的 MCP 目录子集，Fleet 身份配置、外部 Agent 输入/输出及真实试点继续开放。

仅在 platform 和 MCP gate 开启且装配持久目录时注册 `platform_mcp_admin` 蓝图及管理服务。账户模式下仅当前有效的 `admin` cookie 身份可以进入；普通账户、外部 Fleet Bearer、机器凭据、伪造 CF 头都不能获得目录元数据或改变记录。无账户模式的 CF/DEV operator 缺少管理员角色证明，管理端点返回 403，仍可按原可信装配管理目录。`/api/accounts/me` 仅向具备该服务的管理员发布 `features.mcp_catalog_management=true`；其他情形不发起界面目录请求。管理权限以本次有效管理员身份读取为线性化点；已获准的单次短事务可能在会话撤销/角色变更后结束，后续请求必须重新鉴权。没有后台管理任务或跨账户浏览器缓存。

| HTTP | 请求 / 返回 |
|---|---|
| `GET /api/platform/v1/integrations/mcp` | 可选唯一 `after`、`limit`；limit 默认/最大 8、最小 1，after 是有效 connector ID。返回 `{ok:true,connectors:[MCPConnectorSummary],next_cursor:string|null}`，按 BINARY connector ID 排序，用主键 keyset 查询、最多读取 9 条，不使用 offset 或逐项查询。|
| `GET /api/platform/v1/integrations/mcp/record?connector_id=<id>` | connector_id 为唯一必需 query；返回 `{ok:true,connector:MCPConnectorDetail}`。不存在为 404。|
| `POST .../admit?connector_id=<id>` | 仅 `{expected_epoch:integer,pending_revision:integer}`；两者为 1–9007199254740991 的精确整数，拒绝 bool/float/重复 JSON 键/额外字段。|
| `POST .../revoke?connector_id=<id>` | 仅 `{expected_epoch:integer}`；同样严格校验。服务读取原固定配置摘要，在原撤销事务同时复核 expected_epoch，原摘要不进入公开 DTO。|

所有响应（包括错误）为 `Cache-Control:no-store`；写请求要求账户配置的精确 Origin 和 JSON，最大 2 KiB。拒绝缺失前置条件（428）、非法字段/值（400）、错误 Content-Type（415）、超限（413）。列表拒绝重复/未知 query 和非规范十进制 limit；详情和写入仅接受唯一必需 connector_id query。ID 沿用原字符合同（包括单独的 `.` / `..`），不放在会被浏览器归一化的路径段中。不跟随任何客户端 URL。管理员鉴权先于目录访问及请求内容处理。错误统一为原 HTTP 错误 DTO，保留内部 cause，固定公开 detail 不回显 endpoint、SQL、宿主路径或 SecretRef。

`MCPConnectorSummary` 仅包含 `connector_id/owner_id/workspace_id`、不含 path/query 的 `endpoint_origin`、固定工具名称集合、read_only risk/retry、原 timeout/result 上限、`epoch`、active/pending revision、`state`、`adapter_attached`、`runtime_ready` 及 UTC ISO-8601 `created_at/updated_at/revoked_at`。state 优先级为 `restore_disabled > revoked > pending > active > registered`；pending 时旧 active 仍可用，runtime_ready 表示本进程具备适配器且有未禁用的批准快照，不承诺远端健康或真实可达。适配器状态仅是本进程诊断，不作为持久授权。已撤销、恢复禁用及未附着记录仍在管理列表显示，不因此暴露 Run 工具。

`MCPConnectorDetail` 在 summary 外提供 active/pending discovery 的完整受限副本，包含 revision、协议/server、capabilities、工具 description/schema/annotations 和规范内容 SHA-256；无快照为 null。两份 discovery 各继续受 512 KiB 上限约束。页面可以逐项检查内容与摘要；外部内容只以文本渲染，不能创建 HTML、脚本、链接或新权限。DTO 不含 SecretRef、凭据、完整 endpoint、private alias、transport、私有配置快照或配置摘要。目录记录完全校验后才输出，不以 UI 字段掩盖损坏记录。

审批必须在原 SQLite 写事务同时匹配当前 epoch 与当前 pending revision，不允许重复审批当前已批准版本。成功返回该事务产生的新 detail；另一进程刷新、审批、撤销造成 409，原快照不变。撤销同样核对 epoch；重复提交当前禁用记录及其当前 epoch 只返回原事实，保存首次撤销时间。旧 epoch 的重试返回 409，客户端读回后核对，不自动补发。恢复禁用不会变成普通撤销或被重新启用。更新不创建/取消/重放 Run、Workflow、Command，不更改 unknown 或 held/isolated 预约。

HTTP 入口只记录成功管理动作的 request ID、当前管理员 ID、connector ID、动作和结果 epoch；错误沿用原全局错误日志，不能记录正文或 discovery。该记录是边界日志，不冒充持久审计事件数据库。列表/详情/管理均不访问网络或 SecretBroker。SQLite I/O 保持既有同步 Flask 模型和 10 秒锁等待，禁止为管理入口另起失控线程；本切片不把现有 SQLite 驱动宣称为异步。

共享 HTTP 请求的既有默认 15 秒/最大 60 秒截止和外部 AbortSignal 必须覆盖响应头及完整 JSON 正文读取；成功、错误或取消后统一释放 timer/listener。正文取消/超时仍返回原 `request_timeout`，合法 HTTP/解析错误保留既有错误外型和原始 cause，不能在收到响应头时提前解除取消。

入口挂在“账号设置”的管理员区域，复用原静态 SPA 和同一请求边界。目录按需分页，选中项显式加载详情；审批前显示精确 connector、epoch、待审 revision 并要求核对定义，撤销有明确不可逆确认。网络/超时使结果不明时只提供重新读取；409 读取最新详情并要求重新审阅，不能自动用新 epoch 重试写入。页面切换、销毁和身份变化取消本地 fetch/对话框；取消 fetch 不代表服务器撤回事务。恢复页面重新读取当前目录，无轮询、网络 discovery 或自动工具调用。原生 dialog 提供键盘/Escape/焦点恢复，窄屏不横向溢出，所有状态/错误有可见文字和 live region。

实现与验证见[MCP 目录管理计划](../plans/2026-10-25-mcp-catalog-administration.md)。本节在实现之前固定契约；完成证据以实施进度为准。

### 7.10 Fleet 身份管理界面与撤销确认

在 §7.7 的不可变持久身份之上，提供账户管理员的离线查看与撤销入口。只有 platform、Workflow 和 Fleet MCP gate 均开启且已装配持久身份仓库时注册 `platform_integration_admin` 与管理服务。`/api/accounts/me` 仅向有效 cookie 管理员发布 `features.fleet_identity_management=true`。鉴权、精确 Origin、no-store、2 KiB 严格 JSON、错误 cause 与安全日志沿用 §7.9 的账户边界；没有管理员角色证明的旧 CF/DEV operator 返回 403，普通账户或机器/Fleet 凭据不能读取身份元数据。授权线性化点仍为每次请求读取有效管理员身份；没有后台授权缓存。

| HTTP | 请求 / 返回 |
|---|---|
| `GET /api/platform/v1/integrations/fleet` | 可选唯一 `after_owner_id` 与 `after_integration_id`，二者必须同时出现；可选规范十进制 limit，默认/最大 8、最小 1。返回 `{ok:true,identities:[IntegrationIdentitySummary],next_cursor:IntegrationIdentityKey|null}`。使用 `(owner_id,integration_id)` BINARY 复合主键 keyset，最多读取 9 条；无 offset、全表加载或 N+1。|
| `GET .../record?owner_id=<owner>&integration_id=<id>` | 仅接受两个唯一必需 query；返回 `{ok:true,identity:IntegrationIdentityDetail}`；不存在为 404。|
| `POST .../revoke?owner_id=<owner>&integration_id=<id>` | body 仅 `{expected_state:"active"}`。缺失为 428，其他类型/值或额外字段为 400；返回 `{ok:true,revocation:{owner_id,integration_id,state:"revoked"}}`，这是原写事务回执，时间等详情由独立 GET 读回。|

ID 沿用原有效 UTF-8 引用合同：1–128 bytes、无 NUL、无孤立代理码，不收窄为 MCP 名称规则、不做 Unicode 归一化，含 `/`、`?`、`.`、`..` 等值通过 query 精确保留。分页按 UTF-8 BINARY 顺序；浏览器不能用 UTF-16 字符串顺序误拒绝原合法身份。页面 Map 使用无歧义复合键，点选均摊 O(1)。记录数量没有新增总上限，每次读取/响应都必须有界。

`IntegrationIdentitySummary` 只含 owner_id、integration_id、三个既有 scope 的有序子集、template_count/workspace_count、max_artifacts/max_budget_tokens/max_budget_requests、state、UTC ISO-8601 毫秒 created_at/revoked_at。state 优先级 `restore_disabled > revoked > active`；空 scopes 仍是有效登记，不推导为可调用或健康。`IntegrationIdentityDetail` 包含 summary 与完整有序 template_ids/workspace_ids（每组 1–128 项）。两种 DTO 均不含令牌、token_digest、definition_digest、私有快照、namespace 或任何新授权；完整原记录校验失败返回 503，不能输出部分有效字段。列表/详情不调用模型、MCP、Muse、SecretBroker 或 Workflow worker。

登记定义不可修改、删除或复用 owner/id，所以无需增加伪 revision 或公开凭据相关摘要。服务按 owner/id 读取私有原摘要，在原 `revoke` 写事务保持 `expected_digest` 精确校验，并增加可选 `expected_state="active"` 前置条件；旧可信调用不传此条件时保持既有幂等撤销合同。事务内已撤销或恢复禁用返回 409，不改变首次撤销时间或恢复禁用事实。先于该事务提交的并发撤销/恢复禁用必须被拒绝；原摘要不同仍冲突。管理入口不提供登记、改 scope/额度、令牌签发/轮换/展示、恢复启用、删除、取消或重放既有任务的操作；这些配置仍由可信装配提供。

界面挂在账号设置的独立 Fleet 身份区域：按需分页和详情，展示全部授权模板/工作区、预算和禁用原因，所有引用只作文本。撤销必须在原生 dialog 中显示精确 owner/id 和不可恢复含义，要求确认已核对权限。成功后独立读取详情；回执丢失/超时只提示重新读取，409 自动读取当前事实并要求重新审阅，绝不重放 POST。读取失败不把撤销提交说成失败或成功；页面切换/销毁/身份变化取消 fetch 和对话框，恢复时重新读取。复用现有样式与请求截止/取消；键盘、Escape、焦点、390px 无横向溢出和 live region 均纳入真实浏览器验证。

成功 HTTP 日志只写 request ID、管理员 ID、owner/id、revoke 和 revoked；采用结构化 JSON 字符串避免合法控制字符打断日志，不写摘要、正文或 allowlist。错误沿用原全局边界日志并保留内部原因链。SQLite 继续使用同步 Flask 和原 10 秒锁等待；不新增线程、schema、凭据域或工具权限。本节仅闭合 Fleet 身份查看/撤销子集；其余集成配置、外部文件/产物和真实试点仍开放，所有 gate 默认关闭。

实现与验证见[Fleet 身份管理计划](../plans/2026-10-25-fleet-identity-administration.md)。本契约先于代码落盘，完成状态以实际证据为准。

### 7.11 外部 Agent 文本文件输入与固定请求

本节扩展 §7.6 的本机派发子集：只支持已显式选择、按原 owner/workspace 授权的 `ArtifactReference` 文本内容。前驱输出必须先经原 resolver 固定为同样的引用。每项及全部逻辑输入之和最多 8192 bytes，类型只接受 `text/plain`、`text/markdown`、`application/json`；严格 UTF-8 解码，保留完整内容，不做替换、摘要或截断。二进制、code_revision、Memory/summary、Node capability_requirements 和 output_specs 仍在发送前拒绝；Muse cancel/export 仍 unsupported。该功能不是任意本地文件、URL、远程附件上传或产物导出的授权。

无输入任务沿用原 `resolved.snapshot` 作为 prompt，字节语义不变。有文本输入时 prompt 是规范 JSON：`{schema_version:1,resolved_contract:<原 resolved 对象>,text_inputs:[{ref_key,artifact_id,sha256,size,content_type,text}]}`，数组顺序与 resolved 合同一致。完整 prompt 继续最多 8192 Unicode 字符；文本的原始 UTF-8 字节必须逐项符合固定 size/SHA-256。超限或不兼容时明确失败，不能只发送引用、截断数据或偷偷扩展外部上下文额度。输入内容是资料，不授予工具权限，不生成验证或接受事实。

新增纯领域 `ExternalAgentRequest` 私有快照，精确编码 `{schema_version:1,prompt}`，最多 65536 bytes。首次外部 I/O 前，在原 `prepare_send` 写事务同时固定请求与 `send_started_at`；原 lease、control、工作区授权、预算预约和固定 target 检查仍然执行。传入 now 不得早于记录的 updated_at，时钟回退返回 `invalid_external_timestamp`，原请求/时间/任务事实不变；不以回退时间复用发送授权。采集失败时没有外部 I/O，也不记开始发送。已固定请求不可改写；后续 lookup 与获准的同 token 幂等发送使用原完整 prompt，不重新采集内容。显式 resend 前复核原 artifact 的当前 scope、digest、size 和 content_type；失权、移除或变更不能产生新发送。原任务 lookup/inspect 可依赖已固定请求继续核对。绑定已存在时不会为恢复读取文件或重发。

采集只通过作用域 metadata 和 caller-owned verified descriptor，读取上限为固定 size+1（≤8193 bytes），再次核验实际长度与摘要。相同 artifact_id 使用有界字典只读一次，但每个逻辑输入仍计入完整 prompt/总量预算；冲突引用拒绝。外部文本直接来自 Hub 产物，不走 Node receive/alias；同时启用 transfer gate 时，协调器和原 admission 仍只对 Node 步骤要求 Node 回执，external_agent 的 freeze 不接受 Node input_target。外部 target/请求授权照常校验，不借步骤名绕过 Node 接收规则。所有 I/O 沿用 `run_owned_io` 生命周期，无数据库事务跨文件或外部调用，不引入新 worker、队列、重试策略或内部空壳接口。错误保留 cause，日志/HTTP DTO/事件不携带私有 prompt 或文件正文。

`workflow_external_bindings` 增加 nullable `request_snapshot`，独立 external schema/reader 升为 2，Workflow minimum reader 升为 5；无外部域的数据库不提高 reader。启用时事务性升级 schema 1，保留旧 target/task/token/时间/预约；reader 5 仍能在 gate-off 状态只读 schema 1。旧版已开始且无文件输入的记录可从原 resolved 快照确定性恢复完全相同的 prompt；schema 1 不能承载文本输入。schema 2 的文本输入记录若已开始但缺少原请求，必须 fail closed。未知版本、半迁移、私有内容/引用不匹配或损坏均拒绝读取/发送/备份，不能补写新的“原请求”。备份继续按 rowid 每批最多 128 条验证完整历史请求，恢复 frozen，不恢复外部账号或独立 journal。

验证必须覆盖完整 UTF-8/空文件、重复输入与读取预算、错误 metadata/字节/摘要/MIME、完整 prompt 上限、首次事务回滚与采集失败、丢回执/重启原 prompt lookup、失权拒绝 resend、旧 schema 1 只读与原子升级、gate-off/未来 reader、加密及普通备份恢复、历史最后一批损坏，以及原 unknown/held/isolated、配置、预算和取消回归。真实外部服务仍单列 `not_run`。

### 7.12 外部 Agent 的显式 Memory 输入与版本权威

本节描述外部输入的首版边界。后续本地 Native Run 的完整 Memory 与文本产物消费分别按[执行契约 §5.3、§5.4](2026-10-20-workflow-execution-contract.md)实施；下文“本轮仅 external_agent”的描述不覆盖这两项后续扩展，CLI、summary 和远程 capability context 继续拒绝。

本节扩展 §7.11，仅把现有 owner-scoped `platform_memory_items` 作为 `kind=memory` 的权威。调用方必须显式选择 ref_id 和精确 revision；不搜索、不选“最新”、不自动摘要。Memory 没有 workspace 字段，授权是原 owner 范围与当前 Workflow workspace 授权的交集；不得虚构 Memory 的 workspace 归属。当前表原地更新、没有历史版本，因此首次采集仅接受仍存在、enabled=1、revision 精确匹配的记录。summary 没有独立持久版本权威，继续拒绝；Native/CLI/能力执行本轮不接收 context_refs，只允许 external_agent 实际消费 Memory，不能把裸引用当作正文已交付。

现有 Memory 从 revision 0 开始。WorkContract schema 1 的所有 revision 继续为正整数；新增 WorkContract schema 2，仅允许 memory revision 为 0 至 JSON 安全整数，其他字段/校验/上限与 schema 1 相同。WorkflowDefinition 和 resolved 外层仍为 schema 1，明确允许嵌套的 WorkContract 1/2，完整 canonical snapshot 保留嵌套版本；不改变旧 digest/prompt。持久保存任何 schema 2 合同前，在原创建事务把 Workflow minimum_reader_version 提高至 6；reader 6 不依赖外部域存在。备份/恢复须拒绝 schema 2 数据与低于 6 的 reader 标记；旧 reader 不能被诱导为能解析新版合同。

读取端一次查询最多 16 个不同 memory_id，显式投影有界字段，并在同一只读快照内获得完整选择。不可变 Memory 值精确为 `{memory_id,revision,kind,title,content,tags,source}`：沿用现有 Memory 的 kind、字段字节数和 tags 上限，要求规范的原存储文本、唯一有序 tags、合法 ID、精确整数 revision；所有字段完整 UTF-8，不修复损坏数据。enabled 的持久值必须是整数 1，不能把其他值强制转为 true。重复 ID 同 revision 可用于多个逻辑 ref_key，但只读取一次；同 ID 异 revision 拒绝。未知/失权/禁用/版本变化、损坏或超限均在外部 I/O 前明确失败并保留 cause。

每项 Memory canonical 值的完整 UTF-8 字节都计入预算；Memory 与 §7.11 文本 artifact 的全部逻辑输入共享 8192 bytes，同一个值的重复逻辑输入重复计费。完整 prompt 仍最多 8192 Unicode 字符、私有请求仍最多 65536 bytes；不截断 title/content/tags/source，不复用原会截断的 Memory 渲染器。只有存在 Memory 时使用新 prompt envelope：`{schema_version:2,resolved_contract:<原对象>,text_inputs:<原数组>,memory_inputs:[{ref_key,memory_id,revision,sha256,item:<完整 Memory 值>}]}`。sha256 固定 canonical item 的全部字段，数组按合同顺序；没有 Memory 的旧无输入和纯文本 prompt 字节不变。正文仅是资料，不能增加工具、策略、validator 或接受权限。

复用原 request_snapshot 与 send_started_at 的原子提交。首次发送采集完整 Memory；已保存请求用于 lookup、恢复和获准的同 token 幂等发送，不重新生成原 prompt。新发送/获准 resend 前从当前 owner 权威再次批读，并同时核对 revision、enabled 与完整原值/hash；当前记录被编辑、禁用、删除或同 revision 内容损坏均不得发送。授权读取是该次发送权限的线性化点，不承诺中断随后已经开始的外部调用。原 Task 已绑定或需查回时不依赖当前 Memory 仍存在；更新和撤销不重写历史请求，不换任务、不重放写入、不释放 unknown 的 held/isolated 预约。

独立 external schema/reader 升至 3，沿用原表和请求列；启用时在原短事务内从 1/2 升级，schema 1 补 nullable 列，schema 2 仅升级 metadata，随后 Workflow reader 至少为 6。gate-off 仍能只读 schema 1/2；schema 1 禁止文件输入，schema 1/2 禁止 Memory，已开始的 Memory 任务缺原请求必须 fail closed。六项 adapter 能力、target schema 1、Muse journal 身份、取消/export unsupported、预算/lease/control/Node 收发边界保持原合同。备份仍按 rowid 每批 128 条验证全部历史私有请求，包括当前 Memory 已变化或删除的旧请求；恢复 frozen。不得在事件、operator HTTP DTO 或日志附带正文/私有快照。

复杂度为一次有界索引集合查询和 O(n+B) 校验/组装，n≤16、B≤8192；字典去重/匹配均摊 O(1)。I/O 复用 owned executor，不增加服务、线程、队列、搜索或外部依赖。验证包含 revision 0 兼容屏障、真实 Memory 更新/禁用/owner、完整 Unicode/预算、损坏记录、混合输入、首次回滚、丢响应及重启原 prompt、撤销后 lookup 可用但 resend 禁止、旧 schema 1/2 原子迁移、未来 reader、gate-off 和普通/加密备份历史闭包，以及原 Node/postcheck/unknown 安全合同。真实服务仍记为 not_run。实施见[Memory 输入计划](../plans/2026-10-25-external-agent-memory-inputs.md)。

### 7.13 外部结构化产物首版交接决策（2026-10-26）

用户已选定第一版使用已有 Hub `ArtifactRef`：adapter 返回经现有产物入口发布的引用，Hub 必须重新核验 owner/workspace、完整字节及其 hash/size，并精确绑定原任务、dispatch token、当前 generation 与 `output_key`。不得用聊天回复、自报成功或未经发布的路径冒充产物。第一版不创建远程文件下载/导入通道。该决定解除交接方向的歧义；具体强类型回执、幂等发布、取消/崩溃窗口和历史恢复合同仍须在下一实施切片定义并测试。当前 exporter 仍未实现，`output_specs` 继续按 §7.6 拒绝，不能因这项方向选择宣称已经支持。


#### 7.13.1 首版 ArtifactRef 导出精确合同

本节是已批准 Hub ArtifactRef 方向的实施细化。仅显式声明 output_export_supported 的可信登记 adapter 可准入含 output_specs 的外部步骤。仍拒绝 Node capability_requirements 和 summary；Muse 当前继续声明 export unsupported。原无输出请求及结果字节保持兼容；不增加外部下载/上传 HTTP 入口。导出是只读查回既有发布引用，不执行生成、复制、覆盖或新远程任务。

强类型导出集合精确为 schema_version=1、workflow_id、step_key、generation、task（原 ExternalTaskRef 的 agent_id/dispatch_token/external_id/session_id）及 outputs；每项精确为 output_key 和 artifact={artifact_id,sha256,size}。所有标识/大小按既有引用规则，generation 为正 JSON 安全整数，bool 不作整数。集合最多 16 项，output_key 唯一，按 output_key 排序规范化；所有必需输出必须存在，未知输出/重复键/冲突 artifact ID 拒绝，允许缺少 optional 项和全 optional 空集合。任务四项、workflow/step/current generation、原 token/resolved digest 和 target 配置必须逐项绑定。adapter.export_outputs(task, output_specs) 返回这个不可变集合；output_specs 仍只包含原合同，不接受临时参数覆盖。导出失败、任务不符、字节或授权不符保留 unknown/原预约，不声称执行失败或已验证。

外部状态 succeeded 且来源 agent_reported/confirmed 后才查询导出；原状态来源保留。Hub 先按 owner/workspace 核验每个已发布 manifest，再通过 caller-owned verified descriptor 分块读取完整字节并自行核验 hash/size，拒绝未知、隐藏、外来、超限、短读、超长或篡改文件。artifact 输出遵守 max_bytes 和 256 MiB 硬上限；全集合不同 artifact 的字节总量最多 256 MiB。structured 输出必须为完整有界 UTF-8 JSON 对象（64 KiB/深度32/无重复键或非有限数），按冻结的内联 result_schema 用现有 Draft202012Validator 检查；禁外部 schema 引用，不抓取网络。code_revision 输出是原 F3 的 schema_version/source_revision/executor_id 描述 ArtifactRef（64 KiB），按冻结 result_schema 校验 source_revision，并流式核验 source_bundle 的固定 repository/base/head/tree、tar成员与 blob/tree（既有 10 MiB/路径/链接/文件数限制），额外把 bundle 引用纳入原闭包。完整性或结构合格不生成 workflow_evidence，也不代表独立评审/测试通过或人工接受。

ExecutionObservation 的私有导出事实在原 apply_observation 短事务内连同输出引用、subject、状态、事件、预算/槽位事实原子提交；原 claim/epoch/revision、binding、current generation、workspace enabled、取消、frozen 与截止在事务内再次检查。pause/drain/关闭新派发不撤回已准入原任务的只读查回；取消/frozen/失权/截止阻止新输出绑定。原 unknown 仅由显式 reconciliation 可提交终态，导出不能自行释放 held/isolated。public DTO/事件保留原状态来源及已有输出元数据，不包含私有请求、导出对象或外部原始回复。

私有 observation_snapshot 保存 canonical 导出集合；已有字段上限 16384 bytes 不放宽。subject_digest 固定 resolved_contract_digest 和按 output_key 排序的 artifact 引用，code_revision 还包含其 bundle 引用；不混入 lease/时间/状态来源。首次含输出 admission 在原事务提高 Workflow minimum reader 至 8；外部 schema 保持 3，旧记录不主动升级。reader8 可读旧记录，低 reader 不得承载输出合同。原无输出 observation 字节不变。相同原任务/集合重读不重复事件、引用或 revision；已提交集合不可替换，配置变化不触发新导出；恢复先查原持久事实。导出前或读后提交前崩溃可只读重查同一任务，禁止重发原写入。提交后/响应丢失重启从原 observation 恢复；历史验证按 128 行 signed-rowid keyset 检查集合、subject 与引用闭包，gate-off/备份同样拒绝损坏或缺失，普通/加密恢复继续 frozen。

### 7.14 显式版本摘要权威与外部消费（2026-11-11）

本节扩展 §7.11–§7.13，取代其中 summary 尚不支持的历史限制。摘要来源为 operator 明确发布，完整字段、不可变 revision、head epoch、共享预算与历史闭包按[执行契约 §5.6](2026-10-20-workflow-execution-contract.md#56-显式版本摘要上下文2026-11-11)。外部有序 `summary_inputs` 保存完整原版本和摘要 hash；内层 prompt schema 3、外层 request envelope schema 1、external metadata schema/minimum reader 4、Workflow reader 11。无摘要请求保留旧 prompt 字节；初始化已有 metadata 4 不降级，旧 reader 拒绝新数据。

下列接口使用原 `/api/platform/v1` operator 鉴权、Workflow gate 与同源写边界。body 精确匹配字段，不接受 owner、creator、source_kind 或客户端时间。

| 方法与路径 | 字段与结果 |
| --- | --- |
| POST `/workflow-summaries` | workspace_id、summary_id、title、content、client_token、expected_epoch；返回完整规范版本和 sha256。首次 epoch=0，成功及原重试均返回 201。 |
| GET `/workflow-summaries` | workspace_id 必填，limit=1..100（默认 50），cursor 为上一页最后 summary_id；返回 head 的 summary_id/workspace_id/latest_revision/epoch/enabled 与 next_cursor，不含正文。 |
| GET `/workflow-summaries/version` | workspace_id、summary_id、revision 必填；返回该完整历史版本和 sha256，包括已停用的版本。 |
| POST `/workflow-summaries/enabled` | workspace_id、summary_id、expected_epoch、布尔 enabled；返回更新后的 head。 |

没有删除或隐式 latest API。首次发布在同一 SQLite 写事务原子创建 authority schema、head、version、幂等凭据与 reader fence，失败全部回滚。原 dispatch/resolved 固定精确引用，可信 template 决定选择；本轮未提供摘要 UI 或自动生成器。

外部输入采集区分 Memory/summary authority，按合同顺序保存完整值。原发送意图事务重新校验精确版本和当前 enabled/workspace；先于该事务提交的停用阻止新发送。已经存在的发送意图和未知任务继续遵守原 journal lookup-only 恢复，不假定停用能收回已发出的网络请求，不改写 agent_reported/unknown 或 held/isolated。历史/备份验证查原版本且不要求当前启用；新 head 不替换原请求。Muse cancel/export、外部能力要求、其他上下文和二进制/代码包输入仍未扩展。

实现与原始失败、混合输入、当前权威竞态、原 worker/adapter、回滚及 frozen 备份证据见[摘要计划](../plans/2026-11-11-versioned-summary-context.md)，本机证据不等于真实外部模型或完整 F5 验收。

## 8. 代码交付包与验证

源码交接包由固定 exporter 生成，记录 repository_id、base/head/tree 和包 hash。不导出整个 .git、远端凭据、未提交用户文件、宿主配置或密钥。只接收预先定义的归档成员；解包拒绝路径逃逸、链接、设备文件、超额展开和压缩炸弹。

评审节点以只读挂载读取固定 tree；测试节点在隔离工作区执行登记的测试配方。测试报告记录实际命令配方 revision、环境摘要、source revision、exit code 和 JUnit/结构化证据引用。

代码版本变化后重新计算 subject_digest。旧测试“通过”不会迁移到新源码；同一文件名也不能成为沿用旧验证的依据。

### 8.1 F3 已实现格式与限制

`source` 输出是 UTF-8 JSON artifact，固定字段 `schema_version=1`、`source_revision` 和可信执行器提供的 `executor_id`。`source_revision` 为 CodeRevisionRef；源码内容另以 `source_bundle` 输出指向同一引用中的未压缩 tar。包首成员为 `.fleet-source.json`，记录 repository/base/head/tree 及全部 tree entry 的 path/mode/oid/size/included。完整 Git 对象长度为 40 或 64 hex；导出确认 base/head 是 commit、head 对应指定 tree，解包重算原 tree 与每个包含的 blob。被排除 entry 仍参与 tree 校验，但内容不进入包。

此切片复用旧 ArtifactStore：包上限 10 MiB、最多 4096 个 tree entry、manifest 2 MiB、路径 512 UTF-8 字节/32 层；不是 F4 的流式 64 MiB 传输。只接受未压缩 tar 中登记的普通文件，拒绝额外/重复成员、链接、稀疏文件、设备、路径逃逸及超额展开。目标父目录由调用方私有管理；发布采用系统排他目录 rename，竞争者在检查后创建目标也不能被覆盖。macOS/Linux 有本机实现；Windows 行为仍需独立实测。

排除策略涵盖 `.git`、`.env` 及其变体、常见凭据目录、credential/secret 文件名变体和私钥/证书后缀；不读取工作区未提交文件。该策略不证明任意源码内容没有秘密；仓库授权和内容审查仍是独立边界。

评审 JSON 报告上限 64 KiB，实际 ReviewIdentity 来自可信 adapter，拒绝开发执行器自评或报告伪造身份。固定测试已接入原 Run worker/租约/取消与 unknown 恢复，配方记录 source/subject、recipe revision、实际退出/超时、执行器和 JUnit，以及明确 scope=adapter_host 的环境摘要；默认 SandboxBackend 缺 launcher 时关闭执行。CLI 源码采集与独立命令评审已按 §8.2/8.3 接线；本机受控链路经过实际 fixture 子进程、严格 loopback 模型 HTTP 对端、原协调器、真实 Cookie、文件下载和人工接受接口。DirectoryBackend、fixture launcher 和 loopback 模型对端不证明生产隔离、外部模型质量、商业 CLI 或远端节点；macOS 固定测试另有真实 sandbox-exec 文件/网络/凭据探针，Linux/Windows 与生产部署配置仍待验收。

### 8.2 F3 CLI 源码采集协议

仅管理员登记的 develop Run 可设置 `workflow_source={repository_id,revision:1}`；标记由可信 Run adapter 写入，operator/runner 正文不能设置。LegacyTaskBridge 在创建 Task 的同一事务保存 owner/run 来源，poll 通过该来源和当前 dispatch 查回采集要求；普通 Task 没有此字段。首版仅支持 coding-delivery@1 的固定无输入 develop 合同。

Runner 保存创建隔离工作树时的 base commit。CLI 退出后，在原心跳、取消与截止生命周期内，用私有临时 index 固定该任务工作树的全部非忽略文件（包含未提交输出），再用固定身份/UTC 时间创建无 hook、无签名的 commit 对象；不移动分支、不改用户主树或任务 index。归档继续使用 §8.1 exporter；凭据文件内容不进入归档。此 commit 是采集器固定的任务输出，不冒称 CLI 自己提交的版本。

新增 `POST /api/commands/{attempt_id}/source`：机器来自 runner 认证；`X-Task-Nonce`、`X-Content-SHA256` 与精确 Content-Length 是必需头；正文仅为原未压缩 tar，最多 10 MiB，按块接收与校验，不使用 base64 或 result.files。Hub 从任务来源查回 owner/workspace/run/当前 generation，不接受任意路径、scope 或 artifact_id。归档 manifest 提供 base/head/tree，Hub 重算 tree/blob 并核对登记 repository_id。它验证包一致性，不声明远程 Git 历史或模型正确性已独立证明。

上传前持久保存固定 artifact_id、归档 hash/size 与完整 attempt 身份；相同 attempt 同包查回同一预约及 source receipt，异包返回冲突。文件发布使用排他 rename；崩溃后核对同一 ID 的内容再续接，不覆盖异内容。source 输出仍由原交付服务同事务提交，最终短提交在原 Task 写事务的 fencing 内再次检查租约/取消，以及当前 Workflow、generation、工作区、frozen 与截止。两库不是一个分布式事务；source receipt 先于 Task result，崩溃或丢响应只查回 receipt，不重跑 CLI。

成功 result 必须关联当前 attempt 的已提交 source receipt；失败 result 可以没有源码。已提交 source receipt 的原样查回允许迟到取消，但不使被取消 Task 复活。未确认上传不得报告开发成功。心跳维持到源码、文本结果采集和回传完成；网络断开或失租后停止发布。固定源码包先保存在 runner 私有缓存，上传可在原有界生命周期内重试，不能在新 attempt 冒用旧包或自动重新执行 CLI。此入口不实现 F4 分片、任意文件上传或新执行引擎。

永久拒绝、原截止已过或缓存损坏的 pending 记录移入私有 `reconciliation/` 目录，保留原请求、缓存包及错误码，退出自动重放队列；临时网络故障只在重新确认原租约后继续。该记录不证明 Hub 已接受或执行已停止，必须人工核对；不自动删除源码缓存。

### 8.3 F3 只读命令评审适配

评审仍使用原 Run 队列；可信 target 固定 `workflow_review` 标记，包含 adapter id/revision、实际 ReviewIdentity 和配置摘要。配置摘要覆盖固定 argv、launcher 与超时。请求、源码和模型回复不能选择 adapter、命令、执行身份或 launcher。同一注册 revision 的配置变化会使旧 Run 失败关闭，不能静默切换评审者。

命令 adapter 只执行管理员登记的绝对路径程序及固定参数。其独立私有目录包含 `source/`、`control/request.json` 和 `scratch/`；环境仅提供 PATH、HOME、TMPDIR、`FLEET_REVIEW_SOURCE`、`FLEET_REVIEW_REQUEST`。request.json 固定 schema_version、source_revision、subject_digest 和预期 executor；stdout 必须是 §8.1 的完整 JSON 报告，最多 64 KiB，stderr 单独有界接收。拒绝非零退出、超时、截断、身份/主体错配；不从日志猜测 verdict。

launcher 为管理员登记的参数元组，可使用 `{source}`、`{control}`、`{scratch}` 路径替换。它必须把 source/control 以只读方式提供给评审进程、隔离宿主文件和凭据，并按登记策略控制网络；没有 launcher 时拒绝执行。非空 launcher 只是配置事实，不代表隔离已经验收。fixture launcher 仅证明进程/协议行为，真实文件、网络和凭据隔离必须另有验收证据。评审命令不由 Hub 自动取得模型凭据。

命令执行前后还对源码目录、文件集合、权限与内容进行有界 O(n) 核对；文件读取逐级固定目录句柄，拒绝链接、特殊文件及超额文件/字节。变更一律拒收报告；这层核对不能替代只读隔离，也不能证明执行期间从未发生临时写入。

worker 在启动前、运行中及提交时核对原 Run lease/attempt、完整 dispatch binding、当前 generation、已成功 develop 的固定 source subject、工作区授权、取消/frozen/原截止；进程组退出并回收后才确认取消。前驱源码与报告按当前作用域校验，不要求尚未存在的 review evidence。run_started 后崩溃继续 unknown，不能自动重跑评审命令。只允许在有界次数内重试同一报告的发布；已提交 receipt 优先查回，完成之后的取消不能撤回完成事实。负面 verdict 是校验失败，评审执行仍可 succeeded，并继续阻断测试。

### 8.4 独立远程固定测试（2026-11-02）

独立 `coding.test@1` capability 以 `tool.workflow.test` 执行管理员固定的
`coding-delivery-tests@1`，只允许精确 Linux route 与 `validation + remote_test`
注册目标。原接收回执 ready 后才 admission；请求绑定 source revision、subject、
generation、dispatch token 与 capability binding，不接受路径、shell、argv 或环境覆盖。
Node 从原签收 journal 的固定 descriptor 核验、流式解包和运行；64 KiB 结果封套
内带完整有界报告、JUnit、环境与实际固定 recipe argv，Hub 独立核验后在原交付事务
提交 evidence/产物/投影/receipt。Hub 与 Node 的 Python 绝对路径允许不同，固定
参数与 Node argv 摘要仍须完全匹配。

`manual_only` 与原 journal 保证不自动重放；未知保持原状态和 held/isolated。
测试断言失败是验证失败，完整报告的执行可 succeeded；缺失/超限/损坏报告不产生
passed evidence。reader 10 在创建远程 Run 的原事务钉定，备份恢复 frozen。
Node 显式 gate 默认 false，Hub 仍需 Workflow/transfer/capability/remote execution
及命令签名。launcher 存在不是隔离证据，Windows 此动作不启用。完整协议见
[远程固定测试契约](2026-11-02-remote-fixed-test-capability.md)，
[实施计划](../plans/2026-11-02-remote-fixed-test-capability.md)与
[实施进度](../../platform/workflow-platform/implementation-progress.md#2026-11-02-独立远程固定测试能力)
分别记录落盘与本机验证。真实 Linux Node/完整 WF-04/05 仍开放。

## 9. 模块接口与兼容

F4.1 已落盘：

- hub/domain/artifact_transfer.py：纯状态、manifest 和 chunk 校验。
- hub/infrastructure/artifact_transfer_repository.py：reservation/parts/commit 状态。
- hub/application/artifact_transfer_service.py：授权、配额、流式发布和恢复。
- hub/http/artifact_transfer_routes.py：Node-bound 原始 bytes 入口。

F4.2 本机切片已落盘：

- hub/domain/artifact_receive.py、artifact_export.py：纯签名请求与受限回执。
- tools/platform/artifact_transfer.py、artifact_journal.py：Node 接收与独立账本。
- tools/platform/artifact_export.py、artifact_export_journal.py：固定文件导出与独立恢复。
- hub/infrastructure/artifact_capacity.py：Hub 收发统一 admission。

F4.3 目录/报告/显式 admission、Node 本地复核与 Hub 固定绑定派发已落盘：

- hub/domain/capability.py、hub/application/capability_router.py：固定定义、报告和确定性选择。
- hub/infrastructure/capability_schema.py、capability_repository.py：版本目录、报告序号、原持久槽位与固定 binding。
- hub/infrastructure/capability_dispatch.py：从原 Run/token 查回、事务内签名准备与首次 lease 复核。

后续切片拟新增或接线：

- 现有 WorkflowCoordinator 的自动选点与输入 target 固定本机子集见 §6.5；Node 本地子集见 §6.3，Hub 固定绑定派发子集见 §6.4。完整 WF-05 与跨平台/外部验收仍开放。
- tools/platform/connectors/mcp.py、tools/platform/external_agents/base.py：外部边界。

相关旧模块通过小适配层调用，不复制身份/签名/配额判断。新 transfer 与 capability/MCP gate 默认关闭。旧节点无新能力时明确 unsupported，继续观测与旧任务。

V2 文件和 Workflow 引用启用前，先升级备份/恢复与引用扫描。新备份记录最低 reader；旧恢复器拒绝不认识的域，不静默忽略。回滚代码时保留新表和文件，必要时停留在最后一个兼容 reader，不能删库恢复。

验收依据见[工作流验收矩阵](../../testing/workflow-platform-acceptance.md)。MCP 设计参考[官方工具规范](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/server/tools.mdx)；实现前按选定服务核对其支持版本。
