# Agent 实例发现与显式纳管 Implementation Plan

> **Status (2026-09-08): historical / implemented.** Open checkboxes below are the original plan and were not kept in sync after merge. Do not re-implement from unchecked boxes. Remaining work is recorded in `docs/HANDOFF.md` §六 as `done` or `deferred-with-condition`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 宿主机安装探针后可看到全部运行中 AI agent 实例的脱敏元数据；operator 显式纳管后，对话以 bounded、默认 `best_effort` 的脱敏流进入现有 `TranscriptRepository`，并可执行 pause/resume/terminate；撤销只断开观测与治理，不向进程发信号。

**Architecture:** 采用方案 A：轻量发现 + 复用既有 bounded 数据面 + 显式纳管。探针本地枚举进程并经既有 `/api/ingest` push 仅元数据；operator 通过 hub 编排显式纳管，hub 只经现有 agent-pull supervisor 通道下发签名 `adopt`/`detach`。探针 attach 时重新校验 `(pid, started_at, exe_path)`、权限和家族，注册真实 pgid 并接入 `SessionBridge`；纳管后的 transcript、SSE、审计和控制复用现有边界。

**Tech Stack:** Python 3、Flask、SQLite/WAL、Ed25519、pytest、原生 ES Modules。

**Spec:** `docs/superpowers/specs/2026-08-29-agent-instance-discovery-adoption-design.md`

## Global Constraints

- 不自动纳管：没有 operator 显式确认的进程不下发任何控制。
- hub 不主动连接机器：发现由探针 push；纳管和控制由探针 pull；所有 hub -> machine 方向都经过 supervisor poll。
- 发现阶段零对话内容：`instances[]` 只含 `pid`、`pgid`、`exe_path`、脱敏截断 `cmdline`、`agent_family`、`native_file_path`、`started_at`、`attachable`。
- 所有新失败只能使用固定 bounded code；不得暴露自然语言、路径、异常文本、原始输入或 secret；前端只按固定 code 分支。
- `AdoptionRepository` 使用独立 SQLite 文件，与 observation、task/attempt/lease、session、transcript 分库；其故障只能影响纳管请求。
- pid reuse 三道闸：adopt 时校验、每次控制前重验、探针发现漂移时上报；失败时 fail-closed、撤销且不发信号。
- `detach` 绝不发信号；`terminate` 才允许 SIGTERM，且只对仍 adopted 且身份未漂移的 session 生效。
- `/api/adoptions` 和 operator 控制路由使用 `@require_operator`；不得复用 ingest token；poll/receipt 继续使用 `@require_supervisor`。
- session id 使用 `adopt_` 前缀并通过 `session_schema.is_valid_session_id`；原始 pid 只能在探针私有 handle，公开对象只暴露 opaque `process_group_id`。
- 纳管后默认 `best_effort` 脱敏；exact 必须 operator 显式升级，raw 复用已有 AEAD、quota、retention 和 `raw_read` 审计。
- 不改 observation、task/attempt/lease、runner、认证、SSE、`/api/*`、`/api/v1`、前后端独立发布和回滚契约；每阶段保持现有 1086 个测试不回归。
- 单个枚举/记录/事件失败不得丢弃其它实例或毒化后续事件；边界处归类错误。
- 每项任务先写失败测试，验证失败，做最小实现，验证通过后独立提交。

---

## File Structure

新增文件：

- `tools/probe/discovery.py`：不可变 `Instance` 和纯本地 `discover_instances()`。
- `hub/domain/adoption.py`：`Adoption`、`AdoptionStatus`、状态转换。
- `hub/application/adoption_service.py`：候选校验、adopt/revoke/exact 编排。
- `hub/infrastructure/adoption_repository.py`：独立 SQLite 纳管存储和 bounded error。
- `hub/http/adoption_routes.py`：operator 候选、adopt、revoke、exact、source-control transport。
- `tests/test_discovery.py`、`tests/test_instance_schema.py`：发现和 schema 测试。
- `tests/test_adoption_repository.py`、`tests/test_adoption_service.py`：领域/存储/编排测试。
- `tests/test_supervisor_attach.py`、`tests/test_attach_bridge.py`：attach、身份闸和追读测试。
- `tests/test_adoption_http.py`、`tests/test_adoption_control_http.py`：HTTP 认证/错误合同。
- `tests/test_adoption_control_guard.py`：控制前置闸测试。
- `tests/test_exact_capture_upgrade.py`、`tests/test_adoption_lifecycle.py`、`tests/test_adoption_e2e.py`：最后阶段测试。
- `tests/test_frontend_adoption_contracts.py`：Node fixture 前端合同。

修改文件：

- `connectors/generic.py`：抽取 `enumerate_processes() -> Iterator[ProcRow]`。
- `report_schema.py`：增加 `sanitize_instances()`。
- `tools/probe_collectors.py`：payload 增加 `instances`。
- `hub/application/observe_service.py`、`hub/domain/machine.py`：接收并公开 sanitized instances。
- `hub/config.py`、`hub/bootstrap.py`：独立 adoption path/flag、repository/service/blueprint wiring。
- `hub/domain/control.py`：固定动作增加 `adopt`、`detach`。
- `tools/supervisor/supervisor.py`、`tools/supervisor/control_client.py`：已存在进程 attach/detach 及命令执行。
- `tools/session/bridge.py`：managed native tail 生命周期。
- `hub/application/control_router.py`、`hub/application/supervisor_service.py`、`hub/http/supervisor_routes.py`：receipt、审计和既有 pull 通道接线。
- `hub/http/session_routes.py`：按 adoption 状态复用 transcript 查询。
- `frontend/views/machine.js`：实例列表和 operator 状态/动作。
- `docs/HANDOFF.md`、`README.md`：flag、发布顺序、回滚和验收说明。

Phase 1 只读可单独部署；Phase 2 只产生 pending 命令；Phase 3 才启用 probe attach；Phase 4 开放 adopted source-control；Phase 5 开放 exact 与完整 revoke/drift。各阶段通过关闭 `adoption_repositories_enabled`/probe adoption dispatch 或回滚该阶段提交恢复旧流量路径。

---

## Phase 1: 发现面

### Task 1: 抽取进程枚举并实现纯本地发现

**独立交付:** 探针在不产生网络或控制副作用的情况下枚举匹配的 agent，并返回稳定排序的实例元数据。

**Files:**
- Create: `tools/probe/discovery.py`
- Create: `tests/test_discovery.py`
- Modify: `connectors/generic.py`
- Modify: `agent_profiles.py`（只复用既有 `PROFILES`，不复制 family 常量）

**Interfaces:**
- Consumes: 现有 `ps -eo user,pid,time,rss,comm,args` parser 和 profile registry。
- Produces:

```python
@dataclass(frozen=True)
class Instance:
    pid: int
    pgid: int
    exe_path: str
    cmdline: str
    agent_family: str  # codex | claude_code | hermes | generic
    native_file_path: str | None
    started_at: str     # ISO8601
    attachable: bool

def discover_instances() -> list[Instance]: ...
# connectors.generic
 def enumerate_processes(self) -> Iterator[ProcRow]: ...
```

`discover_instances` 必须按 `(pid, started_at)` 排序；坏行单独跳过；`ps` 整体失败返回空列表，调用层使用固定 `discovery_error`。

- [ ] **Step 1: Write the failing test**

```python
def test_discover_instances_returns_identity(monkeypatch):
    monkeypatch.setattr(discovery, "enumerate_process_rows", lambda: [
        discovery.ProcRow(
            user="fleet", pid=321, pgid=320,
            exe_path="/usr/local/bin/codex", cmdline="codex --resume abc",
            started_at="2026-08-30T00:00:01Z",
        )
    ])
    monkeypatch.setattr(discovery, "native_path_for", lambda row: None)
    monkeypatch.setattr(discovery, "can_attach", lambda row, native: True)
    assert discovery.discover_instances() == [discovery.Instance(
        321, 320, "/usr/local/bin/codex", "codex --resume abc", "codex",
        None, "2026-08-30T00:00:01Z", True,
    )]

def test_bad_row_does_not_poison_other_rows(monkeypatch):
    monkeypatch.setattr(discovery, "enumerate_process_rows", lambda: [
        object(), discovery.ProcRow(
            user="fleet", pid=9, pgid=9, exe_path="/bin/hermes",
            cmdline="hermes", started_at="2026-08-30T00:00:02Z")])
    assert [item.pid for item in discovery.discover_instances()] == [9]
```

- [ ] **Step 2: Verify failure**

Run: `pytest -q tests/test_discovery.py`
Expected: FAIL because the module, row adapter, and function do not exist.

- [ ] **Step 3: Implement minimally**

Extract the existing generic `ps` parsing into `enumerate_processes`; let discovery adapt rows, classify only against `OBSERVABLE_AGENT_TYPES`, obtain pgid/start time/native path through existing platform helpers, and calculate `attachable` from same-user and native readability. Truncate only at the schema boundary; catch malformed/per-row permission failures separately from command failure and never return exception text.

- [ ] **Step 4: Verify pass**

Run: `pytest -q tests/test_discovery.py tests/test_generic_connector.py`
Expected: PASS with existing connector output unchanged.

- [ ] **Step 5: Commit**

```bash
git add tools/probe/discovery.py connectors/generic.py agent_profiles.py tests/test_discovery.py
git commit -m "feat: discover running agent instances"
```

**Rollback:** revert this commit; no hub or wire contract has changed.

### Task 2: Sanitize instances and publish them in probe snapshots

**独立交付:** self-report payload 增加安全的 `instances`，旧消费者忽略该 additive 字段即可继续工作。

**Files:** `report_schema.py`; `tools/probe_collectors.py`; `tools/agent-self-report.py`（只验证既有 POST 透传）； create `tests/test_instance_schema.py`; modify `tests/test_probe_collectors.py`。

**Interfaces:**
- Consumes: Task 1 `Instance`/`discover_instances`，现有 `_safe_value` 和 `sanitize_*`。
- Produces:

```python
def sanitize_instances(value: object) -> list[dict]: ...
# build_payload(...) includes "instances": list[dict]
```

每项只含 `pid, pgid, exe_path, cmdline, agent_family, native_file_path, started_at, attachable`；family 仅四值；cmdline/path 做 bounded truncation；unknown keys 丢弃。

- [ ] **Step 1: Write the failing test**

```python
def test_sanitize_instances_allowlists_and_bounds():
    result = sanitize_instances([{
        "pid": 10, "pgid": 8, "exe_path": "/bin/codex",
        "cmdline": "x" * 500, "agent_family": "codex",
        "native_file_path": "/tmp/native.jsonl",
        "started_at": "2026-08-30T00:00:00Z", "attachable": True,
        "secret": "token=drop",
    }])
    assert set(result[0]) == {
        "pid", "pgid", "exe_path", "cmdline", "agent_family",
        "native_file_path", "started_at", "attachable",
    }
    assert len(result[0]["cmdline"]) <= 200
    assert "secret" not in result[0]

def test_build_payload_has_instances(monkeypatch):
    monkeypatch.setattr(probe_collectors, "discover_instances", lambda: [])
    assert probe_collectors.build_payload("m1", ("codex",))["instances"] == []
```

- [ ] **Step 2: Verify failure**

Run: `pytest -q tests/test_instance_schema.py tests/test_probe_collectors.py`
Expected: FAIL because `sanitize_instances` and the payload field are absent.

- [ ] **Step 3: Implement minimally**

Implement the allowlist with existing `_safe_value` conventions, reject malformed entries individually, normalize integer/boolean fields, and truncate bounded strings. Add `"instances": sanitize_instances(discover_instances())` to `build_payload`; keep self-report URL, auth, and POST unchanged.

- [ ] **Step 4: Verify pass**

Run: `pytest -q tests/test_instance_schema.py tests/test_probe_collectors.py tests/test_report_schema.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add report_schema.py tools/probe_collectors.py tools/agent-self-report.py tests/test_instance_schema.py tests/test_probe_collectors.py
git commit -m "feat: publish sanitized instance metadata"
```

**Rollback:** revert this commit; old ingest payloads remain valid.

### Task 3: Ingest and render instances read-only

**独立交付:** hub 保存/返回 sanitized `instances`，machine 页面展示 metadata；不执行 adopt。

**Files:** `hub/application/observe_service.py`; `hub/domain/machine.py`; `frontend/views/machine.js`; create `tests/test_instance_ingest.py`, modify `tests/test_machine_domain.py`, create/modify `tests/test_frontend_adoption_contracts.py`。

**Interfaces:**
- Consumes: Task 2 payload，现有 `public_machine_detail`、`machineSnapshot`。
- Produces:

```python
# snapshot current adds, without removing old fields
{"instances": list[dict], "agents": dict, "system": dict, ...}
```

```javascript
export function renderInstances(instances, onAdopt) { ... }
```

- [ ] **Step 1: Write the failing test**

```python
def test_ingest_keeps_only_sanitized_instance_fields(client):
    response = client.post("/api/ingest", json=valid_payload(instances=[{
        "pid": 7, "pgid": 6, "exe_path": "/bin/codex", "cmdline": "codex",
        "agent_family": "codex", "native_file_path": None,
        "started_at": "2026-08-30T00:00:00Z", "attachable": True,
        "conversation": "drop",
    }]), headers=ingest_header())
    assert response.status_code == 200
    row = read_current("m1")["instances"][0]
    assert row["pid"] == 7
    assert "conversation" not in row

def test_machine_detail_exposes_metadata_only():
    detail = public_machine_detail(snapshot_with_instances(), [])
    assert detail["current"]["instances"][0]["agent_family"] == "codex"
    assert "conversation" not in repr(detail)
```

- [ ] **Step 2: Verify failure**

Run: `pytest -q tests/test_instance_ingest.py tests/test_machine_domain.py`
Expected: FAIL because ingest/detail do not include sanitized instances.

- [ ] **Step 3: Implement minimally**

In `ObserveService.ingest`, call `sanitize_instances(payload.get("instances"))` beside agents/system. Add only the sanitized list to `public_machine_detail`. Render with DOM `createElement`/`textContent`/`setAttribute`; display pid as metadata only and do not expose native content or a public process handle.

- [ ] **Step 4: Verify pass**

Run: `pytest -q tests/test_instance_ingest.py tests/test_machine_domain.py tests/test_frontend_contracts.py tests/test_frontend_adoption_contracts.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hub/application/observe_service.py hub/domain/machine.py frontend/views/machine.js tests/test_instance_ingest.py tests/test_machine_domain.py tests/test_frontend_adoption_contracts.py
git commit -m "feat: display discovered agent instances"
```

**Rollback:** revert; `instances` is additive and old snapshots remain readable.

**Phase 1 gate:** `pytest -q`; metadata ingest creates no session, transcript, control command, or signal.

---

## Phase 2: 纳管编排

### Task 4: 建立独立 Adoption domain 和 SQLite repository

**独立交付:** 纳管记录有独立 schema、状态和错误边界，不下发命令。

**Files:** create `hub/domain/adoption.py`, `hub/infrastructure/adoption_repository.py`, `tests/test_adoption_repository.py`; modify `hub/config.py`。

**Interfaces:**

```python
class AdoptionStatus(str, Enum):
    PENDING = "pending"
    ADOPTED = "adopted"
    REVOKED = "revoked"

@dataclass(frozen=True)
class Adoption:
    adoption_id: str
    machine_id: str
    session_id: str
    pid: int
    pgid: int | None
    started_at: str
    exe_path: str
    agent_family: str
    native_file_path: str | None
    status: str
    capture_quality: str
    actor: str
    created_at: str
    updated_at: str

class AdoptionRepository:
    def __init__(self, db_path: Path): ...
    def init(self) -> None: ...
    def upsert(self, adoption: Adoption | dict) -> Adoption: ...
    def get(self, session_id: str) -> Adoption | None: ...
    def list(self, machine_id: str) -> list[Adoption]: ...
    def list_active(self, machine_id: str) -> list[Adoption]: ...
    def update_status(self, session_id: str, status: str) -> Adoption: ...
    def update_capture_quality(self, session_id: str, quality: str) -> Adoption: ...
```

`AdoptionRepositoryError.code` 只允许 bounded code；session id 走 `is_valid_session_id`；只允许 `pending -> adopted -> revoked`，revoked 行保留。

- [ ] **Step 1: Write the failing test**

```python
def test_adoption_store_is_separate(tmp_path):
    repo = AdoptionRepository(tmp_path / "adoptions.db")
    repo.init()
    assert (tmp_path / "adoptions.db").exists()
    assert repo.path != tmp_path / "state" / "events.jsonl"

def test_status_round_trip_and_active_filter(tmp_path):
    repo = ready_repo(tmp_path)
    row = repo.upsert(adoption_dict(status="pending"))
    assert repo.get(row.session_id).status == "pending"
    repo.update_status(row.session_id, "adopted")
    assert len(repo.list_active("m1")) == 1
    repo.update_status(row.session_id, "revoked")
    assert repo.list_active("m1") == []

def test_path_shaped_id_is_bounded_error(tmp_path):
    repo = ready_repo(tmp_path)
    with pytest.raises(AdoptionRepositoryError) as exc:
        repo.upsert(adoption_dict(session_id="../../etc/passwd"))
    assert exc.value.code == "invalid_adoption"
    assert str(exc.value) == "invalid_adoption"
```

- [ ] **Step 2: Verify failure**

Run: `pytest -q tests/test_adoption_repository.py`
Expected: FAIL because domain/schema/repository do not exist.

- [ ] **Step 3: Implement minimally**

Create a separate WAL SQLite file with schema-version table and `adoptions` table; validate all bounded fields before parameterized SQL; use atomic status updates and idempotent upsert; translate every sqlite error to a code-only repository error.

- [ ] **Step 4: Verify pass**

Run: `pytest -q tests/test_adoption_repository.py tests/test_session_repository.py`
Expected: PASS, and adoption DB failure does not block session repository.

- [ ] **Step 5: Commit**

```bash
git add hub/domain/adoption.py hub/infrastructure/adoption_repository.py hub/config.py tests/test_adoption_repository.py
git commit -m "feat: add isolated adoption repository"
```

**Rollback:** revert and keep adoption flag disabled; existing stores are untouched.

### Task 5: Implement explicit AdoptionService

**独立交付:** 从最近快照选候选，显式创建 pending adoption，审计并入队签名 `adopt`；尚未要求 probe attach。

**Files:** create `hub/application/adoption_service.py`, `tests/test_adoption_service.py`; modify `hub/application/supervisor_service.py` only as needed to reuse existing enqueue; reuse `TranscriptRepository.append_audit`。

**Interfaces:**

```python
@dataclass(frozen=True)
class Candidate:
    machine_id: str
    pid: int
    pgid: int
    exe_path: str
    cmdline: str
    agent_family: str
    native_file_path: str | None
    started_at: str
    attachable: bool

class AdoptionService:
    def list_candidates(self, machine_id: str) -> list[Candidate]: ...
    def adopt(self, machine_id: str, pid: int, started_at: str,
              actor: str) -> Adoption: ...
    def revoke(self, session_id: str, actor: str) -> None: ...
```

- [ ] **Step 1: Write the failing test**

```python
def test_adopt_rejects_stale_candidate(service):
    with pytest.raises(AdoptionServiceError) as exc:
        service.adopt("m1", 404, "2026-08-30T00:00:00Z", "op")
    assert exc.value.code == "stale_candidate"

def test_adopt_enqueues_and_audits(service, candidate):
    row = service.adopt("m1", candidate.pid, candidate.started_at, "op-1")
    assert row.status == "pending"
    assert service.supervisor.commands[-1]["action"] == "adopt"
    assert row.session_id.startswith("adopt_")
    assert any(x["action"] == "adopt_session"
               for x in service.transcripts.read_audit())

def test_adopt_is_idempotent(service, candidate):
    first = service.adopt("m1", candidate.pid, candidate.started_at, "op-1")
    count = len(service.supervisor.commands)
    second = service.adopt("m1", candidate.pid, candidate.started_at, "op-2")
    assert second.session_id == first.session_id
    assert len(service.supervisor.commands) == count
```

- [ ] **Step 2: Verify failure**

Run: `pytest -q tests/test_adoption_service.py`
Expected: FAIL because service and bounded service error do not exist.

- [ ] **Step 3: Implement minimally**

Read only latest sanitized snapshot; match both pid and exact started_at; reject missing/stale/not-attachable/unsupported candidates with fixed codes; generate validated `adopt_<opaque>` id; persist pending; append code-only `adopt_session`; call existing `SupervisorService.enqueue(machine, session_id, None, "adopt", "operator_adopt", nonce=...)`. Active/pending duplicate returns existing row without a second command. `revoke` marks revoked, audits, and enqueues `detach` exactly once with no signal field.

- [ ] **Step 4: Verify pass**

Run: `pytest -q tests/test_adoption_service.py tests/test_supervisor_service.py tests/test_transcript_repository.py`
Expected: PASS and no exception/path/content leak.

- [ ] **Step 5: Commit**

```bash
git add hub/application/adoption_service.py tests/test_adoption_service.py hub/application/supervisor_service.py
git commit -m "feat: orchestrate explicit agent adoption"
```

**Rollback:** revert; no existing operator or probe route changes.

### Task 6: Expose operator API and pending-only action surface

**独立交付:** operator 可 list/adopt/revoke；新动作进入现有签名 pull 链路。Task 7 前 probe 对 adopt/detach 只返回 bounded unsupported receipt，不读文件、不发信号。

**Files:** create `hub/http/adoption_routes.py`, `tests/test_adoption_http.py`; modify `hub/bootstrap.py`, `hub/config.py`, `hub/domain/control.py`, `tools/supervisor/control_client.py`, `hub/http/supervisor_routes.py`, `tests/test_control_client.py`, `tests/test_supervisor_service.py`。

**Interfaces:**

```text
GET    /api/adoptions?machine_id=<machine>
POST   /api/adoptions {"machine_id": str, "pid": int, "started_at": str}
DELETE /api/adoptions/<session_id>
```

POST 返回 `{adoption_id, session_id, status: "pending"}`。`CONTROL_ACTIONS` 只增加 `adopt`、`detach`；control client 仅为 `adopt` 绕过旧的“session 已存在”检查，签名、nonce、machine、attempt scope 和固定动作检查不变。

- [ ] **Step 1: Write the failing test**

```python
def test_adoption_requires_operator_not_ingest(client):
    assert client.post("/api/adoptions", json=valid_candidate(),
                       headers=ingest_header()).status_code == 401

def test_post_then_delete_enqueues_detach(operator_client):
    created = operator_client.post("/api/adoptions", json=valid_candidate()).get_json()
    assert created["status"] == "pending"
    response = operator_client.delete(f"/api/adoptions/{created['session_id']}")
    assert response.status_code == 202
    assert last_supervisor_command()["action"] == "detach"

def test_adopt_creation_does_not_require_existing_session(control_client, signed_adopt):
    result = control_client.handle(signed_adopt)
    assert result["status"] == "rejected"
    assert result["reason"] == "unsupported_action"
```

- [ ] **Step 2: Verify failure**

Run: `pytest -q tests/test_adoption_http.py tests/test_control_client.py`
Expected: FAIL because routes, action codes, wiring and creation exception are absent.

- [ ] **Step 3: Implement minimally**

Register adoption service/routes only when `adoption_repositories_enabled`; parse JSON object; use `g.operator`; translate only known service codes. Add fixed actions and pending-only handlers. Preserve existing control validation order and supervisor auth.

- [ ] **Step 4: Verify pass**

Run: `pytest -q tests/test_adoption_http.py tests/test_control_client.py tests/test_supervisor_routes.py tests/test_supervisor_service.py`
Expected: PASS; ingest token cannot adopt and arbitrary shell/signal remains forbidden.

- [ ] **Step 5: Commit**

```bash
git add hub/http/adoption_routes.py tests/test_adoption_http.py hub/bootstrap.py hub/config.py hub/domain/control.py tools/supervisor/control_client.py hub/http/supervisor_routes.py tests/test_control_client.py tests/test_supervisor_service.py
git commit -m "feat: expose operator adoption commands"
```

**Rollback:** disable adoption flag or revert; runner and existing supervisor actions remain available.

**Phase 2 gate:** create/revoke pending row and inspect signed audit/commands; verify no target read or signal.

---

## Phase 3: probe attach + 追读

### Task 7: Add Supervisor attach/detach and identity gates

**独立交付:** probe 将已存在进程绑定为 managed entry；失败 fail-closed；detach 不发信号。

**Files:** modify `tools/supervisor/supervisor.py`, `tools/supervisor/control_client.py`; create `tests/test_supervisor_attach.py`。

**Interfaces:**

```python
class Supervisor:
    def attach_to_existing(
        self, pid: int, started_at: str, exe_path: str,
        native_file_path: str | None, agent_family: str,
    ) -> str:
        # adopted | pid_reused | exe_changed | no_permission |
        # unsupported_family | native_file_unreadable
        ...
    def detach(self, session_id: str) -> None: ...
```

成功时保存私有 `(pid, started_at, exe_path, pgid)`，公开状态只给 opaque `grp_<hex>`。

- [ ] **Step 1: Write the failing test**

```python
def test_attach_match_returns_adopted(supervisor, fake_proc):
    assert supervisor.attach_to_existing(
        fake_proc.pid, fake_proc.started_at, fake_proc.exe_path, None, "codex"
    ) == "adopted"

def test_reused_pid_fails_closed(supervisor, fake_proc):
    assert supervisor.attach_to_existing(
        fake_proc.pid, "2026-08-29T00:00:00Z", fake_proc.exe_path, None, "codex"
    ) == "pid_reused"
    assert supervisor.all_status() == []

def test_detach_removes_entry_without_signal(supervisor, fake_proc):
    supervisor.attach_to_existing(fake_proc.pid, fake_proc.started_at,
                                   fake_proc.exe_path, None, "codex")
    supervisor.detach(fake_proc.session_id)
    assert fake_proc.poll() is None
    assert fake_proc.signals == []

def test_attached_pause_signals_pgid(supervisor, fake_proc):
    supervisor.attach_to_existing(fake_proc.pid, fake_proc.started_at,
                                   fake_proc.exe_path, None, "codex")
    supervisor.pause_session(fake_proc.session_id)
    assert fake_proc.group_signals == [(fake_proc.pgid, "SIGSTOP")]
```

- [ ] **Step 2: Verify failure**

Run: `pytest -q tests/test_supervisor_attach.py`
Expected: FAIL because attach/detach do not exist.

- [ ] **Step 3: Implement minimally**

Re-read start time and executable immediately before registration; compare exact values; check same-user and native readability; reject unsupported family before adding entry. Create opaque group id and private OS handle on success. `detach` closes bridge/checkpoint if present and removes entry without calling any signal/kill/terminate method; it is idempotent.

- [ ] **Step 4: Verify pass**

Run: `pytest -q tests/test_supervisor_attach.py tests/test_supervisor.py tests/test_control_client.py`
Expected: PASS; launched sessions retain existing behavior and raw pid is absent from public status.

- [ ] **Step 5: Commit**

```bash
git add tools/supervisor/supervisor.py tools/supervisor/control_client.py tests/test_supervisor_attach.py
git commit -m "feat: attach existing processes to supervisor"
```

**Rollback:** revert; launched session control remains unchanged.

### Task 8: Wire successful attach to SessionBridge native tail

**独立交付:** attach 成功才启动 managed native tail；事件经既有 redaction/uploader；detach 后停止读取和上传。

**Files:** modify `tools/supervisor/supervisor.py`, `tools/supervisor/control_client.py`, `tools/session/bridge.py`, `hub/application/control_router.py`; create `tests/test_attach_bridge.py`; modify `tests/test_session_bridge.py`。

**Interfaces:**

```python
# Existing bridge methods are the only tailing API.
SessionBridge.open(manifest, session_config)
SessionBridge.start()
SessionBridge.ingest_native(path, checkpoint_path)
SessionBridge.flush()
SessionBridge.close()
```

Adopt config has `session_id=adopt_<opaque>`, authenticated machine id, opaque stream/group ids, family, `managed=True`, native path, existing spool/uploader, and starts `capture_quality="best_effort"`.

- [ ] **Step 1: Write the failing test**

```python
def test_successful_adopt_starts_managed_bridge(control_client, bridge_factory):
    receipt = control_client.handle(signed_adopt_command())
    assert receipt == {"status": "accepted", "reason": "adopted"}
    assert bridge_factory.last.started is True
    assert bridge_factory.last.config["managed"] is True

def test_failed_attach_never_starts_bridge(control_client, bridge_factory):
    control_client.supervisor.attach_result = "pid_reused"
    receipt = control_client.handle(signed_adopt_command())
    assert receipt["reason"] == "pid_reused"
    assert bridge_factory.last is None

def test_detach_closes_bridge_and_stops_upload(control_client, bridge):
    control_client.handle(signed_adopt_command())
    control_client.handle(signed_detach_command())
    assert bridge.closed is True
    assert bridge.uploaded_after_close == []
```

- [ ] **Step 2: Verify failure**

Run: `pytest -q tests/test_attach_bridge.py tests/test_session_bridge.py`
Expected: FAIL because successful attach is not wired to bridge.

- [ ] **Step 3: Implement minimally**

After `adopted`, construct existing bridge config and call `start` plus bounded `ingest_native`; keep checkpoint under existing spool root. Bridge/native exceptions produce fixed `native_file_unreadable` or `capture_gap` and clean private entry. Detach flushes/closes before removing governance. Forward only bounded receipt/audit values.

- [ ] **Step 4: Verify pass**

Run: `pytest -q tests/test_attach_bridge.py tests/test_session_bridge.py tests/test_transcript_repository.py tests/test_control_client.py`
Expected: PASS; no transcript before adoption, malformed native records remain per-event failures.

- [ ] **Step 5: Commit**

```bash
git add tools/supervisor/supervisor.py tools/supervisor/control_client.py tools/session/bridge.py hub/application/control_router.py tests/test_attach_bridge.py tests/test_session_bridge.py
git commit -m "feat: stream adopted sessions through session bridge"
```

**Rollback:** disable adoption dispatch or revert; launched bridge behavior remains.

**Phase 3 gate:** fixture/controlled process verifies pending -> adopted, redacted best-effort events, adopted -> detached with process alive and zero signal calls.

---

## Phase 4: 控制前置校验

### Task 9: Guard every existing control action for adopted identity

**独立交付:** pause/resume/terminate/quarantine/cancel_attempt 在 signal 前校验 adopted 状态及 `(pid, started_at, exe_path)`；失败撤销且无信号；launched session 路径不变。

**Files:** modify `tools/supervisor/supervisor.py`, `tools/supervisor/control_client.py`, `hub/application/control_router.py`, `hub/application/adoption_service.py`; create `tests/test_adoption_control_guard.py`。

**Interfaces:**

```python
def validate_attached_identity(self, session_id: str) -> str:
    # adopted | adoption_revoked | pid_reused | exe_changed | no_permission
    ...
```

For adopted target execution order is session/attempt check -> signed validation -> adoption status -> identity re-check -> scope conflict -> signal. `terminate` is never accepted for pending/revoked/mismatched target.

- [ ] **Step 1: Write the failing test**

```python
def test_revoked_adoption_control_has_no_signal(control_client, adopted):
    adopted.status = "revoked"
    result = control_client.handle(signed_action("pause_session", adopted.session_id))
    assert result["reason"] == "adoption_revoked"
    assert adopted.process.signals == []

def test_pid_reuse_revokes_without_terminate(control_client, adopted):
    adopted.process.started_at = "2026-08-31T00:00:00Z"
    result = control_client.handle(signed_action("terminate_session", adopted.session_id))
    assert result["reason"] == "pid_reused"
    assert adopted.process.signals == []
    assert adopted.repository.get(adopted.session_id).status == "revoked"

def test_launched_control_stays_compatible(control_client, launched):
    assert control_client.handle(
        signed_action("pause_session", launched.session_id))["status"] == "accepted"
```

- [ ] **Step 2: Verify failure**

Run: `pytest -q tests/test_adoption_control_guard.py`
Expected: FAIL because controls do not inspect adoption identity.

- [ ] **Step 3: Implement minimally**

Add identity recheck immediately before existing GroupOps signal call. On mismatch/permission loss invoke idempotent revoke/detach, append bounded audit and return fixed reason. Keep launched entry path, nonce, attempt scope and supersession ordering unchanged; never route mismatch cleanup through terminate.

- [ ] **Step 4: Verify pass**

Run: `pytest -q tests/test_adoption_control_guard.py tests/test_supervisor_attach.py tests/test_control_client.py tests/test_control_router.py`
Expected: PASS and no mismatch signal.

- [ ] **Step 5: Commit**

```bash
git add tools/supervisor/supervisor.py tools/supervisor/control_client.py hub/application/control_router.py hub/application/adoption_service.py tests/test_adoption_control_guard.py
git commit -m "feat: guard adopted session controls"
```

**Rollback:** revert; adoption dispatch can remain disabled.

### Task 10: Add operator source-control endpoint

**独立交付:** operator 能对 adopted session 发既有五种 action；HTTP 不接受任意 action，不越过 guard。

**Files:** modify `hub/http/adoption_routes.py`, `hub/http/session_routes.py`, `hub/bootstrap.py`, `frontend/views/machine.js`; create `tests/test_adoption_control_http.py`; modify `tests/test_frontend_adoption_contracts.py`。

**Interfaces:**

```text
POST /api/adoption/<session_id>/source/control
{"action":"pause_session|resume_session|terminate_session|quarantine_session|cancel_attempt",
 "reason_code": str}
```

成功返回 `202`、command id 和 bounded `pending`；unknown action/revoked/wrong machine/invalid reason 只返回固定 4xx code；请求不接受 pid、signal、shell 或 command text。

- [ ] **Step 1: Write the failing test**

```python
def test_source_control_requires_operator(adoption_client):
    response = adoption_client.post(
        "/api/adoption/adopt_x/source/control",
        json={"action": "pause_session", "reason_code": "operator_requested"},
        headers=ingest_header())
    assert response.status_code == 401

def test_source_control_rejects_arbitrary_action(operator_client):
    response = operator_client.post(
        "/api/adoption/adopt_x/source/control",
        json={"action": "exec_shell", "reason_code": "x"})
    assert response.json["code"] == "unsupported_action"

def test_revoked_source_cannot_be_controlled(operator_client, revoked_session):
    response = operator_client.post(
        f"/api/adoption/{revoked_session}/source/control",
        json={"action": "terminate_session", "reason_code": "operator_requested"})
    assert response.json["code"] == "adoption_revoked"
```

- [ ] **Step 2: Verify failure**

Run: `pytest -q tests/test_adoption_control_http.py tests/test_frontend_adoption_contracts.py`
Expected: FAIL because route and action callbacks are absent.

- [ ] **Step 3: Implement minimally**

Lookup path session id, require authenticated operator and status adopted, allow only existing five action names, then call existing enqueue with authenticated machine and bounded reason. Frontend renders controls by status and updates from fixed receipts using DOM APIs; it never submits arbitrary text.

- [ ] **Step 4: Verify pass**

Run: `pytest -q tests/test_adoption_control_http.py tests/test_adoption_http.py tests/test_frontend_adoption_contracts.py tests/test_session_routes.py`
Expected: PASS; old session routes and `/api/v1` remain compatible.

- [ ] **Step 5: Commit**

```bash
git add hub/http/adoption_routes.py hub/http/session_routes.py hub/bootstrap.py frontend/views/machine.js tests/test_adoption_control_http.py tests/test_frontend_adoption_contracts.py
git commit -m "feat: control adopted sources from operator API"
```

**Rollback:** disable/remove source-control route; runner and supervisor poll/receipt remain.

**Phase 4 gate:** pause/resume signal correct pgid; terminate only after second identity check; revoked and pid reuse produce zero signal.

---

## Phase 5: exact 升级 + 撤销闭环

### Task 11: Add explicit exact-capture upgrade

**独立交付:** adopted session 可从 best_effort 升 exact；升级有审计，后续 raw 复用既有 AEAD/quota/retention/read audit。

**Files:** modify `hub/http/adoption_routes.py`, `hub/application/adoption_service.py`, `hub/infrastructure/adoption_repository.py`, `hub/http/session_routes.py`, `tools/session/bridge.py`; create `tests/test_exact_capture_upgrade.py`; modify transcript/adoption HTTP tests。

**Interfaces:**

```text
POST /api/adoptions/<session_id>/capture-exact
-> {"session_id": str, "capture_quality": "exact", "status": "adopted"}
```

```python
def upgrade_capture_exact(self, session_id: str, actor: str) -> Adoption: ...
```

- [ ] **Step 1: Write the failing test**

```python
def test_exact_upgrade_is_operator_audited(operator_client, adopted_session):
    response = operator_client.post(
        f"/api/adoptions/{adopted_session}/capture-exact")
    assert response.status_code == 200
    assert response.json["capture_quality"] == "exact"
    assert any(a["action"] == "capture_exact"
               for a in transcript_repo.read_audit())

def test_revoked_cannot_upgrade(operator_client, revoked_session):
    response = operator_client.post(
        f"/api/adoptions/{revoked_session}/capture-exact")
    assert response.json["code"] == "adoption_revoked"

def test_exact_event_uses_redacted_and_encrypted_raw(adopted_session):
    ingest_event(adopted_session, quality="exact", text="password=secret")
    assert "secret" not in transcript_repo.read_redacted(adopted_session)[0]["payload"]["text"]
    assert transcript_repo.read_raw(adopted_session, "evt_1", actor="op")["payload"]["text"] == "password=secret"
```

- [ ] **Step 2: Verify failure**

Run: `pytest -q tests/test_exact_capture_upgrade.py`
Expected: FAIL because quality upgrade route/method are absent.

- [ ] **Step 3: Implement minimally**

Require adopted status, atomically update adoption and managed session quality, append code-only `capture_exact` audit, and make repeat upgrade idempotent. Reuse TranscriptRepository’s existing redaction, AEAD write, quota, retention and raw-read audit; never put plaintext in adoption rows or responses. Return fixed `adoption_revoked`, `unknown_session`, or `capture_quality_immutable` where applicable.

- [ ] **Step 4: Verify pass**

Run: `pytest -q tests/test_exact_capture_upgrade.py tests/test_transcript_repository.py tests/test_session_routes.py`
Expected: PASS including keyless/wrong-key raw fail-closed behavior.

- [ ] **Step 5: Commit**

```bash
git add hub/http/adoption_routes.py hub/application/adoption_service.py hub/infrastructure/adoption_repository.py hub/http/session_routes.py tools/session/bridge.py tests/test_exact_capture_upgrade.py tests/test_transcript_repository.py tests/test_adoption_http.py
git commit -m "feat: add audited exact capture upgrade"
```

**Rollback:** disable exact endpoint/flag; best-effort redacted capture remains.

### Task 12: Complete revoke/drift lifecycle and end-to-end acceptance

**独立交付:** receipt promotion、漂移自动撤销、显式 revoke、detach 无信号、前端状态、bootstrap flags、文档和验收全部闭环。

**Files:** modify `hub/application/adoption_service.py`, `hub/application/control_router.py`, `hub/http/adoption_routes.py`, `hub/bootstrap.py`, `hub/config.py`, `tools/supervisor/supervisor.py`, `tools/supervisor/control_client.py`, `frontend/views/machine.js`; create `tests/test_adoption_lifecycle.py`, `tests/test_adoption_e2e.py`; modify `docs/HANDOFF.md` and `README.md`。

**Interfaces:**

```python
ADOPT_SUCCESS = "adopted"
ADOPT_DRIFT_CODES = frozenset({
    "pid_reused", "exe_changed", "no_permission",
    "unsupported_family", "native_file_unreadable",
})
```

Known `adopted` receipt promotes pending; drift receipt sets revoked, audits and writes bounded policy signal, then idempotently detaches; explicit revoke sets revoked and enqueues detach once. Frontend displays pending/adopted/revoked and fixed errors; SSE continues through existing machine stream.

- [ ] **Step 1: Write the failing test**

```python
def test_adopted_receipt_promotes_and_observes(e2e):
    row = e2e.operator_adopt()
    assert e2e.repo.get(row.session_id).status == "pending"
    e2e.probe_poll_and_receipt("adopted")
    assert e2e.repo.get(row.session_id).status == "adopted"
    e2e.ingest_native_line("hello")
    assert e2e.read_redacted(row.session_id)

def test_revoke_detaches_without_signal(e2e):
    row = e2e.adopted_session()
    e2e.operator_delete(row.session_id)
    assert e2e.repo.get(row.session_id).status == "revoked"
    e2e.probe_poll_and_receipt("detached")
    assert e2e.process.poll() is None
    assert e2e.process.signals == []

def test_drift_revokes_and_never_terminates(e2e):
    row = e2e.adopted_session()
    e2e.process.started_at = "2026-09-01T00:00:00Z"
    e2e.operator_control(row.session_id, "terminate_session")
    e2e.probe_poll_and_receipt("pid_reused")
    assert e2e.repo.get(row.session_id).status == "revoked"
    assert e2e.process.signals == []
    assert any(a["action"] == "adoption_drift" for a in e2e.audit())

def test_disabled_flag_has_no_adoption_service(app):
    assert app.extensions["fleet"]["services"].get("adoptions") is None
    assert app.post("/api/adoptions", json={}).status_code == 404
```

- [ ] **Step 2: Verify failure**

Run: `pytest -q tests/test_adoption_lifecycle.py tests/test_adoption_e2e.py`
Expected: FAIL until receipt promotion, drift revoke, idempotent detach and final flag wiring exist.

- [ ] **Step 3: Implement minimally**

Map only known receipt codes. Make promotion/revoke/detach idempotent under retries. On drift persist revoked, append `adoption_drift` and bounded policy signal, and enqueue detach without signal fields. Wire explicit config/health/rollback docs and reuse existing SSE snapshot publisher. Disable frontend actions while pending/revoked and avoid raw pid/path/content in public UI.

- [ ] **Step 4: Verify pass**

Run:

```bash
pytest -q
pytest -q tests/test_discovery.py tests/test_adoption_service.py \
  tests/test_supervisor_attach.py tests/test_adoption_control_guard.py \
  tests/test_exact_capture_upgrade.py tests/test_adoption_e2e.py
```

Expected: full suite green; metadata visibility, no pre-adopt transcript, explicit adoption, redacted stream, guarded controls, live detach, exact AEAD/raw audit, and pid-reuse no-signal revoke all pass.

- [ ] **Step 5: Commit**

```bash
git add hub/application/adoption_service.py hub/application/control_router.py hub/http/adoption_routes.py hub/bootstrap.py hub/config.py tools/supervisor/supervisor.py tools/supervisor/control_client.py frontend/views/machine.js tests/test_adoption_lifecycle.py tests/test_adoption_e2e.py docs/HANDOFF.md README.md
git commit -m "feat: complete adoption revoke and drift lifecycle"
```

**Rollback:** turn off adoption repository and probe adoption dispatch; discovery may remain enabled. Verify old `/api/*`, `/api/v1`, task/runner, observation, SSE, and independent front/back rollback paths.

**Phase 5 gate:** run spec §6.1/§6.3 matrix on fixture process and at least one controlled supported family; retain receipt/audit/error-code evidence without public raw pid or plaintext.

---

## Self-Review

### Spec coverage

- §0 goals/non-goals: Tasks 1-3 metadata-only discovery; Tasks 5-6 explicit operator adoption; Tasks 8/11 gate content and exact; Task 12 revoke without signal.
- §3 contracts: Task 1 defines `Instance` and `discover_instances`; Task 4 domain/repository; Task 5 exact service signatures; Task 7 supervisor attach/detach; Task 6 actions.
- §4 flows: 4.1 Tasks 1-3; 4.2 Tasks 5-6; 4.3 Tasks 7-8/12; 4.4 Task 11; 4.5 Task 12; 4.6 Tasks 9-10.
- §5 errors/boundaries: fixed action set in Task 6; bounded codes in Tasks 5/7/9; independent DB in Task 4; pull-only and auth separation in global constraints/tests.
- §6 matrix: discovery, service, attach, HTTP, control guard, frontend, exact, lifecycle and E2E files are explicit; final `pytest -q` is required.
- §7 compatibility: old observation/session/transcript/task/runner/SSE/API contracts remain additive and rollbackable.
- §8 five phases: Phase 1 Tasks 1-3; Phase 2 Tasks 4-6; Phase 3 Tasks 7-8; Phase 4 Tasks 9-10; Phase 5 Tasks 11-12. Every phase has an independent gate and rollback.

### Placeholder and consistency checks

- The plan contains no placeholder instructions or incomplete implementation steps.
- `sanitize_instances()` is used consistently, matching repository `sanitize_*` convention.
- `adopt_` is used consistently and is accepted by `is_valid_session_id`.
- `SupervisorService.enqueue` uses the existing `(machine, session_id, attempt_id, action, reason_code, *, nonce)` signature.
- `SessionBridge` uses existing `open/start/ingest_native/flush/close`; no second tail protocol is introduced.
- Fixed existing action names are preserved; only `adopt` and `detach` are added.
- State transitions are consistently `pending -> adopted -> revoked`; repeated receipt/revoke/exact operations are idempotent.
- Each task names files, interfaces, failure test, failure command, implementation behavior, pass command, commit and rollback.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-30-agent-discovery-adoption-plan.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints for review

Which approach?
