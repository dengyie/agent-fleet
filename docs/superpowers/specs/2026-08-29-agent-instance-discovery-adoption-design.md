# Agent 实例发现与显式纳管(Discovery & Explicit Adoption)设计

> 状态:设计稿,待用户 review。
> 来源需求:宿主机只要安装探针即可看到所有正在运行的 AI agent 实例、看到具体对话信息、进行有效控制。
> 架构路线:**方案 A — 轻量发现 + 复用既有 bounded 数据面 + 显式纳管**(用户已选定)。
> 本设计是对 `architecture-v4-control-plane.md` 与 `2026-08-26-agent-session-supervision-design.md` 的**增量**,不推翻任何既有约束。

## 0. 目标与非目标

**目标**

1. 探针能枚举宿主机上所有正在运行的、属于已知家族(claude/codex/hermes/generic)的 agent 实例,并上报其**元数据**到 hub。
2. operator 在页面看到这些实例后,可对任一实例执行**显式纳管(adopt)**;纳管后该实例的对话内容以**脱敏 bounded 流**进入现有 `TranscriptRepository`。
3. 纳管后的实例进入 supervisor 可治理集合,可执行现有 `pause`/`resume`/`terminate` 控制动作。
4. 纳管可由 operator **撤销(revoke)**,撤销只断开观测与治理,不向进程发任何信号。

**非目标(明确不做)**

- 不做自动纳管:对没有 operator 显式确认的进程不下发任何控制。
- 不做 hub 主动连接机器:发现是探针 push 元数据,纳管是探针 pull 拉到命令后才动作。
- 不在发现阶段上传任何对话内容。
- 不让探针跨用户控制实例。
- 不复用 ingest token 做纳管操作(`/api/adoptions` 走 operator 认证域)。

## 1. 核心张力与设计立场

用户需求倾向「装上探针就能看到并控制一切」,而现有架构(v4 §7 + 08-26 spec §12.1)刻意堵死「对没有归属证明的进程注入控制」这条路——理由是对伪装成 codex 的进程注入控制、或把命令注入到别人的会话,是高危的。

本设计的调和方式:**发现 = 全量元数据可见**(满足「看到全部」),**对话与控制 = 显式纳管后开放**(满足「可控制」同时保留人工归属确认这道安全门)。纳管本身是受控动词,全程审计,可随时撤销。

## 2. 架构总览

继承不动:push-only、bounded 错误码、`process_group_id` 存在=managed、控制面 pull-mediated + Ed25519 签名。

新增一条通路,全程 agent 出站,零新协议:

```
[宿主机] 探针(cron/daemon,已有)
  ├─ collect_all(已有)        → observation 快照
  └─ discover_instances(新)   → ps 扫描 + 家族 cmdline 匹配
       → instances[] 元数据附进快照, push /api/ingest
[hub]  快照落 ObservationRepository;页面展示 instances[]
       operator 点「纳管」某实例
       → 记 adoption 审计 → enqueue supervisor 命令 adopt(pid, started_at, ...)
[宿主机] 探针下次 poll 拉到 adopt 命令
       → 校验 (pid, started_at) 仍一致 + exe_path 未变
       → 启 JsonlTailer 追 native transcript → 脱敏 → /api/session-events
       → 该实例进 supervisor 可治理集合(pause/resume/terminate 可下发)
[hub]  TranscriptRepository 收 bounded 流
       operator 点「撤销」→ enqueue 命令 detach → 探针停 tail + 退出治理
```

**关键不变量**

- 发现阶段 `instances[]` 只含元数据,不含对话内容 → 发现阶段零新隐私面。
- 纳管前:不可读内容、不可控制。
- 纳管后:默认 `best_effort` 脱敏流;`exact` 需 operator 显式升,走 AEAD 加密 raw + 审计。
- `adopt`/`detach`/控制/撤销全部 `append_audit`。
- hub 全程不主动连机器。

纳管本质是「把一个已存在的 (pid, pgid) 绑定到一张 session 行 + 让 supervisor 能治理它」,而非「hub 接管进程」。supervisor 对已存在 pgid 的 attach 能力是真正的新增点,其余全是接线现有库。

## 3. 组件与接口

加号 = 新文件,星号 = 改既有文件。每个文件单一职责。

### 3.1 探针侧(宿主机)

| 文件 | 职责 | 关键接口 |
|---|---|---|
| `+ tools/probe/discovery.py` | 扫描运行中的 agent 实例,纯本地、无副作用 | `discover_instances() -> list[Instance]` |
| `* connectors/generic.py` | 抽取现有 `ps` 枚举为可复用方法 | `enumerate_processes() -> Iterator[ProcRow]` |
| `* tools/probe_collectors.py` | `build_payload` 调用 `discover_instances()`,塞 `instances[]` | 快照新增 `instances` 段 |
| `* tools/agent-self-report.py` | 无逻辑改动,`instances[]` 随快照 POST | — |
| `* tools/supervisor/supervisor.py` | 新增 attach/detach 已存在进程的能力 | `attach_to_existing()`, `detach()` |
| `* tools/supervisor/control_client.py` | poll 循环识别新 `adopt`/`detach` 动作码 | 复用现有 pull→验签→执行→回执骨架 |

### 3.2 hub 侧

| 文件 | 职责 | 关键接口 |
|---|---|---|
| `+ hub/domain/adoption.py` | 纳管领域模型 + 状态机 `pending→adopted→revoked` | `Adoption`, `AdoptionStatus` |
| `+ hub/application/adoption_service.py` | 编排:列实例、纳管、撤销、校验前提 | `list_candidates`, `adopt`, `revoke` |
| `+ hub/infrastructure/adoption_repository.py` | 纳管记录持久化,独立 SQLite | `upsert/get/list/list_active` |
| `* hub/http/supervisor_routes.py` | poll 响应混入 `adopt`/`detach` 命令 | 新增动作码,复用签名 |
| `* hub/http/observe_routes.py` 或新蓝片段 | 纳管操作 API | `POST/DELETE/GET /api/adoptions` |
| `* hub/http/session_routes.py` | 查询纳管实例 transcript 时按 adoption 状态放行 exact | 复用现有查询 |
| `* report_schema.py` | 快照 schema 加 `instances[]` 白名单 + 家族校验 | `validate_instances()` |

### 3.3 接口契约(下游任务依赖这些名字与签名)

```python
# discovery
@dataclass(frozen=True)
class Instance:
    pid: int; pgid: int; exe_path: str; cmdline: str
    agent_family: str           # "codex" | "claude_code" | "hermes" | "generic"
    native_file_path: str | None
    started_at: str             # ISO8601, 纳管时做 pid 复用防护
    attachable: bool            # 探针同用户且有 native 文件读权限

# supervisor(探针侧)
class Supervisor:
    def attach_to_existing(self, pid:int, started_at:str, exe_path:str,
                           native_file_path:str|None, agent_family:str) -> str:
        """返回 bounded 码: adopted | pid_reused | exe_changed |
        no_permission | unsupported_family | native_file_unreadable"""
    def detach(self, session_id:str) -> None: ...

# adoption_service(hub 侧)
class AdoptionService:
    def list_candidates(self, machine_id:str) -> list[Candidate]: ...
    def adopt(self, machine_id:str, pid:int, started_at:str, actor:str) -> Adoption: ...
    def revoke(self, session_id:str, actor:str) -> None: ...
```

### 3.4 关键边界纪律

- `AdoptionRepository` 独立 DB,与 session/transcript/observation/task 分库,任一失效不影响其余。
- `report_schema.validate_instances` 用与 `validate_event` 同款的字段白名单 + 家族枚举,cmdline 走截断 + 脱敏再入库与展示。
- 新动作码 `adopt`/`detach` 加入现有 bounded 错误码固定集,前端只比对这些码。

## 4. 数据流(五条时序)

每条标注「谁出站」「bounded 码在哪」「失败如何收敛」。所有 hub→机器方向都是 agent pull。

### 4.1 发现实例(常态,每轮探测)

探针触发 → `discover_instances()`(本地 ps + 家族 cmdline 匹配)→ `instances[]` 进快照(只元数据)→ `POST /api/ingest` → `ObserveService.ingest` diff + `save_snapshot` → `GET /api/status` 展示。

收敛:发现失败(ps 无权限/家族未知)→ 该实例不入快照,不阻断其余;ps 失败 → `instances:[]` + `discovery_error` 码。

### 4.2 提交纳管(operator 显式)

operator 选实例 → `POST /api/adoptions {machine_id, pid, started_at}`(operator 认证域)→ `AdoptionService.adopt`:① 校验候选仍在最近快照 `instances[]`(否则 `stale_candidate`)② 校验 `attachable` 与家族 ③ 生成纳管专属前缀 session_id(走 `is_valid_session_id` 边界)④ `AdoptionRepository.upsert(pending)` ⑤ `append_audit` ⑥ `SupervisorService.enqueue(adopt)` → 返回 `{adoption_id, session_id, status=pending}`。

### 4.3 探针执行纳管 + 开始追读

探针 `control_client` 下次 `/api/supervisor/poll` → 收签名 `adopt` 命令 → Ed25519 验签 + nonce/scope → `supervisor.attach_to_existing`:① 再取一次 `started_at`/`exe_path` ② 校验 `started_at` 一致(否则 `pid_reused`)③ 校验 `exe_path` 未变(否则 `exe_changed`)④ 校验同用户与读权限(否则 `no_permission`)⑤ 注册 (pid,pgid) 进治理集合 ⑥ 启 `JsonlTailer` 追读 → 回执 `/api/supervisor/receipts`。

hub 收 `adopted` → status=adopted → 探针持续 tail → 脱敏 → `/api/session-events` → `TranscriptRepository`(默认 best_effort)。收 `pid_reused`/`exe_changed`/`no_permission` → status=revoked + 审计 + 告警。

### 4.4 升级 exact(可选,operator 显式)

operator 点「升级精确捕获」→ `POST /api/adoptions/<session_id>/capture-exact` → 该 session capture_quality=exact + 审计 → 后续 exact 事件落 AEAD 加密 raw + raw_read 审计(完全复用现有 TranscriptRepository exact 通道)。

### 4.5 撤销纳管(operator 或自动)

触发:operator 撤销 / 探针报漂移 / 会话结束 → `AdoptionService.revoke`:① status=revoked ② 审计 ③ `enqueue(detach)` → 探针 pull 拉到 → `supervisor.detach`:① 停 JsonlTailer、关 checkpoint ② 移出治理集合(**不发信号**,进程继续按原方式跑)→ 回执 detached。

**关键语义**:`detach` 绝不发信号。撤销纳管只断开观测 + 治理。要终止须另发 `terminate`(且只在 adopted 接受)。

### 4.6 控制已纳管实例

operator 对 adopted 实例点 pause → 复用现有 supervisor enqueue("pause") → 探针 pull → 校验 session 仍 adopted 且 (pid, started_at) 一致 → SIGSTOP 该 pgid(现有 `POSIXGroupOps`)→ 回执。唯一前置变化:执行控制前加「adopted 且归属未漂移」校验。

## 5. 错误处理与边界

### 5.1 新增 supervisor 动作码(进 `hub/domain/control.py` `CONTROL_ACTIONS`)

| 动作 | 语义 | 仅 adopted 后 |
|---|---|---|
| `adopt` | 探针侧执行纳管校验 + 注册 + 启 tail | —(纳管首命令) |
| `detach` | 停 tail + 移出治理集合,不发信号 | 是 |

`pause`/`resume`/`terminate`/`quarantine`/`cancel_attempt` 沿用不变。

### 5.2 新增 bounded 错误码(扩展现有固定集,绝不带自然语言/路径/异常)

**纳管提交流 `AdoptionService.adopt`**:`stale_candidate`(候选不在最近快照)、`not_attachable`(attachable==false)、`unsupported_family`、`invalid_candidate`(字段缺失/形状非法)、`already_adopted`(幂等返回既有 session_id)。

**探针执行流 `attach_to_existing`**:`adopted`、`pid_reused`(started_at 不符 → 撤销+审计+告警)、`exe_changed`(exe 漂移 → 同上)、`no_permission`(跨用户/无读权限 → 撤销+审计)、`native_file_unreadable`(adopted 但 best_effort 不落 raw + 记 `capture_gap`)、`unsupported_family`(探针侧适配器缺失 → 撤销+审计)。

**控制已纳管实例(前置校验,挂在现有动作前)**:`unknown_session`(沿用)、`adoption_revoked`(已撤销)、`pid_reused`(执行前再验不一致 → 撤销+审计,不发信号)。

### 5.3 与现有 fail-closed 衔接

1. **签名门不变**:`adopt`/`detach` 走与 `pause` 相同的 Ed25519 + nonce/scope 校验。无 key → 拒签。
2. **纳管库隔离**:`AdoptionRepository` 坏 → 纳管 503(有界码),不波及 observation/task/session/transcript。复用 SessionService 的 `sqlite3.Error → SessionServiceError` 范式。
3. **不引入新攻击面**:`instances[]` 仅元数据 + cmdline 脱敏;纳管前不可读内容;纳管是 operator 显式 + 审计;`terminate` 只在 adopted 接受。
4. **pid 复用三道闸**:纳管时校验、每次控制前再验、探针主动上报漂移。任一触发 → 不发信号,fail-closed 撤销。宁可误撤不可误杀。
5. **进程归属证明**:探针回传 `exe_path`(规范化绝对路径)+ `started_at`;operator 对照 cmdline/exe_path 人眼确认;supervisor 执行前再验 exe 未漂移。
6. **撤销≠终止**:`detach` 不发信号;`terminate` 才发 SIGTERM。两动词分离。

### 5.4 明确排除的攻击面

不做 hub 主动连机器;不做自动纳管;不在发现阶段上传对话内容;不让探针跨用户控制;不复用 ingest token 做纳管操作。

## 6. 测试与验收策略

沿用现有范式:领域/repository 层纯单元、服务编排层依赖注入假 repo、HTTP 层用 `flask.test_client`、探针 supervisor 用临时目录 + 子进程证明真信号落地。每个测试断言 bounded 码或可观测副作用,不把自然语言当 pass。

### 6.1 新增覆盖矩阵

**`tests/test_discovery.py`**:`test_discover_matches_known_families_by_cmdline`、`test_discover_ignores_unmatched_processes`、`test_discovery_payload_contains_metadata_only_no_content`(隐私红线)、`test_report_schema_rejects_oversized_or_secret_shaped_cmdline`、`test_ps_failure_yields_empty_instances_not_crash`。

**`tests/test_adoption_service.py`**:`test_adopt_rejects_stale_candidate`、`test_adopt_rejects_not_attachable`、`test_adopt_enqueues_signed_command_and_writes_audit`、`test_adopt_is_idempotent_returns_existing`、`test_revoke_enqueues_detach_and_audits_does_not_signal`(断言 payload 不含 signal 字段)、`test_adoption_repo_isolated_from_session_and_transcript_db`。

**`tests/test_supervisor_attach.py`**(关键安全闸):`test_attach_to_existing_succeeds_when_pid_started_at_exe_match`、`test_attach_rejects_pid_reused_fail_closed`、`test_attach_rejects_exe_changed`、`test_attach_rejects_cross_user_no_permission`、`test_detach_stops_tail_and_removes_from_governance_without_signal`(断言 `proc.poll() is None`)、`test_pause_on_attached_session_signals_correct_pgid`。

**`tests/test_adoption_http.py`**:`test_post_adoption_requires_operator_auth_not_ingest_token`、`test_delete_adoption_revokes_and_enqueues_detach`、`test_session_events_from_adopted_instance_accepted_best_effort`、`test_exact_capture_upgrade_audits_and_encrypts_raw`。

**`tests/test_adoption_control_guard.py`**:`test_pause_rejected_when_adoption_revoked`、`test_terminate_rechecks_pid_before_signaling`(pid 复用后发 terminate → `pid_reused`,无信号)。

### 6.2 复用现有测试(不回归)

`test_supervisor_process.py`(真信号语义)、`test_session_repository.py`(session_id 边界)、`test_transcript_repository.py`(脱敏/raw/审计)、`test_session_ingest_api.py`(有界码 + ack 协议)、`test_session_bridge.py`(tailer/脱敏前置)——全部必须保持绿色。

### 6.3 验收标准(端到端)

1. **可见性**:装探针的宿主,页面看到全部家族匹配进程(元数据),纳管前不可见对话。
2. **纳管**:纳管一个真在跑的 codex → 对话以脱敏 best_effort 流出现;审计有 `adopt_session`。
3. **控制**:纳管后 pause → 进程真停(SIGSTOP 可观测);detach → 观测断开但进程继续;terminate → SIGTERM。
4. **安全闭环**:杀掉目标让 pid 复用,再发控制 → `pid_reused`,无信号误发,纳管自动撤销并告警。
5. **隔离**:Adoption DB 损坏不影响 observation/task/session/transcript 任一页面可用。

### 6.4 测试纪律

- 断言只比 bounded 码或可观测副作用,不比响应文本。
- supervisor 真信号测试沿用 `test_supervisor_process.py` 的子进程组构造,不引入新 fixture。
- 新文件不改现有测试;若必须改,只放宽输入构造、不收紧既有断言。
- 全套件保持绿;新增约 20+ 测试,不引发回归。

## 7. 与现有设计的衔接

- **v4 §1.1 观测链路**:本设计在 observation 快照里加 `instances[]` 段,属同一条 push-only 链路的增量,不改其流向。
- **v4 §6.5 managed/unmanaged 边界**:纳管产生的 session 行带 `process_group_id`(来自真实 pgid)→ 自动满足 managed 定义;纳管前实例不进 session 表,符合 unmanaged=best_effort 语义。
- **v4 §7 不做的事**:本设计不违反任何一条——不 SSH、不浏览器直连、不自动上传所有 session、不嵌 token、不任意 shell。对话内容仅在 operator 显式纳管后以 bounded 脱敏流上传。
- **08-26 spec §12.1 「不自动接管无归属证明的进程」**:本设计用「operator 显式纳管 + exe_path/started_at 双因子 + 审计」补上了 spec 留待「未来增加显式、可验证、用户确认的 adoption 流程」的那个口子——正是 spec 指明的演进方向。

## 8. 分阶段落地建议(供后续 writing-plans 参考)

1. **发现面**:discovery + report_schema.validate_instances + 快照展示(纯增量,零控制,最低风险)。
2. **纳管编排**:AdoptionService + AdoptionRepository + operator API + adopt/detach 动作码(纯编排,探针侧先只回执不真 attach)。
3. **探针 attach + 追读**:supervisor.attach_to_existing + JsonlTailer 接线 + pid 复用三闸。
4. **控制前置校验**:adopted/漂移校验挂到现有 pause/resume/terminate 前。
5. **exact 升级 + 撤销闭环**:capture-exact API + detach 不发信号语义 + 端到端验收。

每阶段独立可测、可部署、可回滚;每阶段产出不破坏现有 1086 测试。
