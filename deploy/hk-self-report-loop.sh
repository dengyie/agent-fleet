#!/usr/bin/env bash
# Agent Fleet Guardian - Health Check and Auto-Recovery
#
# ARCHITECTURE REQUIREMENT:
# This script MUST run inside the container namespace.
# Running from host namespace will cause health checks to fail because:
# - curl http://127.0.0.1:8790/ connects to host localhost, not container localhost
# - python3 hub/web.py cannot import container modules from host namespace
#
# Correct usage: run inside the hub container as FLEET_USER with
# FLEET_HOME set to that account's home. Do not default to the caller's HOME.
#
set -Eeuo pipefail

here=$(cd "$(dirname "$0")" && pwd -P)
# shellcheck source=fleet-identity.sh
. "$here/fleet-identity.sh"
require_fleet_home
export HOME=$FLEET_HOME

# Configuration
repo_root=$(cd "$(dirname "$0")/.." && pwd -P)
report_interval=${AGENT_FLEET_REPORT_INTERVAL:-120}
health_check_interval=${AGENT_FLEET_HEALTH_CHECK_INTERVAL:-20}
failure_threshold=${AGENT_FLEET_FAILURE_THRESHOLD:-3}
max_restart_failures=${AGENT_FLEET_MAX_RESTART_FAILURES:-5}
web_port=${AGENT_FLEET_WEB_PORT:-8790}
web_host=${AGENT_FLEET_WEB_HOST:-0.0.0.0}
machine_name=${AGENT_FLEET_MACHINE_NAME:-hub-host}

# Files — always FLEET_HOME, never a different caller's HOME
pid_file=${FLEET_HOME}/.hermes/agent-fleet-probe.pid
lock_dir=${pid_file}.lock
lock_file=${lock_dir}/flock
probe_lock_fd=""
probe_lock_owned=0
sleep_pid=""
web_pid_file=${FLEET_HOME}/.hermes/agent-fleet-web.pid
guardian_log=${FLEET_HOME}/.hermes/logs/agent-fleet-guardian.log
web_log=${FLEET_HOME}/.hermes/logs/agent-fleet-web.log
web_error_log=${FLEET_HOME}/.hermes/logs/agent-fleet-web-errors.log
mkdir -p "${FLEET_HOME}/.hermes/logs"
mkdir -p "$lock_dir"

# State
acquire_probe_lock() {
  command -v flock >/dev/null 2>&1 || {
    echo "flock is required for the probe singleton lock" >&2
    return 1
  }
  # The kernel owns this lock for the lifetime of the descriptor. Unlike a
  # mkdir/PID protocol, a second launch cannot mistake the short window before
  # the first process writes its PID for a stale owner.
  exec {probe_lock_fd}>"$lock_file"
  if ! flock -n "$probe_lock_fd"; then
    exec {probe_lock_fd}>&-
    exit 0
  fi
  probe_lock_owned=1
  printf '%s\n' "$$" > "$pid_file"
}

cleanup_probe_state() {
  if [[ "$sleep_pid" =~ ^[0-9]+$ ]]; then
    local stop_result stop_rc
    if stop_result=$(python3 "$repo_root/deploy/hk-web-process-control.py" \
      "$sleep_pid" "$repo_root" "$(id -u)" sleep "child:$$" 2>&1); then
      if [[ "$stop_result" != stopped && "$stop_result" != absent ]]; then
        log WARN "Unexpected sleep child stop result: $stop_result"
      fi
    else
      stop_rc=$?
      log WARN "Could not stop sleep child $sleep_pid (exit=$stop_rc): $stop_result"
    fi
  fi
  # An older process must never remove a newer process's PID file.
  if [[ -f "$pid_file" ]] && [[ "$(cat "$pid_file" 2>/dev/null || true)" == "$$" ]]; then
    rm -f "$pid_file"
  fi
  if (( probe_lock_owned == 1 )) && [[ -n "$probe_lock_fd" ]]; then
    flock -u "$probe_lock_fd" 2>/dev/null || true
    eval "exec ${probe_lock_fd}>&-" 2>/dev/null || true
  fi
}

acquire_probe_lock
trap cleanup_probe_state EXIT
trap 'exit 0' INT TERM

# Logging function
log() {
  local level="${1:-INFO}"
  shift
  echo "[$(date +"%Y-%m-%d %H:%M:%S")] [$level] $*" | tee -a "$guardian_log"
}

interruptible_sleep() {
  sleep "$1" &
  sleep_pid=$!
  wait "$sleep_pid" || true
  sleep_pid=""
}

# A PID file is advisory.  Verify the process identity before sending a
# signal, otherwise PID reuse can terminate an unrelated same-user process.
web_process_matches() {
  local pid=${1:-} expected_cwd=${2:-$repo_root} mode=${3:-api_only}
  local cmdline uid expected_uid
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  [[ -r "/proc/$pid/cmdline" && -r "/proc/$pid/status" ]] || return 1
  expected_uid=$(id -u)
  cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)
  uid=$(awk '/^Uid:/{print $2; exit}' "/proc/$pid/status" 2>/dev/null || true)
  [[ "$uid" == "$expected_uid" ]] || return 1
  [[ "$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)" == "$(readlink -f "$expected_cwd")" ]] || return 1
  [[ "$cmdline" == *"hub/web.py"* ]] || return 1
  [[ "$mode" == "allow_existing" || "$cmdline" == *"--no-serve-frontend"* ]]
}

# Health check function
health_check() {
  local status
  status=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://127.0.0.1:${web_port}/healthz" 2>/dev/null) || status="000"
  echo "$status"
}

# Restart web service function
stop_web_process() {
  local pid=$1 expected_cwd=${2:-$repo_root} result result_code expected_uid
  expected_uid=$(id -u)
  if result=$(python3 "$here/hk-web-process-control.py" "$pid" "$expected_cwd" "$expected_uid" web allow_existing 2>&1); then
    case "$result" in
      stopped) log INFO "Stopped old web process $pid" ;;
      absent) log INFO "Old web process $pid already exited" ;;
      *) log ERROR "Unexpected process-control result for PID $pid: $result"; return 1 ;;
    esac
    return 0
  else
    result_code=$?
  fi

  if (( result_code == 3 )); then
    log WARN "Ignoring stale or mismatched web PID $pid"
    return 2
  fi
  log ERROR "Failed to stop web process $pid: $result"
  return 1
}

restart_web() {
  log WARN "Restarting web service"

  # The pidfd helper binds validation and signaling to one process instance.
  if [[ -s "$web_pid_file" ]]; then
    local old_pid stop_result
    if ! old_pid=$(cat "$web_pid_file" 2>/dev/null); then
      log ERROR "Unable to read web PID file"
      return 1
    fi
    if [[ "$old_pid" =~ ^[0-9]+$ ]]; then
      if stop_web_process "$old_pid" "$repo_root"; then
        :
      else
        stop_result=$?
        (( stop_result == 2 )) || return 1
      fi
    else
      log WARN "Ignoring invalid web PID file"
    fi
  fi

  # Change to work directory
  if ! cd "$repo_root"; then
    log ERROR "Failed to cd to $repo_root"
    return 1
  fi

  # Start new process
  nohup python3 hub/web.py --host "$web_host" --port "$web_port" --no-serve-frontend \
    >> "$web_log" 2>> "$web_error_log" < /dev/null &
  local new_pid=$!
  echo "$new_pid" > "$web_pid_file"
  log INFO "Web service started: PID $new_pid"

  # Verify started (wait 2s then check)
  sleep 2
  if ! web_process_matches "$new_pid"; then
    log ERROR "Web process $new_pid died immediately after start"
    return 1
  fi

  return 0
}

# Main loop
log INFO "Self-report loop with guardian started (PID $$)"
log INFO "Config: health_check_interval=${health_check_interval}s, failure_threshold=${failure_threshold}, max_restart_failures=${max_restart_failures}"

last_report=0
failure_count=0
restart_failure_count=0

while true; do
  current_time=$(date +%s)

  # Health check
  status=$(health_check)
  if [[ "$status" == "200" ]]; then
    if (( failure_count > 0 )); then
      log INFO "Service recovered, health=$status"
    fi
    failure_count=0
  else
    failure_count=$((failure_count + 1))
    log WARN "Health check failed: $status (failure_count=$failure_count/$failure_threshold)"

    if (( failure_count >= failure_threshold )); then
      log ERROR "Health check failed $failure_count times, triggering restart"

      if restart_web; then
        log INFO "Restart successful"
        failure_count=0
        restart_failure_count=0
        interruptible_sleep 10  # Grace period after successful restart
      else
        restart_failure_count=$((restart_failure_count + 1))
        log ERROR "Restart failed (restart_failure_count=$restart_failure_count/$max_restart_failures)"

        if (( restart_failure_count >= max_restart_failures )); then
          log ERROR "FATAL: Exceeded max restart failures ($max_restart_failures), exiting"
          exit 1
        fi

        # Reset health check failure count to retry after next interval
        failure_count=0
        interruptible_sleep 30  # Longer wait after restart failure
      fi
    fi
  fi

  # Periodic report
  time_since_report=$((current_time - last_report))
  if (( time_since_report >= report_interval )); then
    if python3 "$repo_root/tools/agent-self-report.py" \
        --endpoint "http://127.0.0.1:${web_port}" \
        --name "$machine_name" \
        --token-file "$repo_root/credentials/ingest-token" >> "$guardian_log" 2>&1; then
      log INFO "Report sent successfully"
    else
      log WARN "Report failed"
    fi
    last_report=$current_time
  fi

  interruptible_sleep "$health_check_interval"
done
