#!/usr/bin/env bash
set -Eeuo pipefail

here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=fleet-identity.sh
. "$here/fleet-identity.sh"
require_fleet_identity
require_fleet_live_mode
# shellcheck source=hk-overlay-sync.sh
. "$here/hk-overlay-sync.sh"

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
web_host=${AGENT_FLEET_WEB_HOST:-0.0.0.0}
web_port=${AGENT_FLEET_WEB_PORT:-8790}

old_argv=()
old_pid=""
new_pid=""
probe_cwd=""
switched=0
old_stopped=0
probe_stopped=0

hub_pid_for_cwd() {
    local target q_target q_uid require_api_only q_require_api_only
    require_api_only=1
    [[ "${2:-api_only}" == "allow_existing" ]] && require_api_only=0
    target=$(readlink -f "$1")
    q_target=$(printf '%q' "$target")
    q_uid=$(printf '%q' "$(id -u "$FLEET_USER")")
    q_require_api_only=$(printf '%q' "$require_api_only")
    su -s /bin/bash -c "
        candidate=\"\"
        for proc_dir in /proc/[0-9]*; do
            [[ \$(cat \"\$proc_dir/comm\" 2>/dev/null || true) == python* ]] || continue
            [[ \$(readlink \"\$proc_dir/cwd\" 2>/dev/null || true) == $q_target ]] || continue
            [[ \$(awk '/^Uid:/{print \$2; exit}' \"\$proc_dir/status\" 2>/dev/null || true) == $q_uid ]] || continue
            cmdline=\"\$(tr '\\0' ' ' < \"\$proc_dir/cmdline\" 2>/dev/null || true)\"
            [[ \"\$cmdline\" == *hub/web.py* ]] || continue
            [[ $q_require_api_only == 0 || \"\$cmdline\" == *--no-serve-frontend* ]] || continue
            if [[ -n \"\$candidate\" ]]; then
                exit 2
            fi
            candidate=\"\${proc_dir##*/}\"
        done
        [[ -n \"\$candidate\" ]] || exit 1
        printf '%s\\n' \"\$candidate\"
    " "$FLEET_USER"
}

live_hub_pid() {
    # Existing release may have a different launch mode; keep uid/cwd checks.
    hub_pid_for_cwd "$live" allow_existing
}

start_hub() {
    local target=$1 q_target q_log q_host q_port q_command launch_mode=api_only
    local launch_argv=(python3 hub/web.py --host "$web_host" --port "$web_port" --no-serve-frontend)
    if [[ "${2:-}" == restore ]]; then
        (( ${#old_argv[@]} > 0 )) || return 1
        launch_argv=("${old_argv[@]}")
        launch_mode=allow_existing
    fi
    printf -v q_command '%q ' "${launch_argv[@]}"
    q_target=$(printf '%q' "$target")
    q_log=$(printf '%q' "$log_file")
    q_host=$(printf '%q' "$web_host")
    q_port=$(printf '%q' "$web_port")
    su -s /bin/bash -c \
        "cd $q_target && nohup $q_command >> $q_log 2>&1 < /dev/null &" \
        "$FLEET_USER"
    for _ in $(seq 1 20); do
        new_pid=$(hub_pid_for_cwd "$target" "$launch_mode" 2>/dev/null || true)
        [[ -n "$new_pid" ]] && break
        sleep 0.25
    done
    [[ -n "$new_pid" ]] || return 1
    printf '%s\n' "$new_pid" > "$pid_file"
    chown "$FLEET_USER:$FLEET_USER" "$pid_file"
}

probe_process_matches() {
    local probe_pid=${1:-} expected_cwd=${2:-$live}
    local cmdline uid expected_uid actual_cwd expected_root q_probe_pid
    [[ "$probe_pid" =~ ^[0-9]+$ ]] || return 1
    [[ -r "/proc/$probe_pid/cmdline" && -r "/proc/$probe_pid/status" ]] || return 1
    expected_uid=$(id -u "$FLEET_USER")
    uid=$(awk '/^Uid:/{print $2; exit}' "/proc/$probe_pid/status" 2>/dev/null || true)
    [[ "$uid" == "$expected_uid" ]] || return 1
    # A live process can briefly make ``readlink -f /proc/$pid/cwd`` fail
    # while a bind-mounted tree is being replaced.  Read the proc target
    # directly and canonicalize only the known target path; otherwise a
    # healthy old probe is missed and each retry adds another loop.
    expected_root=$(readlink -f "$expected_cwd" 2>/dev/null || true)
    # The installer runs as root, but this container hides other users' proc
    # links from root unless it has ptrace capability. Read the link through
    # the target account instead; otherwise every existing probe is missed and
    # each deployment retry starts another loop.
    q_probe_pid=$(printf '%q' "$probe_pid")
    actual_cwd=$(su -s /bin/bash -c "readlink /proc/$q_probe_pid/cwd 2>/dev/null || true" "$FLEET_USER")
    [[ -n "$expected_root" && "$actual_cwd" == "$expected_root" ]] || return 1
    cmdline=$(tr '\0' ' ' < "/proc/$probe_pid/cmdline" 2>/dev/null || true)
    [[ "$cmdline" == *"deploy/hk-self-report-loop.sh"* ]]
}

stop_probe_loop() {
    local expected_cwd=${1:-$live}
    local pids=() pid proc_dir alive existing
    append_probe_pid() {
        local candidate=$1
        for existing in "${pids[@]}"; do
            [[ "$existing" == "$candidate" ]] && return 0
        done
        pids+=("$candidate")
    }
    # The PID file is advisory: an older loop can remove it from its EXIT
    # trap after a replacement loop has already written a new PID. Scan the
    # process table as well so a failed stop can never leave duplicate probes.
    if [[ -s "$probe_pid_file" ]]; then
        pid=$(cat "$probe_pid_file" 2>/dev/null || true)
        probe_process_matches "$pid" "$expected_cwd" && append_probe_pid "$pid"
    fi
    for proc_dir in /proc/[0-9]*; do
        pid=${proc_dir##*/}
        probe_process_matches "$pid" "$expected_cwd" || continue
        append_probe_pid "$pid"
    done
    for pid in "${pids[@]}"; do
        probe_process_matches "$pid" "$expected_cwd" || continue
        kill -0 "$pid" 2>/dev/null && kill -TERM "$pid" 2>/dev/null || true
    done
    for _ in $(seq 1 20); do
        alive=0
        for pid in "${pids[@]}"; do
            if kill -0 "$pid" 2>/dev/null; then
                alive=1
                break
            fi
        done
        if (( alive == 0 )); then
            break
        fi
        sleep 0.25
    done
    # A guardian may be blocked in a child sleep or network call. Once the
    # bounded grace period expires, kill the exact matching PIDs before a new
    # loop starts; otherwise both loops race on the shared PID file.
    for pid in "${pids[@]}"; do
        probe_process_matches "$pid" "$expected_cwd" || continue
        kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null || true
    done
    rm -f "$probe_pid_file"
}

start_probe_loop() {
    local target=$1 q_target q_log q_home q_user
    q_target=$(printf '%q' "$target")
    q_log=$(printf '%q' "$probe_log_file")
    q_home=$(printf '%q' "$FLEET_HOME")
    q_user=$(printf '%q' "$FLEET_USER")
    # A stale PID file must not make a failed launch look successful.  The
    # loop owns the file and singleton lock; this wait only accepts an exact
    # live process identity.
    rm -f "$probe_pid_file"
    su -s /bin/bash -c \
        "export FLEET_HOME=$q_home FLEET_USER=$q_user HOME=$q_home; cd $q_target && nohup bash deploy/hk-self-report-loop.sh >> $q_log 2>&1 < /dev/null &" \
        "$FLEET_USER"
    for _ in $(seq 1 20); do
        if [[ -s "$probe_pid_file" ]]; then
            local probe_pid
            probe_pid=$(cat "$probe_pid_file" 2>/dev/null || true)
            if probe_process_matches "$probe_pid" "$target"; then
                return 0
            fi
            rm -f "$probe_pid_file"
        fi
        sleep 0.25
    done
    return 1
}

wait_for_status() {
    local attempts=30
    while (( attempts > 0 )); do
        if python3 -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:'"$web_port"'/api/status", timeout=2).read()' \
            >/dev/null 2>&1; then
            return 0
        fi
        attempts=$((attempts - 1))
        sleep 1
    done
    return 1
}

switch_live() {
    case "$FLEET_LIVE_MODE" in
        symlink)
            [[ -e "$live" ]] || { echo "unexpected live path: $live" >&2; exit 2; }
            mv "$live" "$backup"
            switched=1
            ln -s "$release" "$live"
            chown -h "$FLEET_USER:$FLEET_USER" "$live"
            ;;
        overlay)
            [[ -d "$live" && ! -L "$live" ]] || {
                echo "overlay mode requires LIVE to be a real directory (bind-mount), not a symlink: $live" >&2
                exit 2
            }
            fleet_ensure_runtime_tree "$release" "$live"
            fleet_overlay_backup_tree "$live" "$backup"
            # Mark the cutover recoverable before the first target mutation.
            # If a copy fails halfway through, ERR must restore the snapshot.
            switched=1
            fleet_overlay_sync_tree "$release" "$live"
            chown -R "$FLEET_USER:$FLEET_USER" "$live"
            ;;
    esac
}

rollback_live() {
    case "$FLEET_LIVE_MODE" in
        symlink)
            rm -f "$live"
            mv "$backup" "$live"
            chown -h "$FLEET_USER:$FLEET_USER" "$live" 2>/dev/null || chown -R "$FLEET_USER:$FLEET_USER" "$live"
            ;;
        overlay)
            fleet_overlay_restore_tree "$backup" "$live"
            chown -R "$FLEET_USER:$FLEET_USER" "$live"
            ;;
    esac
}

rollback() {
    local rc=$?
    if (( rc == 0 )); then
        return
    fi
    echo "DEPLOY_FAILED rc=$rc; rolling back" >&2
    if [[ -n "$new_pid" ]] && kill -0 "$new_pid" 2>/dev/null; then
        kill -TERM "$new_pid" 2>/dev/null || true
        for _ in $(seq 1 20); do
            kill -0 "$new_pid" 2>/dev/null || break
            sleep 0.25
        done
        kill -0 "$new_pid" 2>/dev/null && kill -KILL "$new_pid" 2>/dev/null || true
    fi
    if (( switched == 1 )); then
        rollback_live
        # A cutover can fail before the old process is stopped (for example
        # if the rsync-free copy fails halfway through).  Do not start a
        # second listener while that process is still serving the restored
        # tree; only recreate it after the old process is gone.
        if (( old_stopped == 1 )) || ! kill -0 "$old_pid" 2>/dev/null; then
            start_hub "$live" restore
            wait_for_status || echo "ROLLBACK_UNHEALTHY" >&2
        fi
        if (( probe_stopped == 1 )); then
            start_probe_loop "$live" || true
        fi
    fi
    exit "$rc"
}
trap rollback ERR

[[ -s "$archive" ]] || { echo "archive missing: $archive" >&2; exit 2; }
[[ -s "$token_source" ]] || { echo "token missing: $token_source" >&2; exit 2; }
[[ -d "$live" || -L "$live" ]] || { echo "unexpected live path: $live" >&2; exit 2; }
[[ ! -e "$backup" && ! -e "$release" ]] || { echo "release already exists: $release_id" >&2; exit 2; }

prepare_runtime_dir() {
    local dir="$1" parent
    if (( EUID == 0 )); then
        install -d -o "$FLEET_USER" -g "$FLEET_USER" -m 755 "$dir"
        return
    fi
    if [[ -e "$dir" ]]; then
        [[ -d "$dir" && -w "$dir" ]] || {
            echo "DEPLOY_PREFLIGHT_FAILED code=runtime_dir_not_writable path=$dir; run provisioning as root or grant $FLEET_USER ownership" >&2
            exit 2
        }
        return
    fi
    parent=$(dirname "$dir")
    [[ -w "$parent" ]] || {
        echo "DEPLOY_PREFLIGHT_FAILED code=runtime_parent_not_writable path=$parent; run provisioning as root or grant $FLEET_USER ownership" >&2
        exit 2
    }
    mkdir -p "$dir"
}

prepare_runtime_dir "$release_root"
prepare_runtime_dir "$log_dir"
mkdir -p "$release"
tar -xzf "$archive" -C "$release"
[[ -f "$release/hub/web.py" && -f "$release/report_schema.py" ]] \
    || { echo "invalid release archive" >&2; exit 2; }

mkdir -p "$release/credentials"
# Runtime credentials are deliberately absent from release archives.  Carry the
# existing credential and runtime set into the candidate release before
# replacing only the refreshed ingest token; otherwise overlay --delete (or a
# symlink cutover) would silently revoke operator, runner, and supervisor
# authentication.  The refresh must happen *after* preservation so the old
# live token cannot overwrite the newly staged token.
fleet_preserve_runtime_tree "$live" "$release"
install -m 600 "$token_source" "$release/credentials/ingest-token"
mkdir -p "$release/state"
if (( EUID == 0 )); then
    chown -R "$FLEET_USER:$FLEET_USER" "$release" "$log_dir"
fi

q_release=$(printf '%q' "$release")
su -s /bin/bash -c \
    "cd $q_release && python3 -m compileall -q connectors hub tools report_schema.py && python3 -c 'import flask, yaml; from hub import web; web.make_app(require_token=True)'" \
    "$FLEET_USER"

old_pid=$(live_hub_pid || true)
[[ -n "$old_pid" ]] || { echo "no Python hub process with cwd $live" >&2; exit 2; }
# Capture NUL-separated argv before modifying the live tree. Shell quoting in
# start_hub preserves spaces and prevents arguments from becoming shell code.
while IFS= read -r -d '' arg; do old_argv+=("$arg"); done < <(
    su -s /bin/bash -c "cat /proc/$old_pid/cmdline" "$FLEET_USER"
)
(( ${#old_argv[@]} > 0 )) || { echo "cannot capture previous hub argv" >&2; exit 2; }
probe_cwd=$(readlink -f "$live")
[[ -n "$probe_cwd" ]] || { echo "cannot resolve probe cwd from $live" >&2; exit 2; }

switch_live

stop_probe_loop "$probe_cwd"
probe_stopped=1
kill -TERM "$old_pid"
for _ in $(seq 1 20); do
    kill -0 "$old_pid" 2>/dev/null || break
    sleep 0.25
done
kill -0 "$old_pid" 2>/dev/null && kill -KILL "$old_pid"
old_stopped=1

start_hub "$live"
wait_for_status
start_probe_loop "$live"

cleanup_token_source() {
    local source_real active_real
    source_real=$(readlink -f "$token_source" 2>/dev/null || true)
    active_real=$(readlink -f "$live/credentials/ingest-token" 2>/dev/null || true)
    # The operator may pass the active live token as the source.  Never delete
    # it after cutover; only remove a distinct staging copy.
    if [[ -n "$source_real" && "$source_real" == "$active_real" ]]; then
        return 0
    fi
    rm -f "$token_source"
}
cleanup_token_source
rm -f "$archive"
trap - ERR

echo "DEPLOY_OK release=$release_id mode=$FLEET_LIVE_MODE old_pid=$old_pid new_pid=$new_pid backup=$backup"
