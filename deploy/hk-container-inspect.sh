#!/usr/bin/env bash
set -Eeuo pipefail

if [[ -z "${FLEET_HOME:-}" ]]; then
    echo "set FLEET_HOME to the hub owner's home; do not default to the caller's HOME" >&2
    exit 2
fi

live=${FLEET_HOME}/agent-fleet
stat -c 'LIVE path=%n type=%F mode=%A owner=%U:%G' "$live"
printf 'LIVE_REAL=%s\n' "$(readlink -f "$live")"

for proc_dir in /proc/[0-9]*; do
    comm=$(cat "$proc_dir/comm" 2>/dev/null || true)
    case "$comm" in
        python*)
            pid=${proc_dir##*/}
            cwd=$(readlink "$proc_dir/cwd" 2>/dev/null || true)
            printf 'PYTHON pid=%s comm=%s cwd=%s\n' "$pid" "$comm" "$cwd"
            ;;
    esac
done

if command -v ss >/dev/null 2>&1; then
    ss -ltnp 2>/dev/null | sed -n '/:8790/p'
fi
