# Phase 4 联调加固 Implementation Plan

> **Status (2026-09-08): historical / implemented.** Open checkboxes below are the original plan and were not kept in sync after merge. Do not re-implement from unchecked boxes. Remaining work is recorded in `docs/HANDOFF.md` §六 as `done` or `deferred-with-condition`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** v4 全链路联调与生产加固：E2E 冒烟脚本、Cloudflare Access operator 认证配置、hosts.yaml 项目白名单、HK 部署步骤、文档收口。

**Architecture:** 不改代码行为，只加部署/验证资产；CF Access 在 Cloudflare 边缘认证（dashboard/API 页面路径），ingest/commands 路径走 bypass 规则保持 token/credential 认证。

**Tech Stack:** bash + curl + python3（冒烟脚本）；Cloudflare Zero Trust 配置（文档）；unittest 回归。

**Spec:** docs/superpowers/specs/2026-08-19-v4-optimization-design.md（§8.3 E2E 冒烟、§7.4 安全防线、§9 Phase 4）

**前置：** Phase 1-3 计划已完成。

## Global Constraints

- 沿用 Phase 1-3 全部约束；token/credential 出现在命令示例中一律用 `<secret>` 占位。
- CF Access 配置只写文档与操作清单，不把任何真实凭据/账号 ID 写进 git。
- 冒烟脚本默认打**本地临时 hub**，不碰生产；打生产需显式 `--endpoint`。

## 文件结构

| 文件 | 动作 | 职责 |
|---|---|---|
| `deploy/e2e-smoke.sh` | 新建 | 本地全链路冒烟（hub + 假 adapter runner） |
| `deploy/cloudflare-access.md` | 新建 | CF Access 配置文档（operator 认证上线步骤） |
| `hosts.yaml` | 修改 | 各 host 加 `projects` 白名单 |
| `deploy/nginx-expose.md` | 修改 | 补充 v4 路由与 CF Access 衔接说明 |
| `README.md`、`docs/HANDOFF.md` | 修改 | v4 上线状态收口 |

---

### Task 1: E2E 冒烟脚本

**Files:**
- Create: `deploy/e2e-smoke.sh`
- Test: 运行脚本本身即测试（退出码非 0 即失败）

**Interfaces:**
- Consumes: Phase 1-3 全部产物。
- Produces: `deploy/e2e-smoke.sh [--endpoint URL]`：无参时本地起临时 hub（端口 8799、临时 state 目录、`--dev-operator smoke@local`、临时 runner credential + hosts 白名单），执行：ingest 上线 → 建任务 → runner --once（假 codex 命令：python 写文件）→ 断言 succeeded + diff 含文件名 → 重复提交结果断言幂等 → kill 清理。任何一步失败 `exit 1` 并打印失败点。

- [ ] **Step 1: 写 deploy/e2e-smoke.sh**

```bash
#!/usr/bin/env bash
# deploy/e2e-smoke.sh — agent-fleet v4 本地全链路冒烟
# 用法: deploy/e2e-smoke.sh            # 本地临时 hub（默认）
#       deploy/e2e-smoke.sh --endpoint https://hub.example.com   # 打指定 hub（需凭据环境变量）
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-.venv/bin/python}"
[ -x "$PY" ] || PY=python3
# 解析为绝对路径（冒烟 hub 在临时目录中以副本运行）
PY="$(cd "$(dirname "$PY")" && pwd)/$(basename "$PY")" 2>/dev/null || PY="$(command -v "$PY")"

ENDPOINT=""
LOCAL=""
if [ "${1:-}" = "--endpoint" ]; then
  ENDPOINT="${2:?usage: e2e-smoke.sh [--endpoint URL]}"
fi

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
info() { echo "smoke: $*"; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"; [ -n "${HUB_PID:-}" ] && kill "$HUB_PID" 2>/dev/null || true' EXIT

INGEST_TOKEN="smoke-ingest-$$"
RUNNER_SECRET="smoke-runner-$$"
MACHINE="smoke-$(( $$ % 1000 ))"

if [ -z "$ENDPOINT" ]; then
  # --- 本地模式：在临时目录跑仓库副本，不污染真实 state/credentials ---
  mkdir -p "$WORK/state" "$WORK/credentials"
  cp -r hub tools connectors report_schema.py requirements.txt "$WORK/"
  cp hosts.yaml "$WORK/hosts.yaml"
  # 给冒烟机器注册项目白名单
  cat >> "$WORK/hosts.yaml" <<EOF
  - name: $MACHINE
    desc: smoke
    transport: ingest
    stale_after_s: 300
    projects: [demo]
EOF
  printf '{"%s": "%s"}' "$MACHINE" "$RUNNER_SECRET" > "$WORK/credentials/runner-credentials.json"
  (cd "$WORK" && AGENT_FLEET_INGEST_TOKEN="$INGEST_TOKEN" \
    "$PY" hub/web.py --port 8799 --host 127.0.0.1 --dev-operator smoke@local \
    > hub.log 2>&1 &)
  # 找到 hub 进程 pid 供清理
  HUB_PID="$(pgrep -f 'hub/web.py --port 8799' | head -1 || true)"
  LOCAL=1
  ENDPOINT="http://127.0.0.1:8799"
  for i in $(seq 1 30); do
    curl -sf "$ENDPOINT/api/status" > /dev/null 2>&1 && break
    sleep 0.3
    [ "$i" = 30 ] && fail "hub 未就绪（见 $WORK/hub.log）"
  done
  info "本地 hub 已起: $ENDPOINT (machine=$MACHINE)"
else
  : "${AGENT_FLEET_INGEST_TOKEN:?需环境变量 AGENT_FLEET_INGEST_TOKEN}"
  : "${RUNNER_CREDENTIAL:?需环境变量 RUNNER_CREDENTIAL (<machine>:<secret>)}"
  INGEST_TOKEN="$AGENT_FLEET_INGEST_TOKEN"
  MACHINE="${RUNNER_CREDENTIAL%%:*}"
  RUNNER_SECRET="${RUNNER_CREDENTIAL#*:}"
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
PROJ="$WORK/proj"; mkdir -p "$PROJ"
git -C "$PROJ" init -q
git -C "$PROJ" -c user.email=s@s -c user.name=s commit -q --allow-empty -m init
printf '%s\n' "$RUNNER_SECRET" > "$WORK/runner-credential"
cat > "$WORK/runner.yaml" <<EOF
hub: $ENDPOINT
machine: $MACHINE
credential_file: $WORK/runner-credential
projects:
  demo:
    path: $PROJ
agents:
  codex:
    command: ["$PY", "-c", "import pathlib; pathlib.Path('hello.txt').write_text('hi'); print('done')"]
    timeout_s: 60
poll_interval_s: 1
heartbeat_interval_s: 1
cache_dir: $WORK/cache
EOF
"$PY" tools/agent-runner.py --config "$WORK/runner.yaml" --once || fail "runner 执行失败"
info "4. runner 单轮完成 ✓"

# 4) 断言终态与结果
TASK=$(curl -sf "$ENDPOINT/api/tasks/$TASK_ID")
STATE=$(printf '%s' "$TASK" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["task"]["state"])')
[ "$STATE" = "succeeded" ] || fail "任务状态 $STATE != succeeded"
printf '%s' "$TASK" | grep -q 'hello.txt' || fail "diff_stat 缺 hello.txt"
info "5. 任务 succeeded + diff 含 hello.txt ✓"

# 5) SSE 可达（curl 超时退出码 28 属预期，只校验收到的首帧）
SSE_OUT=$(curl -sN --max-time 2 "$ENDPOINT/api/stream" || true)
printf '%s' "$SSE_OUT" | head -1 | grep -q ': connected' \
  || fail "SSE 首帧缺失"
info "6. SSE 连接 ✓"

echo "SMOKE OK"
```

- [ ] **Step 2: 运行冒烟脚本**

Run: `bash deploy/e2e-smoke.sh`
Expected: 输出 `SMOKE OK`。若失败，按打印的失败点修实现（Phase 1-3），直到通过。**这是 Phase 4 的主要联调手段。**

- [ ] **Step 3: 提交**

```bash
chmod +x deploy/e2e-smoke.sh
git add deploy/e2e-smoke.sh
git commit -m "feat: v4 E2E 冒烟脚本 — ingest/建任务/幂等/runner/SSE 全链路"
```

---

### Task 2: hosts.yaml 项目白名单

**Files:**
- Modify: `hosts.yaml`

- [ ] **Step 1: 给每台 host 加 projects**

`hosts.yaml` 中每个 host 条目追加 `projects` 键（值按该机器实际登记的项目；当前只有 agent-fleet 一个登记项目）：

```yaml
  - name: hk
    desc: HK 容器（hub 自报告）
    transport: ingest
    stale_after_s: 300
    projects: [agent-fleet]
```

对其余 5 台（worker-a/worker-b/worker-c/worker-d/worker-e）同样追加 `projects: [agent-fleet]`；mac-local 等未登记机器在首次需要建任务时再登记。**没有 `projects` 键的 host 任务创建会被 400 拒绝**（安全默认）。

- [ ] **Step 2: 验证 + 提交**

Run: `.venv/bin/python -c "import yaml; yaml.safe_load(open('hosts.yaml'))" && .venv/bin/python -m unittest discover -s tests -v`
Expected: yaml 合法 + 测试全绿

```bash
git add hosts.yaml
git commit -m "feat: hosts.yaml 项目白名单 — 任务创建准入"
```

---

### Task 3: Cloudflare Access 配置文档

**Files:**
- Create: `deploy/cloudflare-access.md`
- Modify: `deploy/nginx-expose.md`（补一段衔接说明）

- [ ] **Step 1: 写 deploy/cloudflare-access.md**

完整内容：

````markdown
# Cloudflare Access — operator 认证配置（v4 控制面）

hub 的 `/api/tasks*` 与页面路由依赖 `Cf-Access-Authenticated-User-Email` 头确认 operator
身份。该头由 Cloudflare Access 边缘注入；nginx/容器不改动。

## 路径划分

| 路径 | 认证 | Access 策略 |
|---|---|---|
| `/`、`/machine/*`、`/task/*`、`/api/status`、`/api/machines/*`、`/api/events`、`/api/stream`、`/api/tasks*` | CF Access（operator） | **Include** 策略 |
| `/api/ingest`、`/api/scan` | X-Agent-Fleet-Token | **Bypass** 策略 |
| `/api/commands/*` | X-Runner-Credential | **Bypass** 策略 |

机器 probe/runner 不带浏览器 cookie，必须 bypass；它们有自己的凭据，bypass 不降低安全性。

## 配置步骤（Zero Trust Dashboard）

1. Zero Trust → Access → Applications → Add an application → Self-hosted。
2. Application domain: `hub.example.com`（与现有 CF 代理同域）。
3. 建两条 Policy（顺序：先 Bypass 后 Include，Access 按顺序匹配）：
   - Policy A「machine-credentials」：Action = **Bypass**，规则：
     `http.request.uri.path starts with "/api/ingest"` **or**
     `http.request.uri.path starts with "/api/scan"` **or**
     `http.request.uri.path starts with "/api/commands"`。
   - Policy B「operators」：Action = **Allow**，规则：Emails = 运维邮箱列表；
     Session duration 24h。
4. 保存后用浏览器访问 `https://hub.example.com/api/status`：应跳 CF 登录页，
   登录后返回 JSON。用 curl 无 cookie 访问同路径：应 302 到登录页（不是 200）。
5. 验证 bypass：`curl -X POST https://hub.example.com/api/commands/poll` 无 credential
   应返回 **403 JSON**（hub 的拒绝），而不是 CF 登录页 HTML。

## 安全注意

- Access 头只在经 CF 边缘的请求上可信。**源站必须只接受 CF IP**（现有 nginx 已仅监听
  容器内网 127.0.0.1，公网入口只有 CF 代理 ✅，保持现状）。
- 不要把 `--dev-operator` 带到生产启动参数。
- 吊销 operator：从 Policy B 移除邮箱即可，无需动 hub。
- runner credential 吊销：编辑 HK 容器 `credentials/runner-credentials.json` 删对应机器键，
  重启 hub（下次 poll 即 403）。

## 上线前 checklist

- [ ] Policy A/B 按上表生效（curl 验证通过）
- [ ] 生产 hub 启动命令不含 `--dev-operator`
- [ ] `credentials/runner-credentials.json` 0600 root 所有，只含已部署 runner 的机器
- [ ] hosts.yaml 目标机器已配 `projects` 白名单
````

- [ ] **Step 2: nginx-expose.md 末尾追加**

```markdown
## v4 控制面衔接（2026-08-19 起）

nginx 反代配置不变（443 → 8790）。operator 认证由 Cloudflare Access 在边缘完成，
配置见 `deploy/cloudflare-access.md`。注意 `/api/ingest`、`/api/scan`、`/api/commands/*`
必须在 Access 中 bypass，否则 probe/runner 会被登录页拦截。
```

- [ ] **Step 3: 提交**

```bash
git add deploy/cloudflare-access.md deploy/nginx-expose.md
git commit -m "docs: CF Access operator 认证配置与 bypass 规则"
```

---

### Task 4: HK 部署步骤 + 文档收口

**Files:**
- Modify: `docs/HANDOFF.md`、`README.md`

- [ ] **Step 1: HANDOFF.md 部署状态小节追加 v4 上线步骤**

在「四、部署状态」末尾追加：

```markdown
### v4 控制面上线步骤（2026-08-19）

1. HK 容器：`git pull` → `.venv/bin/pip install -r requirements.txt`（无新依赖，确认即可）
   → 重启 web 进程。启动时自动建 `state/fleet.db`（SQLite，WAL）。
2. HK 容器建 runner 凭据文件（0600）：`credentials/runner-credentials.json`，
   内容 `{"<machine>": "<openssl rand -hex 32 生成>"}`，每台 runner 机器一键。
3. Cloudflare 按 `deploy/cloudflare-access.md` 配 Access（Bypass + Allow 两条策略）。
4. 每台 agent 机器：`~/.config/agent-fleet/runner.yaml`（样例 `deploy/agent-runner.yaml.example`）
   + `~/.config/agent-fleet/runner-credential`（0600，与 hub 侧对应键同值）
   + cron `* * * * * ... tools/agent-runner.py --once`（或常驻进程）。
5. 验收：`bash deploy/e2e-smoke.sh`（本地）→ 生产浏览器建一个真实任务 →
   runner 机器确认 worktree 执行与结果回传 → 页面 `/task/<id>` 实时日志。
```

「六、未实现能力」中的「中心向 agent 下发控制命令：未实现」条目删除（已实现）；
保留 files API 未实现条目。

- [ ] **Step 2: README.md 架构图与「当前限制」更新**

架构图 runner 链路补进 ASCII（在 HK hub 框下加一行 runner 说明）；「当前限制」整节替换为：

```markdown
## 受控开发链路（v4）

浏览器 → CF Access → `POST /api/tasks` → SQLite 任务队列 → agent runner 主动 poll
→ 本地 worktree + adapter（codex/claude_code/hermes）→ 有界日志/diff 回传 → 页面实时展示。

- 任务创建要求目标机器在线且项目在 hosts.yaml `projects` 白名单内
- runner 只执行白名单项目 + 注册 adapter，无任意 shell 接口
- lease 300s + 30s 心跳；runner 崩溃任务自动回队列重派
- 结果幂等（attempt_id），网络失败本地缓存重传

未实现：`GET /api/tasks/<id>/files/<path>` 按需读文件（当前结果只含 diff 摘要与有界日志）。
```

- [ ] **Step 3: 提交**

```bash
git add docs/HANDOFF.md README.md
git commit -m "docs: v4 上线步骤与架构收口"
```

---

### Task 5: 最终全量验收

- [ ] **Step 1: 全量测试 + 语法检查 + 冒烟**

Run:
```bash
.venv/bin/python -m unittest discover -s tests -v
python3 -m compileall -q connectors hub tools tests
bash deploy/e2e-smoke.sh
```
Expected: 全部通过，最后输出 `SMOKE OK`

- [ ] **Step 2: 安全自查清单（逐项人工核对）**

- `grep -rn "secret\|token\|credential" hub/ tools/ --include="*.py" | grep -v "test" `：确认无硬编码凭据
- `git status`：确认 `credentials/`、`state/`、runner.yaml 未被跟踪
- `/api/status` 响应中无 sessions/session_id/display_name（既有回归测试覆盖）
- 生产启动无 token 时 `RuntimeError`（既有回归测试覆盖）
- `hub/` 全目录无 subprocess/ssh（`grep -rn "subprocess\|ssh" hub/`：应只有注释/文档字符串提到"不执行"）

- [ ] **Step 3: 提交收尾**

```bash
git commit --allow-empty -m "chore: v4 四阶段全部落地 — 观测加固+任务队列+runner+联调"
```

---

## Self-Review 记录

- **Spec 覆盖**：§8.3 四条 E2E 冒烟中「真实 probe POST 200」「真实 runner 执行回传」「SSE 实时性」由 Task 1 脚本覆盖（本地等价物）；「Lease 恢复（kill runner 300s 后回 queued）」由 Phase 2 单测覆盖（等不了 300s 的用例用注入 now 测）。CF Access（§4.1 auth 设计 + 用户确认项）✅ Task 3。部署脚本更新 ✅ Task 4。
- **无占位符**：冒烟脚本为完整可执行 bash；CF 文档为完整操作步骤；无 TBD。
- **边界诚实声明**：生产 CF Access 配置本身是控制台手工操作，本计划交付文档与验收命令，执行人需有 CF Zero Trust 权限。
