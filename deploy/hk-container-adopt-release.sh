#!/usr/bin/env bash
set -Eeuo pipefail

if [[ -z "${FLEET_USER:-}" || -z "${FLEET_HOME:-}" ]]; then
    echo "set FLEET_USER and FLEET_HOME to the account that owns hub; do not default to a placeholder user or the caller's HOME" >&2
    exit 2
fi

release_id=${1:?release id required}
release=${FLEET_HOME}/.hermes/agent-fleet-releases/$release_id
live=${FLEET_HOME}/agent-fleet
backup=${FLEET_HOME}/agent-fleet.previous-$release_id
pid_file=${FLEET_HOME}/.hermes/agent-fleet-web.pid

[[ -d "$release" && -f "$release/hub/web.py" ]] || {
    echo "release missing: $release" >&2
    exit 2
}
[[ -d "$live" && ! -L "$live" ]] || {
    echo "live is not the expected rollback directory: $live" >&2
    exit 2
}
[[ ! -e "$backup" ]] || {
    echo "backup already exists: $backup" >&2
    exit 2
}

pid=$(su -s /bin/bash -c '
    for proc_dir in /proc/[0-9]*; do
        [[ $(cat "$proc_dir/comm" 2>/dev/null || true) == python* ]] || continue
        [[ $(readlink "$proc_dir/cwd" 2>/dev/null || true) == '"$release"' ]] || continue
        printf "%s\n" "${proc_dir##*/}"
        exit 0
    done
    exit 1
' "$FLEET_USER")

[[ -n "$pid" ]] || { echo "release process not found" >&2; exit 2; }
python3 -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8790/api/status", timeout=3).read()'

mv "$live" "$backup"
ln -s "$release" "$live"
printf '%s\n' "$pid" > "$pid_file"
chown -h "$FLEET_USER:$FLEET_USER" "$live"
chown "$FLEET_USER:$FLEET_USER" "$pid_file"
rm -f \
    ${FLEET_HOME}/.hermes/agent-fleet-ingest-token.new \
    ${FLEET_HOME}/.hermes/agent-fleet-release.tgz

echo "ADOPT_OK release=$release_id pid=$pid backup=$backup"
