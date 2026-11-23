# Native provider runtime implementation plan

> **For agentic workers:** Execute task-by-task with the executing-plans workflow;
> implementation units may be delegated after their interface is fixed. Keep
> checkbox progress and fresh behavior evidence in this document.

**Goal:** Implement Agy through native ACP and improve all-family evidence,
native permission delivery and managed continuation results for browser work.

**Architecture:** Keep Flask/application/repository and signed outbound node
boundaries. Add a bounded node-owned ACP runtime with exact native resume;
reuse capture, operation receipts, journal, question reservation and Git output
collectors. Vendor negotiation remains in the node provider package.

**Tech stack:** Python 3.10+, stdlib JSON-RPC subprocess transport, existing Flask,
SQLite, encrypted spool and static JavaScript. No new runtime dependency.

## Global constraints

- Normative design: [native-provider-runtime.md](../../native-provider-runtime.md).
- UTF-8 without BOM; preserve unrelated worktrees and changes.
- Agy uses `agy acp`; no print fallback or latest-session selector.
- The browser uses public HTTP and operator authentication only.
- Capability evidence cannot grant actions. Native IDs/paths/auth remain private.
- At-most-once command dispatch; uncertain prompt/answer delivery is not retried.
- Every schema addition needs old/corrupt version and transaction rollback tests.
- Real client acceptance remains distinct from strict subprocess fixture tests.

## Task 1: ACP transport, Agy runtime and Supervisor integration

**Files:** Create `tools/providers/{__init__,acp_transport,acp_mailbox,agy_runtime,agy_worker}.py`;
modify `tools/adapters/agy.py`, `agent_profiles.py`,
`tools/adapters/native_resume.py`, `tools/supervisor/native_resume.py`;
test `tests/test_agy_acp.py`, `tests/fixtures/providers/agy_acp_peer.py` and
existing `tests/test_agy.py`.

**Interfaces:** `AgyAdapter.build_argv(instruction: str) -> list[str]` creates an
owned worker command. `AcpMailbox.answer(root, request_id, selected) -> str`
delivers only current native answers. Worker emits private
`{"type":"fleet_acp_session","session_id":"native-id","cwd":"owned-cwd","resume_supported":true}`
and public typed rows through `on_line`. `AgyRuntime` consumes a trusted command,
cwd, environment, event callback and mailbox. `run_turn(text, resume_session=None)`
returns a validated native outcome and never automatically retries a prompt.

- [x] Write independent stdio peer tests and run to prove missing runtime fails.
- [x] Implement bounded transport and native negotiation/update projection.
- [x] Implement worker/adapter launch and exact persisted ACP continuation.
- [x] Implement cancellation and background/permission settlement; reject unknown
      client file/terminal requests and source identity mismatches.
- [x] Run `python3 -m pytest tests/test_agy.py tests/test_agy_acp.py tests/test_native_continuation.py tests/test_native_tail_drain.py tests/test_cross_platform_agents.py -q` (included in the final combined scope below).
- [ ] Commit the verified units together; shared action/evidence/result contracts
      are released atomically rather than as incompatible intermediate versions.

Independent-peer test shape:

```python
def test_resume_failure_never_creates_sibling(strict_peer):
    outcome = strict_peer.run_worker(resume_session="retained-native-id")
    assert outcome.returncode != 0
    assert strict_peer.methods == ["initialize", "authenticate", "session/resume"]
    assert "session/new" not in strict_peer.methods
```

## Task 2: All-family observable evidence

**Files:** Create `fleet_core/agent_evidence.py` and
`tests/test_agent_evidence.py`; modify public session DTO projection,
`session_schema.py`, browser DTO parser and existing inspector only as needed.

**Interfaces:** Pure `project_agent_evidence(session: Mapping) -> dict` produces
bounded protocol/provenance, visible stream/tools/model/usage/permissions and
history-quality facts. It consumes additive `runtime_protocol` metadata from
Task 1. It must not alter `allowed_actions` or require installed CLIs.

- [x] Add behavior tests for all families, unknown clients, unsupported and
      observed evidence, oversized/secret-shaped metadata and revoked authority.
- [x] Implement pure projection and additive round-trip DTO parsing.
- [x] Document per-family facts and protocol upgrade decision criteria.
- [x] Run `python3 -m pytest tests/test_agent_evidence.py tests/test_agent_capability_layers.py tests/test_native_transcript_detail.py tests/test_boundaries.py tests/test_frontend_separation.py -q`.
- [ ] Commit and record evidence.

```python
def test_evidence_never_grants_control():
    dto = project_agent_evidence({"agent_family": "agy", "runtime_protocol": "acp"})
    assert "allowed_actions" not in dto
```

## Task 3: Native permission answer end to end

**Files:** Extend existing interaction admission and question persistence,
shared signed Supervisor action contract, `tools/supervisor/control_client.py`,
and session UI parser/renderer where required. Test with
`tests/test_acp_interactions.py` and existing `tests/test_session_interactions.py`.

**Interfaces:** Task 1 mailbox exposes `answer(root, request_id, selected) -> str`.
ACP interaction payload has additive `delivery="native_permission"`; public
option IDs map to native option IDs only in the worker. Fixed signed action
`answer_native_request` accepts `{request_id, selected}` after existing Hub
reservation/idempotency. Node resolves mailbox privately from owned session
identity. Existing question answers and ordinary messages retain their protocols.

- [x] Prove live ACP answers currently take the wrong append/new-turn path.
- [x] Implement strict additive request marker, signed answer action, operator
      authorization, current node identity and request expiry checks.
- [x] Add real SQLite/HTTP + signed client/mailbox tests for concurrent retries,
      response loss, revoked authority, closed mailbox and wrong option.
- [x] Run `python3 -m pytest tests/test_acp_interactions.py tests/test_session_interactions.py tests/test_native_session_api.py tests/test_supervisor_control.py -q` (included in the permission regression scope below).
- [ ] Commit and record evidence.

```python
def test_native_answer_does_not_append_text(hub, pending_native_request):
    response = hub.answer(pending_native_request, selected="option_1", client_token="once")
    assert response.status_code == 202
    assert hub.commands()[0]["action"] == "answer_native_request"
    assert len(hub.commands()) == 1
```

## Task 4: Per-continuation results

**Files:** Extend `tools/supervisor/continuation_capture.py` and existing result
collection/delivery, add bounded result persistence/application/HTTP following
existing ports, and extend the current result panel. Tests:
`tests/test_continuation_results.py` and migration/collection regression suites.

**Interfaces:** Result identity is the existing signed continuation command ID,
bound to exact session/task. Existing command journal supplies command identity;
`ContinuationCapture.send` must accept optional command identity without breaking
legacy callers. Use the original managed workspace and a before-turn baseline.
Do not rewrite startup task results. HTTP reads are operator-scoped and bounded.

- [x] Reproduce missing follow-up result with a real temporary Git repository.
- [x] Implement before/after collection, independent execution/collection state,
      atomic idempotent storage and recoverable upload.
- [x] Implement public result read and view using existing frontend components.
- [x] Verify concurrent result isolation, dirty baseline, failure/no changes,
      lost upload, migration rollback/corruption and redaction bounds.
- [x] Run `python3 -m pytest tests/test_continuation_results.py tests/test_task_patch_bounds.py tests/test_task_summary_collection.py tests/test_native_continuation.py tests/test_native_chat_frontend.py -q` (covered by the combined and installed-browser scopes below).
- [ ] Commit and record evidence.

```python
def test_original_result_survives_continuation(result_service, completed_turn):
    original = result_service.startup_result(completed_turn.task_id)
    result_service.publish(completed_turn)
    assert result_service.startup_result(completed_turn.task_id) == original
    assert result_service.get(completed_turn.command_id).session_id == completed_turn.session_id
```

## Task 5: Integration, platform gates and publication

- [ ] Review implementation against every lifecycle/error/authorization clause in
      the development contract and correct gaps before publication.
- [ ] Update native session/task API, testing entry points, acceptance matrix,
      source comparison status and existing Obsidian project/canonical indices.
- [ ] Run changed tests, architecture/packaging checks and full journey gate with
      existing installed browser tooling; do not download large binaries silently.
- [ ] Check staged diff, UTF-8 no BOM, no unrelated changes, and actual old-store
      migration. Rebase onto latest main before merge.
- [ ] Follow existing GitHub CI/deployment chain and inspect exact revision;
      actual client/model checks retain truthful platform-specific evidence.

## Executed evidence

- Baseline main `afe29b1`, research commit `1b55375`; isolated feature branch
  `codex/native-provider-runtime`. No product changes existed at planning start.
- Existing six relevant suites: 104 passed before runtime implementation.
- Task 2: `python3 -m pytest tests/test_agent_evidence.py
  tests/test_session_repository.py tests/test_agent_capability_layers.py
  tests/test_native_transcript_detail.py tests/test_native_session_api.py
  tests/test_session_interactions.py tests/test_boundaries.py
  tests/test_frontend_separation.py tests/test_frontend_contracts.py
  tests/test_session_frontend_contracts.py tests/test_agent_capability_frontend.py
  -q`: 327 passed, 1 browser skipped, 3 subtests passed. Follow-up with existing
  `/tmp/fleet-browser/node_modules/playwright`, `FLEET_BROWSER_CHANNEL=chrome`:
  native-metadata chat and all capability frontend tests: 4 passed, including
  1440/390px evidence labels and no authority promotion. The 34-test evidence
  suite was rerun after strengthening SQLite failure recovery to authenticated
  HTTP: 34 passed. No real vendor/model acceptance is inferred.
- Task 2: same-quality capture notification first reproduced as a failing
  history-gap test; the minimal projection fix preserves `unknown` until an
  actual gap/downward transition. Old v5 migration rollback, malformed schema,
  unknown future version rejection and concurrent durable unions are covered.
  `git diff --check`, local documentation links and UTF-8 without BOM passed.
- Read-only client closure check: live client tar had 199 files (421730 bytes),
  included all seven required new provider/evidence/native-request modules,
  excluded Hub/tests/frontend, and passed isolated imports plus absolute
  `agy_worker.py --help` from its extracted tree. The backend release script
  uses `git archive HEAD`, so new modules must be tracked and committed before
  publication. Counts describe the checked intermediate tree, not final CI.
- Final protocol/result/evidence/architecture scope:
  `PYTHONPATH=. python3 -m pytest tests/test_agy.py tests/test_agy_acp.py
  tests/test_acp_transport_bounds.py tests/test_acp_lifecycle_races.py
  tests/test_acp_interactions.py tests/test_agent_evidence.py
  tests/test_continuation_results.py tests/test_native_continuation.py
  tests/test_native_tail_drain.py tests/test_cross_platform_agents.py
  tests/test_task_patch_bounds.py tests/test_task_summary_collection.py
  tests/test_agent_profiles.py tests/test_agent_capability_layers.py
  tests/test_boundaries.py tests/test_frontend_separation.py
  tests/test_release_layout.py -q -rs`: **351 passed, 2 subtests**.
  Includes an actual worker-to-capture/spool/Hub archive check for complete
  oversized Unicode tool output and source exclusion from ordinary chat.
- Full-suite execution exposed an old unmanaged-question error-precedence
  regression. A standalone run reproduced `503` instead of the required
  `409 session_not_managed`. Shared ownership/revocation/control-service admission
  now runs before question lookup, while action admission remains specific to
  native versus ordinary answers. Fresh scope:
  `PYTHONPATH=. python3 -m pytest tests/test_codock_management_security.py
  tests/test_acp_interactions.py tests/test_session_interactions.py
  tests/test_native_session_api.py tests/test_supervisor_control.py
  tests/test_supervisor_process.py -q`: **273 passed, 2 platform skips,
  2 subtests**. Suites overlap and are not summed as unique coverage.
- This file is the pre-publication execution checkpoint. Exact final platform
  counts, GitHub merge SHA, deployment receipt and external/client boundaries are
  recorded in the publication PR and the existing canonical operations entry;
  release success must not be inferred from local fixture results.
- Fresh real Chrome scope:
  `FLEET_PLAYWRIGHT_MODULE=/tmp/fleet-browser/node_modules/playwright
  FLEET_BROWSER_CHANNEL=chrome PYTHONPATH=. python3 -m pytest
  tests/test_native_chat_frontend.py tests/test_agent_capability_frontend.py
  -q -rs`: **33 passed**. Reused installed tooling; no browser download.
- `ruff check --select E9,F63,F7,F82,F401 hub tools connectors fleet_core`
  and `git diff --check` passed. All 69 changed files decoded as UTF-8 without
  BOM; 51 Python files parsed with the Python 3.10 grammar, and changed Markdown
  local links resolved. These are checks at this checkpoint, not platform CI.
