#!/usr/bin/env bash
# deploy/test-release-routing.sh — 本地路由冒烟(Phase gate)
#
# 目标:验证 deploy/nginx-frontend-backend.example.conf 的「路由意图」:
#   - / 与 /assets/* 由 Nginx root 直接提供前端 release;
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

# --- 1. 静态路由意图: Nginx 直接提供 release ---
static_block="$(awk '/location \/assets\//,/^    }/' "$CONF")"
root_block="$(awk '/location \/ \{/,/^    }/' "$CONF")"
config_block="$(awk '/location = \/config\.js/,/^    }/' "$CONF")"
grep -q 'root /srv/agent-fleet/frontend/current;' "$CONF" || fail "缺少前端 release root"
for block in "$static_block" "$config_block"; do
  printf '%s\n' "$block" | grep -qF 'try_files $uri =404;' || fail "assets/config 路由缺少 404 边界"
done
printf '%s\n' "$root_block" | grep -qF 'try_files $uri $uri/ /index.html;' || fail "缺少 SPA 入口回退"
if printf '%s\n' "$static_block" "$config_block" "$root_block" | grep -q 'proxy_pass'; then
  fail "静态路由不应代理到后端"
fi
info "静态 /、/assets/*、/config.js 由 Nginx release 提供 ✓"

# --- 2. API 前缀涵盖 ingest、scan、commands、tasks、machines ---
api_block="$(awk '/location \/api\/ \{/,/^    }/' "$CONF")"
printf '%s\n' "$api_block" | grep -qF 'proxy_pass http://fleet_backend;' || fail "API 未指向 fleet_backend"
info "/api/* -> fleet_backend ✓"

# --- 3. 精确 SSE 路由禁止缓存/缓冲，保留长读超时 ---
sse_block="$(awk '/location = \/api\/stream/,/^    }/' "$CONF")"
for directive in 'proxy_pass http://fleet_backend;' 'proxy_buffering off;' 'proxy_cache off;' 'proxy_http_version 1.1;'; do
  printf '%s\n' "$sse_block" | grep -qF "$directive" || fail "SSE 缺少 $directive"
done
pcfg="$(printf '%s\n' "$sse_block" | sed -nE 's/^[[:space:]]*proxy_read_timeout ([0-9]+)s;.*/\1/p')"
[ -n "$pcfg" ] && [ "$pcfg" -ge 60 ] || fail "SSE 读超时缺失或小于 60 秒"
info "/api/stream: 禁用缓冲/缓存 + 长读超时 ✓"

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