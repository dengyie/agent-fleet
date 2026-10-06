#!/usr/bin/env bash
set -Eeuo pipefail

here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=fleet-identity.sh
. "$here/fleet-identity.sh"
require_fleet_identity
require_fleet_live_mode
# shellcheck source=hk-overlay-sync.sh
. "$here/hk-overlay-sync.sh"

release_id=${1:?release id required}
release=${FLEET_HOME}/.hermes/agent-fleet-releases/$release_id
live=${FLEET_HOME}/agent-fleet
backup=${FLEET_HOME}/agent-fleet.previous-$release_id
pid_file=${FLEET_HOME}/.hermes/agent-fleet-web.pid
switched=0

[[ -d "$release" && -f "$release/hub/web.py" ]] || {
    echo "release missing: $release" >&2
    exit 2
}
[[ ! -e "$backup" ]] || {
    echo "backup already exists: $backup" >&2
    exit 2
}

q_release=$(printf '%q' "$release")
q_uid=$(printf '%q' "$(id -u "$FLEET_USER")")
pid=$(su -s /bin/bash -c "
    candidate=\"\"
    for proc_dir in /proc/[0-9]*; do
        [[ \$(cat \"\$proc_dir/comm\" 2>/dev/null || true) == python* ]] || continue
        [[ \$(readlink \"\$proc_dir/cwd\" 2>/dev/null || true) == $q_release ]] || continue
        [[ \$(awk '/^Uid:/{print \$2; exit}' \"\$proc_dir/status\" 2>/dev/null || true) == $q_uid ]] || continue
        cmdline=\"\$(tr '\\0' ' ' < \"\$proc_dir/cmdline\" 2>/dev/null || true)\"
        [[ \"\$cmdline\" == *hub/web.py* ]] || continue
        [[ \"\$cmdline\" == *--no-serve-frontend* ]] || continue
        if [[ -n \"\$candidate\" ]]; then
            exit 2
        fi
        candidate=\"\${proc_dir##*/}\"
    done
    [[ -n \"\$candidate\" ]] || exit 1
    printf '%s\\n' \"\$candidate\"
" "$FLEET_USER")

[[ -n "$pid" ]] || { echo "release process not found" >&2; exit 2; }
python3 -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8790/healthz", timeout=3).read()'

rollback_live() {
    case "$FLEET_LIVE_MODE" in
        symlink)
            rm -f "$live"
            mv "$backup" "$live"
            ;;
        overlay)
            fleet_overlay_restore_tree "$backup" "$live"
            ;;
    esac
    chown -R "$FLEET_USER:$FLEET_USER" "$live" 2>/dev/null || true
}

rollback() {
    local rc=$?
    if (( rc != 0 && switched == 1 )); then
        echo "ADOPT_FAILED rc=$rc; rolling back" >&2
        rollback_live || true
    fi
    exit "$rc"
}
trap rollback ERR

# Releases intentionally omit runtime data. Preserve the complete live set
# before either cutover mode replaces the runtime tree.
fleet_preserve_runtime_tree "$live" "$release"

case "$FLEET_LIVE_MODE" in
    symlink)
        [[ -d "$live" && ! -L "$live" ]] || {
            echo "symlink mode expects LIVE to be a real directory to rename: $live" >&2
            exit 2
        }
        mv "$live" "$backup"
        switched=1
        ln -s "$release" "$live"
        chown -h "$FLEET_USER:$FLEET_USER" "$live"
        ;;
    overlay)
        [[ -d "$live" && ! -L "$live" ]] || {
            echo "overlay mode requires LIVE to be a real directory (bind-mount): $live" >&2
            exit 2
        }
        fleet_overlay_backup_tree "$live" "$backup"
        switched=1
        fleet_overlay_sync_tree "$release" "$live"
        chown -R "$FLEET_USER:$FLEET_USER" "$live"
        ;;
esac

printf '%s\n' "$pid" > "$pid_file"
chown "$FLEET_USER:$FLEET_USER" "$pid_file"
rm -f \
    "${FLEET_HOME}/.hermes/agent-fleet-ingest-token.new" \
    "${FLEET_HOME}/.hermes/agent-fleet-release.tgz"
trap - ERR

echo "ADOPT_OK release=$release_id mode=$FLEET_LIVE_MODE pid=$pid backup=$backup"
