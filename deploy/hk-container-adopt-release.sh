#!/usr/bin/env bash
set -Eeuo pipefail

here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=fleet-identity.sh
. "$here/fleet-identity.sh"
require_fleet_identity
require_fleet_live_mode

release_id=${1:?release id required}
release=${FLEET_HOME}/.hermes/agent-fleet-releases/$release_id
live=${FLEET_HOME}/agent-fleet
backup=${FLEET_HOME}/agent-fleet.previous-$release_id
pid_file=${FLEET_HOME}/.hermes/agent-fleet-web.pid

[[ -d "$release" && -f "$release/hub/web.py" ]] || {
    echo "release missing: $release" >&2
    exit 2
}
[[ ! -e "$backup" ]] || {
    echo "backup already exists: $backup" >&2
    exit 2
}

q_release=$(printf '%q' "$release")
pid=$(su -s /bin/bash -c "
    for proc_dir in /proc/[0-9]*; do
        [[ \$(cat \"\$proc_dir/comm\" 2>/dev/null || true) == python* ]] || continue
        [[ \$(readlink \"\$proc_dir/cwd\" 2>/dev/null || true) == $q_release ]] || continue
        printf '%s\\n' \"\${proc_dir##*/}\"
        exit 0
    done
    exit 1
" "$FLEET_USER")

[[ -n "$pid" ]] || { echo "release process not found" >&2; exit 2; }
python3 -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8790/api/status", timeout=3).read()'

case "$FLEET_LIVE_MODE" in
    symlink)
        [[ -d "$live" && ! -L "$live" ]] || {
            echo "symlink mode expects LIVE to be a real directory to rename: $live" >&2
            exit 2
        }
        mv "$live" "$backup"
        ln -s "$release" "$live"
        chown -h "$FLEET_USER:$FLEET_USER" "$live"
        ;;
    overlay)
        [[ -d "$live" && ! -L "$live" ]] || {
            echo "overlay mode requires LIVE to be a real directory (bind-mount): $live" >&2
            exit 2
        }
        command -v rsync >/dev/null 2>&1 || { echo "overlay mode needs rsync" >&2; exit 2; }
        mkdir -p "$backup"
        rsync -a --delete "$live/" "$backup/"
        rsync -a --delete "$release/" "$live/"
        chown -R "$FLEET_USER:$FLEET_USER" "$live"
        ;;
esac

printf '%s\n' "$pid" > "$pid_file"
chown "$FLEET_USER:$FLEET_USER" "$pid_file"
rm -f \
    "${FLEET_HOME}/.hermes/agent-fleet-ingest-token.new" \
    "${FLEET_HOME}/.hermes/agent-fleet-release.tgz"

echo "ADOPT_OK release=$release_id mode=$FLEET_LIVE_MODE pid=$pid backup=$backup"
