#!/usr/bin/env bash
set -Eeuo pipefail

output=${1:-/tmp/agent-fleet-release.tgz}
origin=${2:-}
repo_root=$(cd "$(dirname "$0")/.." && pwd)

cd "$repo_root"
# Pack tracked files only. A working-tree tar would ship local leftovers
# such as tests/__tmp_app_context/state/. git archive never includes
# gitignored runtime stores, credentials, or untracked junk.
# hosts.yaml stays tracked as a local example, but MUST NOT ship in the
# CI artifact: overlaying it onto LIVE bind-mount clobbers the production
# inventory. §0 restores LIVE hosts.yaml from bak; tests assert absence.
git archive --format=tar HEAD \
    agent_profiles.py \
    README.md requirements.txt report_schema.py session_schema.py \
    fleet-gates.conf \
    frontend connectors hub tools deploy docs tests \
    > "$repo_root/.package-release.tmp.tar"

# Provenance stamp (optional 2nd arg, "commit|run|url"): appended as
# RELEASE_ORIGIN at the archive root so a deployed release dir can be
# traced back to the exact CI run. CI and local packaging share this one
# code path — the CI workflow only passes the values.
if [[ -n "$origin" ]]; then
    IFS='|' read -r o_commit o_run o_url <<< "$origin"
    printf 'commit: %s\nrun: %s\nurl: %s\n' "$o_commit" "$o_run" "$o_url" \
        > "$repo_root/RELEASE_ORIGIN"
    tar -rf "$repo_root/.package-release.tmp.tar" \
        -C "$repo_root" RELEASE_ORIGIN
    # Keep the last writer's stamp for post-mortem; name is gitignored (*.tmp).
    mv "$repo_root/RELEASE_ORIGIN" "$repo_root/.RELEASE_ORIGIN.tmp.staged"
fi

gzip -c "$repo_root/.package-release.tmp.tar" > "$output"
rm -f "$repo_root/.package-release.tmp.tar"
[[ -n "$origin" ]] && rm -f "$repo_root/.RELEASE_ORIGIN.tmp.staged"

echo "release archive: $output"
