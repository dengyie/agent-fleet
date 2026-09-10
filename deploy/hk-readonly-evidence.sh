#!/usr/bin/env bash
# Bounded HK production evidence for recovery-plan Task 1.
# Status codes, listener ownership, process class, symlink targets, file
# existence, and a truncated <title>. Never dumps bodies, environ, argv
# secrets, credential files, or tokens.
#
# Run ON the HK host (or inside the production container) after an
# authorized login. Do not treat a laptop run as production fact.
# This workstation is not the hub host: no ${FLEET_HOME}/agent-fleet.
# Set FLEET_HOME and optionally HUB_PUBLIC_URL before running on the host.
set -Eeuo pipefail

if [[ -z "${FLEET_HOME:-}" ]]; then
    echo "set FLEET_HOME to the hub owner's home; laptop HOME is not production fact" >&2
    exit 2
fi
hub_public_url=${HUB_PUBLIC_URL:-https://hub.example.com}

redact() {
    sed -E 's/(token|password|secret|credential|passwd|authorization)[=:][^[:space:]]*/\1=REDACTED/Ig'
}

printf '=== status codes (no bodies) ===\n'
curl -sS -o /dev/null -w 'public=%{http_code}\n' "${hub_public_url}/api/status" || printf 'public=000\n'
curl -sS -o /dev/null -w 'loopback=%{http_code}\n' --connect-timeout 3 http://127.0.0.1:8790/api/status || printf 'loopback=000\n'

printf '=== nginx upstream (server_name from HUB_PUBLIC_URL host) ===\n'
if command -v nginx >/dev/null 2>&1; then
    nginx -T 2>/dev/null | grep -A8 -B2 "server_name ${hub_public_url#https://}" | redact || true
else
    printf 'nginx=absent\n'
fi

printf '=== :8790 listeners ===\n'
if command -v ss >/dev/null 2>&1; then
    ss -ltnp 2>/dev/null | sed -n '/:8790/p' | redact || true
else
    printf 'ss=absent\n'
fi

printf '=== process class (no argv) ===\n'
for proc_dir in /proc/[0-9]*; do
    comm=$(cat "$proc_dir/comm" 2>/dev/null || true)
    case "$comm" in
        python*|guardian*|hermes*)
            pid=${proc_dir##*/}
            cwd=$(readlink "$proc_dir/cwd" 2>/dev/null || true)
            exe=$(readlink "$proc_dir/exe" 2>/dev/null || true)
            printf 'pid=%s comm=%s cwd=%s exe=%s\n' "$pid" "$comm" "$cwd" "$exe"
            ;;
    esac
done

printf '=== LIVE / releases (existence + symlink, no contents) ===\n'
live=${FLEET_HOME}/agent-fleet
if [[ -e "$live" || -L "$live" ]]; then
    if command -v stat >/dev/null 2>&1; then
        stat -c 'LIVE path=%n type=%F mode=%A owner=%U:%G' "$live" 2>/dev/null \
            || stat -f 'LIVE path=%N type=%HT mode=%Sp owner=%Su:%Sg' "$live"
    fi
    printf 'LIVE_REAL=%s\n' "$(readlink -f "$live" 2>/dev/null || true)"
else
    printf 'LIVE=absent\n'
fi

releases=${FLEET_HOME}/.hermes/agent-fleet-releases
if [[ -d "$releases" ]]; then
    printf 'releases_dir=present\n'
    find "$releases" -maxdepth 2 -type f -name 'web.py' -print 2>/dev/null | sort || true
else
    printf 'releases_dir=absent\n'
fi

for marker in \
    ${FLEET_HOME}/.hermes/.f-diag-run \
    ${FLEET_HOME}/.hermes/guardian.sh.bak \
    ${FLEET_HOME}/agent-fleet/fleet-gates.conf
do
    if [[ -e "$marker" ]]; then
        printf 'exists=%s\n' "$marker"
    else
        printf 'missing=%s\n' "$marker"
    fi
done

printf '=== frontend title (bounded) ===\n'
curl -sS --connect-timeout 5 "${hub_public_url}/" \
    | tr '\n' ' ' \
    | grep -o '<title>[^<]*</title>' \
    | cut -c1-120 \
    || true

printf '=== done ===\n'
