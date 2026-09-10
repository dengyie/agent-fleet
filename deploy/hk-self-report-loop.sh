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

here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=fleet-identity.sh
. "$here/fleet-identity.sh"
require_fleet_home
export HOME=$FLEET_HOME

# Configuration
repo_root=$(cd "$(dirname "$0")/.." && pwd)
report_interval=${AGENT_FLEET_REPORT_INTERVAL:-120}
health_check_interval=${AGENT_FLEET_HEALTH_CHECK_INTERVAL:-20}
failure_threshold=${AGENT_FLEET_FAILURE_THRESHOLD:-3}
max_restart_failures=${AGENT_FLEET_MAX_RESTART_FAILURES:-5}
web_port=${AGENT_FLEET_WEB_PORT:-8790}
web_host=${AGENT_FLEET_WEB_HOST:-0.0.0.0}
machine_name=${AGENT_FLEET_MACHINE_NAME:-hub-host}

# Files — always FLEET_HOME, never a different caller's HOME
pid_file=${FLEET_HOME}/.hermes/agent-fleet-probe.pid
web_pid_file=${FLEET_HOME}/.hermes/agent-fleet-web.pid
guardian_log=${FLEET_HOME}/.hermes/logs/agent-fleet-guardian.log
web_log=${FLEET_HOME}/.hermes/logs/agent-fleet-web.log
web_error_log=${FLEET_HOME}/.hermes/logs/agent-fleet-web-errors.log
mkdir -p "${FLEET_HOME}/.hermes/logs"

# State
printf '%s\n' "$$" > "$pid_file"
trap 'rm -f "$pid_file"' EXIT INT TERM

# Logging function
log() {
  local level="${1:-INFO}"
  shift
  echo "[$(date +"%Y-%m-%d %H:%M:%S")] [$level] $*" | tee -a "$guardian_log"
}

# Health check function
health_check() {
  local status
  status=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://127.0.0.1:${web_port}/" 2>/dev/null) || status="000"
  echo "$status"
}

# Restart web service function
restart_web() {
  log WARN "Restarting web service"

  # Kill old process
  if [[ -s "$web_pid_file" ]]; then
    local old_pid=$(cat "$web_pid_file")
    if kill -0 "$old_pid" 2>/dev/null; then
      log INFO "Killing old web process $old_pid"
      kill "$old_pid" 2>/dev/null || true
      sleep 2

      # Force kill if still alive
      if kill -0 "$old_pid" 2>/dev/null; then
        log WARN "Process $old_pid still alive, sending SIGKILL"
        kill -9 "$old_pid" 2>/dev/null || true
        sleep 1
      fi

      # Verify killed
      if kill -0 "$old_pid" 2>/dev/null; then
        log ERROR "Failed to kill process $old_pid"
        return 1
      fi
    fi
  fi

  # Change to work directory
  if ! cd "$repo_root"; then
    log ERROR "Failed to cd to $repo_root"
    return 1
  fi

  # Start new process
  nohup python3 hub/web.py --host "$web_host" --port "$web_port" \
    >> "$web_log" 2>> "$web_error_log" < /dev/null &
  local new_pid=$!
  echo "$new_pid" > "$web_pid_file"
  log INFO "Web service started: PID $new_pid"

  # Verify started (wait 2s then check)
  sleep 2
  if ! kill -0 "$new_pid" 2>/dev/null; then
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
    ((failure_count++))
    log WARN "Health check failed: $status (failure_count=$failure_count/$failure_threshold)"

    if (( failure_count >= failure_threshold )); then
      log ERROR "Health check failed $failure_count times, triggering restart"

      if restart_web; then
        log INFO "Restart successful"
        failure_count=0
        restart_failure_count=0
        sleep 10  # Grace period after successful restart
      else
        ((restart_failure_count++))
        log ERROR "Restart failed (restart_failure_count=$restart_failure_count/$max_restart_failures)"

        if (( restart_failure_count >= max_restart_failures )); then
          log ERROR "FATAL: Exceeded max restart failures ($max_restart_failures), exiting"
          exit 1
        fi

        # Reset health check failure count to retry after next interval
        failure_count=0
        sleep 30  # Longer wait after restart failure
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

  sleep "$health_check_interval"
done
