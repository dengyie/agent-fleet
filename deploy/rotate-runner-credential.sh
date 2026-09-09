#!/usr/bin/env bash
# deploy/rotate-runner-credential.sh — runner 凭据轮换 / 吊销 SOP（默认不连生产）
#
# 用法:
#   bash deploy/rotate-runner-credential.sh <machine>
#   bash deploy/rotate-runner-credential.sh <machine> --revoke
#   bash deploy/rotate-runner-credential.sh <machine> --apply   # 仅写本地副本，仍不 SSH
#
# 默认只生成 secret 并打印人工步骤。--apply 才把 JSON 键写入
# ${FLEET_HOME:-$PWD}/credentials/runner-credentials.json（0600）。
# 禁止本脚本 SSH / scp / curl 生产。
set -euo pipefail

cd "$(dirname "$0")/.."

usage() {
  echo "usage: $0 <machine> [--revoke|--apply]" >&2
  exit 2
}

MACHINE="${1:-}"
MODE="print"
shift || true
while [ "$#" -gt 0 ]; do
  case "$1" in
    --revoke) MODE="revoke" ;;
    --apply) MODE="apply" ;;
    -h|--help) usage ;;
    *) usage ;;
  esac
  shift
done

[ -n "$MACHINE" ] || usage
if ! printf '%s' "$MACHINE" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'; then
  echo "invalid machine name: $MACHINE" >&2
  exit 2
fi

FLEET_HOME="${FLEET_HOME:-$PWD}"
CRED_DIR="$FLEET_HOME/credentials"
CRED_FILE="$CRED_DIR/runner-credentials.json"
AGENT_FILE='~/.config/agent-fleet/runner-credential'

if ! command -v openssl >/dev/null 2>&1; then
  echo "need openssl" >&2
  exit 1
fi

NEW_SECRET="$(openssl rand -hex 32)"

echo "=== runner credential SOP ==="
echo "machine: $MACHINE"
echo "hub file: credentials/runner-credentials.json"
echo "agent file: $AGENT_FILE (0600, 单行 secret)"
echo "mode: $MODE  (default print; --apply writes LOCAL copy only)"
echo

if [ "$MODE" = "revoke" ]; then
  cat <<EOF
吊销步骤（人工，不由本脚本连生产）：

1. 在 HK 容器编辑 credentials/runner-credentials.json，删除键 "$MACHINE"。
2. chmod 0600 该文件；TERM hub web，使下次 poll 读到新 JSON。
3. 该机 poll 应变 403。不要只删机器上的 runner-credential 而留 Hub 键。
4. 需要恢复时重新生成 secret，走轮换步骤而不是把旧值写回去。
EOF
  exit 0
fi

cat <<EOF
轮换步骤（旧 secret 在 Hub JSON 被替换后立即失效）：

1. 生成本次 secret（已生成，勿提交 git / 勿贴到 issue）：
   $NEW_SECRET

2. Hub（HK 容器，路径相对 LIVE 根）把 JSON 改成单 secret 映射，保持每机一键：
   {..., "$MACHINE": "<上面的 secret>"}
   chmod 0600 credentials/runner-credentials.json

3. 机器侧写入同一值（单行、无换行杂质）：
   umask 077
   printf '%s\n' '<secret>' > $AGENT_FILE
   chmod 0600 $AGENT_FILE

4. TERM / 重启该机 runner（cron --once 则等下一分钟）。Hub web 必须读到新 JSON
   后再让 runner 带着新 secret poll，否则会出现短暂 403。

5. 拨测：无凭据 poll=403；该机新凭据 poll=200/204；旧 secret 必须 403。

--apply 默认关。打开后只写本地 \$FLEET_HOME/credentials/，仍不 SSH。
EOF

if [ "$MODE" != "apply" ]; then
  exit 0
fi

mkdir -p "$CRED_DIR"
umask 077
python3 - "$CRED_FILE" "$MACHINE" "$NEW_SECRET" <<'PY'
import json, os, sys, tempfile
path, machine, secret = sys.argv[1], sys.argv[2], sys.argv[3]
data = {}
if os.path.exists(path):
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read().strip()
    if raw:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise SystemExit("credentials file is not a JSON object")
data[machine] = secret
directory = os.path.dirname(path) or "."
fd, tmp = tempfile.mkstemp(prefix="runner-credentials.", dir=directory)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
finally:
    if os.path.exists(tmp):
        os.remove(tmp)
print("wrote", path)
PY
chmod 0600 "$CRED_FILE"
echo "local apply done. copy the printed secret to the agent machine by hand."
