#!/usr/bin/env bash
# Source-only helpers for copying a release into a bind-mounted LIVE tree.
# Runtime state is intentionally outside release archives and must survive
# both normal cutovers and rollback.

FLEET_RELEASE_MANAGED_PATHS=(
    agent_profiles.py README.md requirements.txt report_schema.py
    session_schema.py platform_schema.py fleet-gates.conf RELEASE_ORIGIN frontend connectors hub tools deploy
    docs tests
)
FLEET_RUNTIME_PATHS=(credentials state var)
FLEET_RUNTIME_FILES=(hosts.yaml)
FLEET_RSYNC_RUNTIME_EXCLUDES=(
    --exclude=credentials/
    --exclude=state/
    --exclude=var/
    --exclude=hosts.yaml
    --exclude=.git/
    --exclude=.env
)

# Runtime trees are live while the hub writes SQLite WAL/SHM and JSONL files.
# A file can disappear between directory enumeration and cp(1), so retry the
# bounded snapshot a few times. Other copy errors still fail closed.
fleet_cp_tree_retry() {
    local source=$1 target=$2 attempts=0 rc=1
    while (( attempts < 5 )); do
        if cp -a "$source/." "$target/"; then
            return 0
        else
            rc=$?
        fi
        attempts=$((attempts + 1))
        sleep 0.2
    done
    return "$rc"
}

fleet_preserve_runtime_tree() {
    local source=$1 target=$2 rel
    for rel in "${FLEET_RUNTIME_PATHS[@]}"; do
        if [[ -d "$source/$rel" ]]; then
            mkdir -p "$target/$rel"
            fleet_cp_tree_retry "$source/$rel" "$target/$rel"
        fi
    done
    for rel in "${FLEET_RUNTIME_FILES[@]}"; do
        if [[ -f "$source/$rel" ]]; then
            cp -a "$source/$rel" "$target/$rel"
        fi
    done
}

fleet_ensure_runtime_tree() {
    local source=$1 target=$2 rel
    for rel in "${FLEET_RUNTIME_PATHS[@]}"; do
        if [[ -d "$source/$rel" && ! -e "$target/$rel" && ! -L "$target/$rel" ]]; then
            mkdir -p "$target/$rel"
            fleet_cp_tree_retry "$source/$rel" "$target/$rel"
        fi
    done
    for rel in "${FLEET_RUNTIME_FILES[@]}"; do
        if [[ -f "$source/$rel" && ! -e "$target/$rel" && ! -L "$target/$rel" ]]; then
            cp -a "$source/$rel" "$target/$rel"
        fi
    done
}

fleet_overlay_backup_tree() {
    local source=$1 backup=$2
    mkdir -p "$backup"
    if command -v rsync >/dev/null 2>&1; then
        rsync -a "$source/" "$backup/"
    else
        fleet_cp_tree_retry "$source" "$backup"
    fi
}

fleet_overlay_sync_tree() {
    local source=$1 target=$2 rel source_path target_path
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --delete "${FLEET_RSYNC_RUNTIME_EXCLUDES[@]}" "$source/" "$target/"
        return
    fi

    # Minimal-image fallback: replace only paths owned by the release.  This
    # has the same runtime-preservation contract without requiring rsync.
    for rel in "${FLEET_RELEASE_MANAGED_PATHS[@]}"; do
        source_path="$source/$rel"
        target_path="$target/$rel"
        if [[ -e "$target_path" || -L "$target_path" ]]; then
            rm -rf "$target_path"
        fi
        if [[ -e "$source_path" || -L "$source_path" ]]; then
            cp -a "$source_path" "$target_path"
        fi
    done
}

fleet_overlay_restore_tree() {
    local backup=$1 target=$2
    # Roll back release code only. The snapshot's runtime files are stale as
    # soon as a live writer commits, even before the Hub has been stopped.
    # Restoring data requires a separate, explicitly quiesced recovery flow.
    fleet_overlay_sync_tree "$backup" "$target"
}
