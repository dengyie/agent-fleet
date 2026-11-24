# Native session metadata recovery review — 2026-11-25

## Scope and confirmed failures

Baseline: GitHub main `800515a8a1288f06ff0eb9699eb3f47de131c8fa`.
The work is limited to the backend/node metadata chain. No frontend product,
deployment implementation or vendor configuration/history is changed.

The original focused regression produced **7 failed, 1 passed** before the
implementation. Source-shaped Claude rows lost their string client version;
ZCode/Hermes native stores had no metadata projection; old EOF archive and
retained managed identities could not recover existing metadata. A direct
repository regression also proved that a historical observation required
fill-only semantics to avoid replacing a newer model/title.

Additional regressions reproduced these reachable ordering/binding failures:

- A recovered capture opened without its original process-group envelope and
  could downgrade a retained managed session's capture quality.
- A Codex rollout may have no native name even when the exact v5 thread-store
  row has a title/name, model and version.
- Publishing the current native-store name before importing older rollout
  rows allowed an old title in those rows to overwrite the current name.

Read-only inspection of nine fixed Mac/Windows acceptance identities confirmed
that some missing facts already exist in the vendor stores. Reader-only checks
with the new source passed on both actual OS hosts; they did not execute Agent
CLIs, send prompts or call an external model. Missing native facts remain
unknown. In particular Pi's numeric session version is not a client version.

## Implementation and layering

`normalization.py` owns native-row version parsing. `native_storage.py` reads a
single exact display row with query-only SQLite, optional-column support,
finite vendor timestamp conversion and bounded values. Codex supports only the
observed `state_5.sqlite` layout and requires matching id, cwd and rollout path;
an explicitly set `name` takes precedence over `title`. No newest-schema guess
or database scan is introduced.

The focused `native_metadata.py` reader inspects bounded JSONL prefix/tail
windows, verifies the existing private continuation binding and refuses
ambiguous identities, unsafe files and changed snapshots. It returns display
facts only. RunnerCapture owns publication through the existing encrypted
spool and stores a version/fingerprint/digest checkpoint atomically in its
existing recovery index. Live spool writer ownership excludes recovery.

Historical prefix facts use the additive boolean
`session_metadata.fill_missing`; the existing SQLite repository fills only
NULL/empty metadata fields within its transaction. Current native tail/store
facts retain normal per-field Hub cursor ordering. Archive metadata is applied
after each imported content slice, so older source rows cannot replace the
current store name. Native content cursors are not rewound or advanced by the
metadata reader.

HTTP response loss reuses the original spooled event ids. A checkpoint failure
after successful spool publication can produce another metadata observation
on the next pass; it does not duplicate conversation messages or create an
unacknowledgeable content-derived event id. This cross-file boundary is
documented rather than presented as an atomic transaction.

Managed replay validates the original manifest's session, machine, attempt
and family before retaining its process-group envelope. It does not adopt a
live process or grant an action. Existing HTTP DTOs/operator authorization and
the browser's separate interactive-question protocol remain intact. No
session/Supervisor SQLite schema version changes.

## Verification

The final targeted command was:

```sh
python3 -m pytest tests/test_native_metadata_recovery.py \
  tests/test_native_transcript_detail.py tests/test_review_node_sync.py \
  tests/test_native_agent_storage.py tests/test_session_bridge.py \
  tests/test_session_repository.py tests/test_session_schema.py \
  tests/test_agent_evidence.py tests/test_review_node_runner_capture.py \
  tests/test_native_continuation.py tests/test_continuation_results.py \
  tests/test_native_session_api.py tests/test_boundaries.py \
  tests/test_frontend_separation.py tests/test_round5_node.py \
  tests/test_runner_conversation.py tests/test_release_layout.py -q
```

Result: **494 passed, 50 subtests passed**. The new file contains 28 behavior
tests, including EOF recovery, original session identity, read-only native
stores, ambiguous/mismatched bindings, old/future checkpoints, malformed and
partial rows, bounded windows, live writer exclusion, HTTP response loss,
checkpoint disk failure, fill-only projection and returning to a previous
title without stalling the sequence.

```sh
FLEET_PLAYWRIGHT_MODULE=/tmp/fleet-browser/node_modules/playwright \
FLEET_BROWSER_CHANNEL=chrome python3 -m pytest \
  tests/test_native_chat_frontend.py \
  -k 'source-record or native-metadata or directory-metadata' -q
```

Result: **3 passed, 27 deselected**, using actual Chrome and temporary HTTP/
SQLite. Ruff's syntax/undefined/unused-import gate, `git diff --check` and
UTF-8/no-BOM checks passed. Windows/macOS CI selectors now explicitly include
the new suite; production publication still requires the unchanged release
workflow's complete gates.

## Merge, production release, and client acceptance

PR [#127](https://github.com/dengyie/agent-fleet/pull/127) was merged as
`0fce3293dc27a56f86321365400b0f94775f8f79`. Main CI
[45468876082](https://github.com/dengyie/agent-fleet/actions/runs/45468876082)
completed successfully: Linux `8906 passed, 2 warnings, 204 subtests`,
47/47 journeys and 2830 selectors; Windows `1065 passed, 44 skipped`; macOS
`673 passed, 2 skipped, 103 subtests`. Package and deploy jobs succeeded.

The receiver reported `deployed/succeeded`; the rollback anchor is
`/var/lib/agent-fleet-deploy/45468876082-xj1kegr0`. The production release
matched all 1350 backend files and 107 public resources to that CI run. All six
live SQLite databases and their deployment snapshots passed integrity and
foreign-key checks. The operator authorization probe returned 401 for
anonymous, Runner, and Supervisor session reads, 200 for the operator, and 401
for a revoked cookie.

Both signed clients reached `0fce3293dc27-s33` (sequence 33), with all 200
release files verified and the runner/control-client active; sequence 32 was
retained. On the nine fixed Mac/Windows histories, operator HTTP verification
read 1162 events and confirmed prior event identities and hashes were
preserved. Four sessions exposed a native title, seven a model, four a
provider, six a client version, and seven an explicit native start time. No
conversation message or tool event was replayed; the check recorded zero
business writes and zero Agent invocations. Fields absent from a particular
native source remain unknown.

Chrome production checks at 1440px and 390px verified the native title, client
version, model and start-time metadata in the session view. The continuation
results API was requested exactly once and returned the expected
“尚未上报续聊成果” state for these older sessions. Both layouts had zero page
errors, asset failures, and business API writes. The broad recent-25 directory
sample still had zero native titles; this fixed-history acceptance therefore
does not imply fleet-wide metadata completeness. No real Agent CLI or external
model was run.

The private, sanitized acceptance evidence is retained at
`/Users/mango/.cache/agent-fleet/verification/native-session-metadata-20261125/`.

## Boundaries

The JSONL pass reads at most 256 KiB of prefix and 2 MiB of tail and parses at
most 4096 complete rows per window. A native name/model outside those windows
and absent from a supported exact store cannot be recovered by this pass.
Unsupported vendor database versions, absent binaries and unobserved provider
fields remain unknown. Native titles, prompts and credentials are not copied
into this report or metadata checkpoints. Internal reasoning, system prompts,
usage/cost and raw database information remain outside the public conversation
contract. No real Agent execution is claimed for this iteration.

The canonical operational runbook records the release and field coverage;
private acceptance evidence remains outside the repository and contains no
native titles or raw conversation text in the report summary.
