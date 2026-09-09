#!/usr/bin/env bash
set -Eeuo pipefail

release_id=${1:?release id required}
release=${HOME}/.hermes/agent-fleet-releases/$release_id
live=${HOME}/agent-fleet
backup=${HOME}/agent-fleet.previous-$release_id
pid_file=${HOME}/.hermes/agent-fleet-web.pid

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
' "${FLEET_USER:-fleet}")

[[ -n "$pid" ]] || { echo "release process not found" >&2; exit 2; }
python3 -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8790/api/status", timeout=3).read()'

mv "$live" "$backup"
ln -s "$release" "$live"
printf '%s\n' "$pid" > "$pid_file"
chown -h "${FLEET_USER:-fleet}:${FLEET_USER:-fleet}" "$live"
chown "${FLEET_USER:-fleet}:${FLEET_USER:-fleet}" "$pid_file"
rm -f \
    ${HOME}/.hermes/agent-fleet-ingest-token.new \
    ${HOME}/.hermes/agent-fleet-release.tgz

echo "ADOPT_OK release=$release_id pid=$pid backup=$backup"
