#!/usr/bin/env bash
set -Eeuo pipefail

output=${1:-/tmp/agent-fleet-release.tgz}
origin=${2:-}
repo_root=$(cd "$(dirname "$0")/.." && pwd)

cd "$repo_root"
staging_dir=$(mktemp -d "$repo_root/.package-release.XXXXXX")
cleanup() { rm -rf "$staging_dir"; }
trap cleanup EXIT
archive_tar=$staging_dir/release.tar
# Backend release only; publish frontend separately with package-frontend-release.sh.
# Pack tracked files only. A working-tree tar would ship local leftovers
# such as tests/__tmp_app_context/state/. git archive never includes
# gitignored runtime stores, credentials, or untracked junk.
# hosts.yaml stays tracked as a local example, but MUST NOT ship in the
# CI artifact: overlaying it onto LIVE bind-mount clobbers the production
# inventory. §0 restores LIVE hosts.yaml from bak; tests assert absence.
git archive --format=tar HEAD \
    agent_profiles.py \
    README.md requirements.txt requirements-test.txt report_schema.py session_schema.py platform_schema.py \
    fleet-gates.conf \
    connectors hub tools deploy docs tests \
    > "$archive_tar"

# Provenance stamp (optional 2nd arg, "commit|run|url"): appended as
# RELEASE_ORIGIN at the archive root so a deployed release dir can be
# traced back to the exact CI run. CI and local packaging share this one
# code path — the CI workflow only passes the values.
if [[ -n "$origin" ]]; then
    IFS='|' read -r o_commit o_run o_url <<< "$origin"
    stamp=$staging_dir/RELEASE_ORIGIN
    printf 'commit: %s\nrun: %s\nurl: %s\n' "$o_commit" "$o_run" "$o_url" \
        > "$stamp"
    # BSD tar otherwise adds AppleDouble resource-fork files rejected by the
    # deployment receiver. GNU tar ignores this environment setting.
    COPYFILE_DISABLE=1 tar -rf "$archive_tar" \
        -C "$staging_dir" RELEASE_ORIGIN
fi

gzip -c "$archive_tar" > "$output"

echo "release archive: $output"
