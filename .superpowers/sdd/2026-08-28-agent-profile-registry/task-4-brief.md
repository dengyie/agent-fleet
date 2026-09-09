# Task 4 brief: session→agent_family 绑定(hub 存储 + 服务 + pin 测试)

这是你的唯一需求来源。按 Step 1-7 顺序执行(TDD)。本任务把「会话跑在哪个 agent 上」从事件一路落到 hub 会话行与公开 DTO。

**工作目录**:`/opt/agent-fleet/.claude/worktrees/agent-profile-registry/`(先 `cd` 过去)

**背景(已核实,勿重复改动 producer)**:session 事件 wire 合同 `session_schema.py` 已 allowlist `session_start`/`session_metadata` 的 `payload.agent_family`;producer `tools/session/bridge.py:616` 已下发 `ev["payload"] = {"agent_family": self._agent_family, ...}`。**还缺**:hub `sessions` 表无 `agent_family` 列、`SessionService._session_spec_from_event` 不提取它、DTO 无从浮现。

**全局约束**:fail-closed 不动;只持久化 allowlisted 字段(`agent_family` 是家族名,非秘密);老库迁移幂等(重复 `init()` 不报错、不丢行);`agent_family` 有界 sanitize(≤64、拒绝 `/` `\` 形状)。

## Step 1: 写失败测试

新建 `tests/test_session_repository.py`(若已存在则在其内追加):

```python
import sqlite3

from hub.infrastructure.session_repository import SessionRepository


def test_agent_family_roundtrips(tmp_path):
    repo = SessionRepository(tmp_path / "s.db")
    repo.init()
    repo.upsert_session({
        "session_id": "s1", "machine_id": "m1", "managed": True,
        "process_group_id": "grp_1", "agent_family": "codex",
    })
    row = repo.get_session("s1")
    assert row["agent_family"] == "codex"


def test_agent_family_defaults_empty_when_absent(tmp_path):
    repo = SessionRepository(tmp_path / "s.db")
    repo.init()
    repo.upsert_session({"session_id": "s2", "machine_id": "m1"})
    row = repo.get_session("s2")
    assert row["agent_family"] == ""


def test_agent_family_rejects_path_shapes(tmp_path):
    repo = SessionRepository(tmp_path / "s.db")
    repo.init()
    repo.upsert_session({
        "session_id": "s3", "machine_id": "m1", "agent_family": "../../etc/passwd",
    })
    row = repo.get_session("s3")
    assert row["agent_family"] == ""


def test_legacy_db_migrates_without_losing_rows(tmp_path):
    db = tmp_path / "s.db"
    repo = SessionRepository(db)
    repo.init()
    repo.upsert_session({"session_id": "old", "machine_id": "m1"})
    conn = sqlite3.connect(str(db))
    conn.execute("ALTER TABLE sessions DROP COLUMN agent_family")
    conn.close()
    repo2 = SessionRepository(db)
    repo2.init()  # 幂等,不抛
    assert repo2.get_session("old")["session_id"] == "old"
    assert repo2.get_session("old")["agent_family"] == ""
```

## Step 2: 运行确认失败

Run: `python3 -m pytest tests/test_session_repository.py -q`
Expected: FAIL(`row["agent_family"]` KeyError —— 列不存在;至少第一个测试失败)

## Step 3: 实现 hub 存储(`hub/infrastructure/session_repository.py`)

1. `SCHEMA_VERSION = 1` → `SCHEMA_VERSION = 2`。
2. `init()` 的 `executescript` CREATE TABLE `sessions` 在 `attempt_id TEXT,` 后加一列 `agent_family TEXT,`(即 `attempt_id TEXT,\n agent_family TEXT,\n finished_at TEXT`)。
3. 在 `executescript(...)` 之后、`INSERT OR REPLACE INTO meta` 之前,加 `self._migrate_v2(conn)`。
4. 新增方法(放 `_connect` 附近):

```python
    def _migrate_v2(self, conn) -> None:
        """Idempotent v1->v2: add the ``agent_family`` column."""
        try:
            conn.execute("ALTER TABLE sessions ADD COLUMN agent_family TEXT")
        except sqlite3.OperationalError:
            pass  # column already present (fresh v2 schema or re-run)
```

5. `_normalize_spec`:在 `clean` dict 构建(then process_group_id/attempt_id 处理)之后、return 前,追加:

```python
        family = spec.get("agent_family")
        if (isinstance(family, str) and family
                and "/" not in family and "\\" not in family):
            clean["agent_family"] = family[:64]
```

6. `_clean_row`:返回 dict 追加 `"agent_family": str(row.get("agent_family") or "")[:64],`。
7. `upsert_session` 的 INSERT 列与值、ON CONFLICT 更新语句都加入 `agent_family`(列顺序:`... process_group_id, attempt_id, agent_family, finished_at`;值紧跟 `clean.get("attempt_id")` 后为 `clean.get("agent_family")`;ON CONFLICT 加 `agent_family=excluded.agent_family`)。

## Step 4: 实现服务提取(`hub/application/session_service.py`)

`_session_spec_from_event` 在 `if attempt: spec["attempt_id"] = attempt` 之后、`return spec` 之前,追加(`Mapping` 已在顶部 import):

```python
        payload = event.get("payload")
        family = payload.get("agent_family") if isinstance(payload, Mapping) else None
        if isinstance(family, str) and family:
            spec["agent_family"] = family[:64]
```

## Step 5: 追加 wire 级 pin 测试(不改 producer)

在 `tests/test_session_schema.py`(或最贴近的 session_schema 测试文件)追加一条,固定 session_start 的 `payload.agent_family` 能通过 validation 保留:

```python
from session_schema import validate_event


def test_session_start_payload_keeps_agent_family():
    ev = {
        "schema_version": 1,
        "event_id": "evt_1", "stream_id": "stream_1",
        "machine_id": "m1", "session_id": "s1", "sequence": 1,
        "kind": "session_start", "capture_quality": "best_effort",
        "emitted_at": "2026-08-28T00:00:00Z",
        "payload": {"agent_family": "codex"},
    }
    clean = validate_event(ev)
    assert clean["payload"].get("agent_family") == "codex"
```

若该测试文件已装同款,跳过并在报告中说明。

## Step 6: 运行确认通过

Run:
```
python3 -m pytest tests/test_session_repository.py tests/test_session_schema.py -q
python3 -m pytest tests/test_session_service.py -q
```
Expected: PASS;若 `tests/test_session_service.py` 按旧 `_clean_row` 精确 dict 断言,同步更新为含 `agent_family`(在报告中说明)。

## Step 7: 提交

```bash
git add hub/infrastructure/session_repository.py hub/application/session_service.py tests/test_session_repository.py tests/test_session_schema.py
git commit -m "feat: bind session rows to agent_family (schema v2)"
```

**完成报告**:写 status、提交哈希、一行测试摘要、疑虑(尤其 Step 5/6 的文件是否存在与是否需调整)到
`/opt/agent-fleet/.claude/worktrees/agent-profile-registry/.superpowers/sdd/2026-08-28-agent-profile-registry/task-4-report.md`
返回消息只给这些字段。不得派发子 agent。