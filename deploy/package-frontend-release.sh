#!/usr/bin/env bash
# deploy/package-frontend-release.sh — 独立静态前端 release 打包（Task 19）
#
# 用法：
#   deploy/package-frontend-release.sh <output_dir> [release_version]
#
# 行为：
#   - 只打包仓库 frontend/ 目录下的静态 release 文件（HTML/CSS/JS/非秘密 config），
#     完整保留 frontend/ 内部的相对目录结构；不依赖构建系统，不访问外部网络。
#   - 在 <output_dir>/manifest.json 写入确定性 manifest，只含 release 版本与文件清单，
#     不含时间戳、随机值、哈希或任何秘密。
#   - release 版本必须由调用者显式提供（$2 或环境变量 FRONTEND_RELEASE_VERSION）。
#   - fail-closed：清单中任何文件命中下列红线即整体拒绝，且不产生任何输出文件：
#       credentials/               凭据目录
#       state/                     runtime 状态/数据库快照（frontend/state/store.js
#                                  本身是允许的静态模块，见下）
#       runner-credential          runner 凭据名
#       ingest-token               ingest 凭据名
#       .env / *.pem / *.key       环境变量文件/私钥/密钥
#   - 全程不访问外部网络、不访问生产端点；只使用标准库 shell 工具。
set -euo pipefail
set -eu

start_dir="$(pwd)"
repo_root="$(cd "$(dirname "$0")/.." && pwd)"

# frontend 源目录固定为仓库 frontend/；测试可用 FRONTEND_SRC 覆盖（不改变发布产物）。
frontend_dir="${FRONTEND_SRC:-$repo_root/frontend}"

output_dir="${1:-}"
release_version="${2:-${FRONTEND_RELEASE_VERSION:-}}"

usage() {
  echo "usage: $0 <output_dir> [release_version]  # release_version 亦可经 FRONTEND_RELEASE_VERSION 传入" >&2
}

# --- 参数守卫 ---
if [ -z "$output_dir" ]; then
  echo "error: output_dir required" >&2
  usage
  exit 2
fi
if [ -z "$release_version" ]; then
  echo "error: release version required (参数 2 或 FRONTEND_RELEASE_VERSION)" >&2
  usage
  exit 2
fi
# manifest.json 直接嵌入该版本号：仅允许保守字符集，保证生成的 JSON 无注入。
case "$release_version" in
  *[!A-Za-z0-9._-]*)
    echo "error: release_version 含非法字符: $release_version" >&2
    exit 2
    ;;
esac

case "$output_dir" in
  /*) ;;
  *) output_dir="$start_dir/$output_dir" ;;
esac
# 拒绝把输出目录放在 frontend 源目录内（否则复制会自我递归）。
case "$output_dir" in
  "$frontend_dir"|"$frontend_dir"/*)
    echo "error: 拒绝把输出写入 frontend 源目录: $output_dir" >&2
    exit 2
    ;;
esac

# --- 源目录守卫 ---
if [ ! -d "$frontend_dir" ]; then
  echo "error: 未找到 frontend 目录: $frontend_dir" >&2
  exit 2
fi
if [ ! -f "$frontend_dir/index.html" ]; then
  echo "error: frontend/index.html 缺失，不是完整 release 源" >&2
  exit 2
fi

# --- 红线扫描器（fail-closed）---
# 命中以下任何一条即认为该相对路径禁止进入 release（函数返回 0 = 禁止；1 = 允许）：
#   * 任一路径成分等于 credentials，或包含 runner-credential / ingest-token；
#   * 任一成分形如 .env，或以 .pem / .key 结尾；
#   * 落在 state/ 目录下的非静态模块文件（*.js / *.css 之外的状态/数据库快照）；
#   * 任何位置出现 *.db / *.sqlite / *.jsonl / *.pem / *.key 文件。
path_is_forbidden() {
  local rel="$1"
  local base="${rel##*/}"
  local comp

  # 逐路径成分检查凭据与密钥形态
  IFS='/'
  for comp in $rel; do
    case "$comp" in
      credentials)                 return 0 ;;
      *runner-credential*)         return 0 ;;
      *ingest-token*)              return 0 ;;
      .env|.env.*|*.env)           return 0 ;;
      *.pem|*.key)                 return 0 ;;
    esac
  done

  # state/ 目录只允许静态模块文件（.js / .css）；其余视为状态/数据库快照拒绝。
  case "$rel" in
    *state*)
      case "$base" in
        *.js|*.css) ;;   # 例如 state/store.js —— 允许的静态模块
        *) return 0 ;;   # 例如 state/fleet.db、state/current.json —— 拒绝
      esac
      ;;
  esac

  # 兜底的数据库/凭据/状态文件名（任何目录层级）
  case "$base" in
    *.db|*.db-*|*.sqlite|*.sqlite3|*.jsonl|state.json|current.json|*.pem|*.key)
      return 0 ;;
  esac
  return 1
}

# --- 枚举文件清单（稳定排序，保证确定性）---
cd "$frontend_dir"
files=()
while IFS= read -r rel; do
  [ "$rel" = "./.DS_Store" ] && continue
  files+=("${rel#./}")
done < <(find . -type f -print | sort)

if [ "${#files[@]}" -eq 0 ]; then
  echo "error: frontend/ 下没有可打包文件" >&2
  exit 2
fi

# --- fail-closed 红名单扫描：任何文件命中即整体拒绝 ---
rejected=0
for rel in "${files[@]}"; do
  if path_is_forbidden "$rel"; then
    echo "forbidden path in frontend release: $rel" >&2
    rejected=1
  fi
done
if [ "$rejected" -ne 0 ]; then
  echo "error: 打包被拒绝（含禁止进入 release 的路径）" >&2
  exit 1
fi

# --- 落盘：复制所有文件，然后写确定性 manifest.json ---
mkdir -p "$output_dir"
for rel in "${files[@]}"; do
  mkdir -p "$output_dir/$(dirname "$rel")"
  cp "$frontend_dir/$rel" "$output_dir/$rel"
done

manifest="$output_dir/manifest.json"
{
  printf '{ "version": "%s", "files": [' "$release_version"
  n="${#files[@]}"
  i=1
  for rel in "${files[@]}"; do
    if [ "$i" -eq "$n" ]; then
      printf '"%s"] }' "$rel"
    else
      printf '"%s", ' "$rel"
    fi
    i=$((i + 1))
  done
  printf '\n'
} >"$manifest"

echo "frontend release packaged: $output_dir (version=$release_version, files=${#files[@]})"