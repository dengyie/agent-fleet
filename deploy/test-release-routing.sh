#!/usr/bin/env bash
# deploy/test-release-routing.sh — 本地路由冒烟(Phase gate)
#
# 目标:验证 deploy/nginx-frontend-backend.example.conf 的「路由意图」:
#   - / 与 /assets/* 走前端 release (fleet_frontend);
#   - /api/* (含 /api/stream、/api/ingest、/api/scan、/api/commands/*) 走后端
#     (fleet_backend);
#   - /api/stream 关闭 proxy_buffering 且长读超时(SSE);
#   - 不出现通配 CORS(Access-Control-Allow-Origin:*)。
#
# 约束:
#   - 只检查渲染出的示例配置文本 + 可选本地回环进程;绝不访问 Cloudflare、
#     生产 hub.example.com 或任何外部服务;
#   - 不依赖真实 Nginx/证书/凭据(示例配置用占位 upstream 与占位证书路径);
#   - trap 只清理本脚本自己启动的临时进程/文件。
set -euo pipefail
cd "$(dirname "$0")/.."

CONF="deploy/nginx-frontend-backend.example.conf"
[ -f "$CONF" ] || { echo "ROUTE FAIL: 缺少 $CONF" >&2; exit 1; }

fail() { echo "ROUTE FAIL: $*" >&2; exit 1; }
info() { echo "routing: $*"; }

# 用 grep -c 计数;set -e 下 0 匹配会让管道失败,故统一 || true 后判定。
have() { grep -c -- "$1" "$CONF" 2>/dev/null || true; }

# --- 1. 静态路由意图: / 和 /assets/* -> fleet_frontend ---
for loc in "location /assets/" "location /"; do
  [ "$(have "$loc")" -ge 1 ] || fail "缺少静态 location: $loc"
done
[ "$(have "proxy_pass http://fleet_frontend")" -ge 2 ] \
  || fail "前端 location 未指向 fleet_frontend"
info "静态 / 与 /assets/* -> fleet_frontend ✓"

# --- 2. API 路由意图: /api/* 与 probe/runner 路径 -> fleet_backend ---
for loc in "/api/stream" "/api/ingest" "/api/scan" "/api/commands/" "/api/tasks" "/api/machines" "/api/"; do
  [ "$(have "location ${loc}")" -ge 1 ] || fail "缺少 API location: ${loc}"
done
[ "$(have "proxy_pass http://fleet_backend")" -ge 6 ] \
  || fail "API location 未指向 fleet_backend"
info "/api/* (含 probe/runner) -> fleet_backend ✓"

# --- 3. SSE 契约: /api/stream 必须关闭 buffering 且长读超时 ---
sse_block="$(awk '/location \/api\/stream/,/^    }/' "$CONF")"
printf '%s\n' "$sse_block" | grep -q "proxy_buffering off" \
  || fail "/api/stream 未关闭 proxy_buffering"
printf '%s\n' "$sse_block" | grep -qE "proxy_read_timeout [0-9]+s" \
  || fail "/api/stream 未设置 proxy_read_timeout"
pcfg="$(printf '%s\n' "$sse_block" | grep -E "proxy_read_timeout" | grep -oE '[0-9]+' || true)"
if [ -n "${pcfg:-}" ] && [ "${pcfg:-0}" -lt 60 ]; then
  fail "/api/stream 读超时过短 ($pcfg s),SSE 要求长超时"
fi
info "/api/stream: proxy_buffering off + 长读超时 ✓"

# --- 4. 无通配 CORS(no-cors 规则) ---
if grep -qi "Access-Control-Allow-Origin" "$CONF"; then
  if grep -qiE "Access-Control-Allow-Origin[:[:space:]]*\*" "$CONF"; then
    fail "拒绝通配 CORS: Access-Control-Allow-Origin:* 出现在示例配置"
  fi
  info "含 Access-Control-Allow-Origin 但非通配(默许明示上游场景)✓"
else
  info "无任何 CORS 设置(同源,合法)✓"
fi

# --- 5. 无真实凭据/私钥泄入示例 ---
for token in 'BEGIN PRIVATE KEY' 'ingest-token' 'runner-credential' 'AGENT_FLEET_INGEST_TOKEN' \
             'postgres://' 'secret='; do
  if grep -qF -- "$token" "$CONF"; then
    fail "示例配置含秘密/真实凭据标记: $token"
  fi
done
info "示例配置不含真实凭据/私钥 ✓"

echo "ROUTE OK"