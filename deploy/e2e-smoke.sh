#!/usr/bin/env bash
# deploy/e2e-smoke.sh — agent-fleet v4 本地全链路冒烟
# 用法: deploy/e2e-smoke.sh            # 本地临时 hub（默认）
#       deploy/e2e-smoke.sh --endpoint https://hub.example.com   # 打指定 hub（需凭据环境变量）
set -euo pipefail
cd "$(dirname "$0")/.."

# --- Python 解析（本地冒烟在临时目录跑仓库副本）---
PY="${PYTHON:-.venv/bin/python}"
if [ -x "$PY" ]; then
  # 解析为绝对路径（冒烟 hub 在临时目录中以副本运行）
  PY="$(cd "$(dirname "$PY")" && pwd)/$(basename "$PY")"
else
  PY="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)"
fi
[ -n "$PY" ] && [ -x "$PY" ] || { echo "SMOKE FAIL: 找不到 python3；请设置 PYTHON=… 指向含 flask/pyyaml 的解释器" >&2; exit 1; }

ENDPOINT=""
LOCAL=""
if [ "${1:-}" = "--endpoint" ]; then
  ENDPOINT="${2:?usage: e2e-smoke.sh [--endpoint URL]}"
fi

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
info() { echo "smoke: $*"; }

WORK="$(mktemp -d)"
HUB_PID=""
cleanup() {
  rm -rf "$WORK" 2>/dev/null || true
  if [ -n "$HUB_PID" ]; then kill "$HUB_PID" 2>/dev/null || true; fi
}
trap cleanup EXIT

INGEST_TOKEN="smoke-ingest-$$"
RUNNER_SECRET="smoke-runner-$$"
MACHINE="smoke-$(( $$ % 1000 ))"

if [ -z "$ENDPOINT" ]; then
  # --- 本地模式：临时目录跑仓库副本，不污染真实 state/credentials ---
  mkdir -p "$WORK/state" "$WORK/credentials"
  cp -r hub tools connectors report_schema.py requirements.txt "$WORK/"
  # 隔离的白名单 hosts.yaml：仅冒烟机器 + projects: [demo]
  cat > "$WORK/hosts.yaml" <<EOF
hosts:
  - name: $MACHINE
    desc: smoke
    transport: ingest
    stale_after_s: 300
    projects: [demo]
EOF
  printf '{"%s": "%s"}' "$MACHINE" "$RUNNER_SECRET" > "$WORK/credentials/runner-credentials.json"
  chmod 600 "$WORK/credentials/runner-credentials.json"
  (
    cd "$WORK"
    AGENT_FLEET_INGEST_TOKEN="$INGEST_TOKEN" \
      exec "$PY" hub/web.py --port 8799 --host 127.0.0.1 --dev-operator smoke@local
  ) > "$WORK/hub.log" 2>&1 &
  HUB_PID=$!
  LOCAL=1
  ENDPOINT="http://127.0.0.1:8799"
  ready=0
  for _ in {1..30}; do
    if curl -sf "$ENDPOINT/api/status" >/dev/null 2>&1; then ready=1; break; fi
    sleep 0.3
  done
  [ "$ready" = 1 ] || fail "hub 未就绪（见 $WORK/hub.log）"
  info "本地 hub 已起: $ENDPOINT (machine=$MACHINE)"
else
  # --- 远端模式：凭据必须来自环境变量，绝不内嵌 ---
  : "${AGENT_FLEET_INGEST_TOKEN:?需环境变量 AGENT_FLEET_INGEST_TOKEN}"
  : "${RUNNER_CREDENTIAL:?需环境变量 RUNNER_CREDENTIAL (<machine>:<secret>)}"
  INGEST_TOKEN="$AGENT_FLEET_INGEST_TOKEN"
  MACHINE="${RUNNER_CREDENTIAL%%:*}"
  RUNNER_SECRET="${RUNNER_CREDENTIAL#*:}"
  if [ -z "$MACHINE" ] || [ "$MACHINE" = "$RUNNER_CREDENTIAL" ]; then
    fail "RUNNER_CREDENTIAL 应为 <machine>:<secret>"
  fi
fi

# 1) ingest 上线
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$ENDPOINT/api/ingest" \
  -H "X-Agent-Fleet-Token: $INGEST_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"machine\":\"$MACHINE\",\"agents\":{\"codex\":{\"installed\":true}},\"system\":{\"platform\":\"smoke\"}}")
[ "$code" = 200 ] || fail "ingest 返回 $code"
info "1. ingest 200 ✓"

# 2) 建任务（本地模式用 dev-operator；远端模式依赖 CF Access cookie 不可用 → 跳过）
if [ -z "$LOCAL" ]; then
  info "2. 远端模式：跳过 operator API 冒烟（需 CF Access 浏览器会话）"
  exit 0
fi

TASK_JSON=$(curl -sf -X POST "$ENDPOINT/api/tasks" -H 'Content-Type: application/json' \
  -d "{\"machine\":\"$MACHINE\",\"agent_type\":\"codex\",\"project\":\"demo\",\"instruction\":\"写 hello.txt 并打印 done\",\"client_token\":\"smoke-$$\"}") \
  || fail "创建任务失败"
TASK_ID=$(printf '%s' "$TASK_JSON" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["task"]["task_id"])')
info "2. 任务已建: $TASK_ID ✓"

# 幂等：同 client_token 再建
CREATED=$(curl -s -X POST "$ENDPOINT/api/tasks" -H 'Content-Type: application/json' \
  -d "{\"machine\":\"$MACHINE\",\"agent_type\":\"codex\",\"project\":\"demo\",\"instruction\":\"重复\",\"client_token\":\"smoke-$$\"}" \
  | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["created"])')
[ "$CREATED" = "False" ] || fail "client_token 幂等失效"
info "3. client_token 幂等 ✓"

# 3) 假项目 + runner 单轮（假 codex：真实子进程写文件）
#    runner_config.load_config() 出于安全强制 hub 为 https://（runner 凭据仅走 HTTPS），
#    本地回环 http hub 沿用 tests/test_runner.py E2E 的做法：直接构造 RunnerConfig +
#    单轮 poll_once（tools/agent-runner.py 同一执行核心，不改 Phase 1-3 代码）。
PROJ="$WORK/proj"; mkdir -p "$PROJ"
git -C "$PROJ" init -q
git -C "$PROJ" -c user.email=s@s -c user.name=s commit -q --allow-empty -m init
printf '%s\n' "$RUNNER_SECRET" > "$WORK/runner-credential"
chmod 600 "$WORK/runner-credential"

cat > "$WORK/run_once.py" <<'PYEOF'
import importlib.util
import os
import sys
from pathlib import Path

fleet = Path(__file__).resolve().parent
sys.path.insert(0, str(fleet))
spec = importlib.util.spec_from_file_location(
    "agent_runner", fleet / "tools" / "agent-runner.py")
mod = importlib.util.module_from_spec(spec)
sys.modules["agent_runner"] = mod
spec.loader.exec_module(mod)
from tools import runner_config

cfg = runner_config.RunnerConfig(
    hub=os.environ["SMOKE_HUB"],
    machine=os.environ["SMOKE_MACHINE"],
    credential=os.environ["SMOKE_SECRET"],
    runner_id=os.environ["SMOKE_MACHINE"],
    projects={"demo": Path(os.environ["SMOKE_PROJ"])},
    agents={"codex": {
        "command": [os.environ["SMOKE_PY"], "-c",
                    "import pathlib; pathlib.Path('hello.txt').write_text('hi'); print('done')"],
        "timeout_s": 60}},
    poll_interval_s=1, heartbeat_interval_s=1,
    cache_dir=fleet / "cache",
)
try:
    ok = mod.poll_once(cfg)
except Exception as exc:
    print(f"RUNNER ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
    raise SystemExit(1)
if not ok:
    print("RUNNER MISS: 本轮未领取到任务", file=sys.stderr)
    raise SystemExit(1)
PYEOF

SMOKE_HUB="$ENDPOINT" SMOKE_MACHINE="$MACHINE" SMOKE_SECRET="$RUNNER_SECRET" \
  SMOKE_PROJ="$PROJ" SMOKE_PY="$PY" \
  "$PY" "$WORK/run_once.py" || fail "runner 执行失败"
info "4. runner 单轮完成 ✓"

# 4) 断言终态与结果
TASK=$(curl -sf "$ENDPOINT/api/tasks/$TASK_ID") || fail "获取任务详情失败"
STATE=$(printf '%s' "$TASK" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["task"]["state"])')
[ "$STATE" = "succeeded" ] || fail "任务状态 $STATE != succeeded"
printf '%s' "$TASK" | grep -q 'hello.txt' || fail "diff_stat 缺 hello.txt"
info "5. 任务 succeeded + diff 含 hello.txt ✓"

# 5) SSE 可达（curl 超时退出码 28 属预期，只校验收到的首帧）
SSE_OUT=$(curl -sN --max-time 2 "$ENDPOINT/api/stream" || true)
printf '%s' "$SSE_OUT" | head -1 | grep -q ': connected' \
  || fail "SSE 首帧缺失"
info "6. SSE 连接 ✓"

# --- Task 21 释放门：前端独立静态打包断言 + 后端独立启动断言 ---

# 6a) 临时前端静态 release 打包：包内必须含 index.html/config.js/client/SSE/视图模块。
#     deploy/package-frontend-release.sh 是 fail-closed 的独立打包器（无 npm/构建链/外网）。
PACKAGE_DIR="$WORK/frontend-release"
bash deploy/package-frontend-release.sh "$PACKAGE_DIR" "smoke-$(( $$ % 1000 ))" >/dev/null \
  || fail "前端静态打包失败"
for rel in index.html config.js api/client.js realtime/sse.js \
           views/fleet.js views/machine.js views/task.js; do
  [ -f "$PACKAGE_DIR/$rel" ] || fail "前端 release 缺模块: $rel"
done
info "6a. 前端静态包含 index/config/client/SSE/视图模块 ✓"

# 6b) 后端独立启动：hub.bootstrap.create_app 在无 frontend/ 文件时也能装配成功。
#     指向一个不存在的前端目录断言后端不依赖任何前端文件；不触碰生产 load_config()
#     的 HTTPS 校验、也不使用真实凭据。
BODY="$(printf '%s' "$(
  AGENT_FLEET_INGEST_TOKEN="" SMOKE_ROOT="$WORK" \
  "$PY" - "$WORK" <<'PYEOF'
import os, sys
from pathlib import Path
_fleet = Path(sys.argv[1])
sys.path.insert(0, str(_fleet))               # $WORK 内的 hub 包
from hub.bootstrap import create_app
from hub.config import FleetConfig

root = _fleet
fake_frontend = root / "no-such-frontend"      # 显式不存在
cfg = FleetConfig(
    root=root,
    state_dir=root / "state",
    hosts_file=root / "hosts.yaml",
    event_log=root / "state" / "events.jsonl",
    task_db=root / "state" / "fleet.db",
    ingest_token="",                            # 空 token 仅作开箱断言，不设生产状态
    tasks_enabled=False,
    frontend_cutover=False,
    frontend_dir=fake_frontend,                 # 后端启动不读取任何前端文件
)
app = create_app(cfg)
print("CREATE_APP ok")
PYEOF
)" || true)"
printf '%s' "$BODY" | grep -q 'CREATE_APP ok' || fail "后端 create_app 启动失败（无前端文件时）"
info "6b. hub.bootstrap.create_app 无前端文件独立启动 ✓"

# 6c) 会话数据面 / supervisor 控制面 route 装配：显式启用时（无前端、无真实凭据）
#     后端必须注册 session 收口与 supervisor poll/receipt 路由，且既有 /api/status
#     与 /api/stream 仍可达。仅断言 create_app 装配，不发起任何 poll/receipt 请求。
BODY2="$(printf '%s' "$(
  AGENT_FLEET_INGEST_TOKEN="" SMOKE_ROOT="$WORK" \
  "$PY" - "$WORK" <<'PYEOF'
import sys
from pathlib import Path
_fleet = Path(sys.argv[1])
sys.path.insert(0, str(_fleet))
from hub.bootstrap import create_app
from hub.config import FleetConfig

root = _fleet / "sess-root"
(root / "state").mkdir(parents=True, exist_ok=True)
cfg = FleetConfig(
    root=root,
    state_dir=root / "state",
    hosts_file=root / "state" / "hosts.yaml",
    event_log=root / "state" / "events.jsonl",
    task_db=root / "state" / "fleet_tasks.db",
    ingest_token="",
    tasks_enabled=False,
    frontend_cutover=False,
    frontend_dir=root / "no-such-frontend",
    session_repositories_enabled=True,
    session_db=root / "var" / "sessions" / "meta.db",
    session_transcript_root=root / "var" / "sessions" / "transcripts",
    session_encryption_raw=bytes(range(32)),
    supervisor_enabled=True,
    supervisor_signing_raw=bytes(range(32)),
)
app = create_app(cfg)
rules = sorted(r.rule for r in app.url_map.iter_rules())
for required in ("/api/session-events", "/api/sessions",
                 "/api/supervisor/poll", "/api/supervisor/receipts",
                 "/api/status", "/api/stream"):
    if required not in rules:
        raise SystemExit(f"missing route: {required}")
print("SESSION_ROUTES ok")
PYEOF
)" || true)"
printf '%s' "$BODY2" | grep -q 'SESSION_ROUTES ok' \
  || fail "session/supervisor 路由装配失败（create_app）"
info "6c. session 数据面 + supervisor 控制面路由独立装配 ✓"

echo "SMOKE OK"