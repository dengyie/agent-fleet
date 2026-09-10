#!/usr/bin/env bash
set -Eeuo pipefail

if [[ -z "${FLEET_USER:-}" || -z "${FLEET_HOME:-}" ]]; then
    echo "set FLEET_USER and FLEET_HOME to the account that owns hub; do not default to a placeholder user or the caller's HOME" >&2
    exit 2
fi

archive=${1:-${FLEET_HOME}/.hermes/agent-fleet-release.tgz}
token_source=${2:-${FLEET_HOME}/.hermes/agent-fleet-ingest-token.new}
release_id=${3:-$(date -u +%Y%m%dT%H%M%SZ)}
live=${FLEET_HOME}/agent-fleet
release_root=${FLEET_HOME}/.hermes/agent-fleet-releases
release=$release_root/$release_id
backup=${FLEET_HOME}/agent-fleet.previous-$release_id
log_dir=${FLEET_HOME}/.hermes/logs
log_file=$log_dir/agent-fleet-web.log
pid_file=${FLEET_HOME}/.hermes/agent-fleet-web.pid
probe_pid_file=${FLEET_HOME}/.hermes/agent-fleet-probe.pid
probe_log_file=$log_dir/agent-fleet-probe.log

old_pid=""
new_pid=""
switched=0

hub_pid_for_cwd() {
    local target
    target=$(readlink -f "$1")
    su -s /bin/bash -c '
        for proc_dir in /proc/[0-9]*; do
            [[ $(cat "$proc_dir/comm" 2>/dev/null || true) == python* ]] || continue
            [[ $(readlink "$proc_dir/cwd" 2>/dev/null || true) == '"$target"' ]] || continue
            printf "%s\n" "${proc_dir##*/}"
            exit 0
        done
        exit 1
    ' "$FLEET_USER"
}

live_hub_pid() {
    hub_pid_for_cwd "$live"
}

start_hub() {
    local target=$1
    su -s /bin/bash -c \
        "cd '$target' && nohup python3 hub/web.py --host 0.0.0.0 --port 8790 >> '$log_file' 2>&1 < /dev/null &" \
        "$FLEET_USER"
    for _ in $(seq 1 20); do
        new_pid=$(hub_pid_for_cwd "$target" 2>/dev/null || true)
        [[ -n "$new_pid" ]] && break
        sleep 0.25
    done
    [[ -n "$new_pid" ]] || return 1
    printf '%s\n' "$new_pid" > "$pid_file"
    chown "$FLEET_USER:$FLEET_USER" "$pid_file"
}

stop_probe_loop() {
    [[ -s "$probe_pid_file" ]] || return 0
    local pid
    pid=$(cat "$probe_pid_file")
    if kill -0 "$pid" 2>/dev/null; then
        kill -TERM "$pid" 2>/dev/null || true
        for _ in $(seq 1 20); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.25
        done
    fi
    rm -f "$probe_pid_file"
}

start_probe_loop() {
    local target=$1
    su -s /bin/bash -c \
        "cd '$target' && nohup bash deploy/hk-self-report-loop.sh >> '$probe_log_file' 2>&1 < /dev/null &" \
        "$FLEET_USER"
    for _ in $(seq 1 20); do
        [[ -s "$probe_pid_file" ]] && return 0
        sleep 0.25
    done
    return 1
}

wait_for_status() {
    local attempts=30
    while (( attempts > 0 )); do
        if python3 -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8790/api/status", timeout=2).read()' \
            >/dev/null 2>&1; then
            return 0
        fi
        attempts=$((attempts - 1))
        sleep 1
    done
    return 1
}

rollback() {
    local rc=$?
    if (( rc == 0 )); then
        return
    fi
    echo "DEPLOY_FAILED rc=$rc; rolling back" >&2
    if [[ -n "$new_pid" ]] && kill -0 "$new_pid" 2>/dev/null; then
        kill -TERM "$new_pid" 2>/dev/null || true
    fi
    if (( switched == 1 )); then
        rm -f "$live"
        mv "$backup" "$live"
        chown -R "$FLEET_USER:$FLEET_USER" "$live"
        start_hub "$live"
        wait_for_status || true
        start_probe_loop "$live" || true
    fi
    exit "$rc"
}
trap rollback ERR

[[ -s "$archive" ]] || { echo "archive missing: $archive" >&2; exit 2; }
[[ -s "$token_source" ]] || { echo "token missing: $token_source" >&2; exit 2; }
[[ -d "$live" ]] || { echo "unexpected live path: $live" >&2; exit 2; }
[[ ! -e "$backup" && ! -e "$release" ]] || { echo "release already exists: $release_id" >&2; exit 2; }

mkdir -p "$release" "$log_dir"
tar -xzf "$archive" -C "$release"
[[ -f "$release/hub/web.py" && -f "$release/report_schema.py" ]] \
    || { echo "invalid release archive" >&2; exit 2; }

mkdir -p "$release/credentials"
install -m 600 "$token_source" "$release/credentials/ingest-token"
if [[ -d "$live/state" ]]; then
    cp -a "$live/state" "$release/state"
else
    mkdir -p "$release/state"
fi
chown -R "$FLEET_USER:$FLEET_USER" "$release" "$log_dir"

su -s /bin/bash -c \
    "cd '$release' && python3 -m compileall -q connectors hub tools report_schema.py && python3 -c 'import flask, yaml; from hub import web; web.make_app(require_token=True)'" \
    "$FLEET_USER"

old_pid=$(live_hub_pid || true)
[[ -n "$old_pid" ]] || { echo "no Python hub process with cwd $live" >&2; exit 2; }

mv "$live" "$backup"
ln -s "$release" "$live"
switched=1

stop_probe_loop
kill -TERM "$old_pid"
for _ in $(seq 1 20); do
    kill -0 "$old_pid" 2>/dev/null || break
    sleep 0.25
done
kill -0 "$old_pid" 2>/dev/null && kill -KILL "$old_pid"

start_hub "$live"
wait_for_status
start_probe_loop "$live"

rm -f "$token_source" "$archive"
trap - ERR

echo "DEPLOY_OK release=$release_id old_pid=$old_pid new_pid=$new_pid backup=$backup"
