# Shared hub-owner identity for deploy scripts. Source only; do not execute.
# FLEET_USER / FLEET_HOME are operator-supplied. Never default to a
# placeholder unix user or the caller's HOME.

_fleet_die() {
    echo "$*" >&2
    exit 2
}

_fleet_valid_user() {
    [[ "$1" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]]
}

_fleet_valid_home() {
    [[ "$1" == /* ]] || return 1
    case "$1" in
        *[[:space:]]*|*[\'\"\`\$\\\;\&\|\<\>\(\)]*) return 1 ;;
    esac
    return 0
}

require_fleet_home() {
    [[ -n "${FLEET_HOME:-}" ]] || _fleet_die "set FLEET_HOME to the hub owner's home; do not default to the caller's HOME"
    _fleet_valid_home "$FLEET_HOME" || _fleet_die "FLEET_HOME must be an absolute path with no whitespace or shell metacharacters"
}

require_fleet_user() {
    [[ -n "${FLEET_USER:-}" ]] || _fleet_die "set FLEET_USER to the account that owns hub; do not default to a placeholder user"
    _fleet_valid_user "$FLEET_USER" || _fleet_die "FLEET_USER must be a plain unix username"
    if command -v id >/dev/null 2>&1; then
        id -u "$FLEET_USER" >/dev/null 2>&1 || _fleet_die "FLEET_USER is not a local account: $FLEET_USER"
    fi
}

require_fleet_identity() {
    require_fleet_home
    require_fleet_user
}

require_fleet_live_mode() {
    case "${FLEET_LIVE_MODE:-}" in
        symlink|overlay) ;;
        *)
            _fleet_die "set FLEET_LIVE_MODE=symlink (retarget LIVE with ln -s) or overlay (bind-mount LIVE; this script will refuse ln -s)"
            ;;
    esac
}
