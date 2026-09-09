#!/usr/bin/env bash
# deploy/test-static-frontend.sh — 独立前端静态冒烟（Phase 3 gate）
#
# 目标：证明 frontend/ 是由一个纯标准库静态服务器（python3 -m http.server）就能
# 独立发布的 release，绝不依赖 Flask、绝不读取凭据，也不使用构建链。
#
# 覆盖：
#   - 用系统 python3 -m http.server 从仓库 frontend/ 起一个回环端口上的静态服务器；
#   - 轮询就绪（curl），取回全部要求的静态资源并断言 HTTP 200；
#   - 断言任何响应体都不含凭据 header 名（与测试套件的凭据红线一致）；
#   - trap 只清理本脚本自己启动的服务器 PID。
#
# 约束：不起 Flask、不访问外部服务、不用 Node/npm/构建产物、不做 git clean、
#       不删除 .playwright-mcp/。
set -euo pipefail
cd "$(dirname "$0")/.."

FRONTEND_DIR="$PWD/frontend"
CURL="${CURL:-curl}"

REQUIRED_PATHS=(
  index.html
  config.js
  routes.js
  api/client.js
  api/contracts.js
  realtime/sse.js
  state/store.js
  views/fleet.js
  views/machine.js
  views/task.js
  styles/app.css
)

# 与 tests/test_release_layout.py 相同的凭据红线：响应体不得含这些 header 名。
CREDENTIAL_HEADERS=(
  'X-Agent-Fleet-Token'
  'X-Runner-Credential'
)

fail() { echo "STATIC FAIL: $*" >&2; exit 1; }
info() { echo "static: $*"; }

[ -d "$FRONTEND_DIR" ] || fail "未找到 frontend/ 目录（$FRONTEND_DIR）"
[ -f "$FRONTEND_DIR/index.html" ] || fail "frontend/index.html 缺失"

# --- 动态选择回环端口：绑定 127.0.0.1:0 让内核分配空闲端口，再从 bound socket 读出 ---
PY="${PYTHON:-$(command -v python3 || command -v python)}"
[ -n "$PY" ] || fail "找不到 python3 用于 python3 -m http.server"
if ! "$PY" -m http.server --help >/dev/null 2>&1; then
  fail "python http.server 模块不可用（$PY）"
fi

PORT_LINE="$("$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print("READY_PORT", s.getsockname()[1]); s.close()')"
PORT="$(printf '%s\n' "$PORT_LINE" | awk '/READY_PORT/{print $2}')"
[ -n "${PORT:-}" ] || fail "无法动态分配回环端口（输出: $PORT_LINE）"
case "$PORT" in
  ''|*[!0-9]*) fail "动态端口非数字: $PORT" ;;
esac

BASE="http://127.0.0.1:$PORT"
BODY_TMP="$(mktemp "${TMPDIR:-/tmp}/static-frontend.XXXXXX")"

# --- 启动静态服务器；只清理本脚本启动的 $SERVER_PID 与临时文件 ---
SERVER_PID=""
cleanup() {
  rm -f "$BODY_TMP" 2>/dev/null || true
  if [ -n "$SERVER_PID" ]; then
    kill "$SERVER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

(
  cd "$FRONTEND_DIR"
  exec "$PY" -m http.server "$PORT" --bind 127.0.0.1
) >/dev/null 2>&1 &
SERVER_PID=$!

# --- 就绪轮询 ---
ready=0
for _ in {1..40}; do
  if "$CURL" -fsS --max-time 2 "$BASE/index.html" >"$BODY_TMP" 2>/dev/null; then
    ready=1
    break
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    fail "静态服务器提前退出（pid=$SERVER_PID）"
  fi
  sleep 0.25
done
[ "$ready" = 1 ] || fail "http.server 未就绪（curl 不达 $BASE）"
info "静态服务器就绪: $BASE (pid=$SERVER_PID)"

# --- 拉取全部必需路径，断言 HTTP 200 + 响应体无凭据 header 名 ---
for rel in "${REQUIRED_PATHS[@]}"; do
  code="$("$CURL" -s -o "$BODY_TMP" -w '%{http_code}' "$BASE/$rel")"
  [ "$code" = 200 ] || fail "$rel 返回 $code（期望 200）"
  for header in "${CREDENTIAL_HEADERS[@]}"; do
    if grep -qF "$header" "$BODY_TMP"; then
      fail "$rel 的响应体含凭据 header 名 '$header'"
    fi
  done
  info "200 OK, 无凭据: /$rel"
done

# --- 目录遍历防御：越界路径应 404（静态 shell 边界） ---
if [ "$("$CURL" -s -o /dev/null -w '%{http_code}' \
     "$BASE/../../../etc/passwd")" = 200 ]; then
  fail "路径遍历返回 200，应 404"
fi
info "路径遍历被拒绝 (404) ✓"

echo "STATIC OK (base=$BASE, pid=$SERVER_PID)"