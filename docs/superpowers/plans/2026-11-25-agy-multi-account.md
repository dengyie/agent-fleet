# AGY Multi-account Implementation Plan

> **For agentic workers:** Use executing-plans to implement this plan task-by-task in the current isolated worktree. Review each delivery before release.

**Goal:** Provide independently authorized Google personal AGY accounts on each machine, remotely manageable over operator HTTP, with immutable task/continuation account identity.

**Architecture:** Reuse the signed supervisor queue and its SQLite transaction for provider operations. Keep Google credentials in node-owned hashed profiles; relay short-lived OAuth responses through encrypted, command-bound storage. Keep runtime and control lifecycle in tools, HTTP orchestration in hub, protocol validation in fleet_core.

**Tech Stack:** Flask, SQLite, AES-256-GCM, existing Ed25519 supervisor commands, Python subprocess/ACP, pytest, existing three-platform CI.

## Global Constraints

- No frontend product changes, real Agent CLI calls or external-model requests.
- UTF-8 without BOM; protect primary worktree and unrelated changes.
- Browser operator/admin identity and machine credentials remain distinct.
- No automatic account rotation or imported terminal/IDE credentials.
- Scope is Google personal OAuth multi-account. Other T3 authentication methods are not implied by support for multiple Google accounts.
- AGY shared executable stays installed. Disable retains credentials; logout clears only the selected account through negotiated native ACP logout after stopping workers; profiles, histories and workspaces remain.
- Feature branch must rebase latest main before merge; follow existing production CI deployment and verify receipt.

## Files and responsibility

- `fleet_core/provider_accounts.py`: identifiers, operation payload, bounded OAuth URL/response validators.
- `hub/infrastructure/provider_account_repository.py`: versioned account/operation tables, atomic signed-queue insertion, encrypted temporary response and monotonic reports.
- `hub/application/provider_account_service.py`: owner/machine checks, operation idempotency, task admission and private command resolution.
- `hub/http/provider_account_routes.py`: operator APIs and machine-only operation/report endpoints.
- `tools/providers/agy_accounts.py`: node-private hashed profiles, generation fences, credential environment and per-account worker lock.
- `tools/providers/agy_auth.py`: bounded asynchronous native --login, response handoff, timeout, verification, durable report replay.
- Existing supervisor, task repository/domain, runner, continuation and composition seams: narrow integration only.
- `tests/test_provider_accounts.py`, `tests/test_agy_accounts.py`: HTTP/SQLite/concurrency and real fixture process behavior.

### Task 1: Protocol and isolated node profiles

**Interfaces:** `account_id(value) -> str`; `authorization_url(value) -> str`; `login_response(url, value) -> str`; `AgyAccounts(root).environment(id, source) -> dict`; `.runtime(id)` yields an owned lease and refuses disabled/signed-out accounts.

- [x] Write and run failing tests: two profiles differ even for case-only identifiers, ambient Google env aliases are absent, SSH file-storage gate is set, and callbacks reject mismatched state.
- [x] Implement deterministic SHA256 paths, strict identifiers and OAuth validation, durable generation fence and native lock. A lease captures generation and `cancelled()` detects account mutation.
- [x] Verify with `python3 -m pytest tests/test_agy_accounts.py -q`.

```python
def test_profiles_never_share_google_credentials(tmp_path):
    accounts = AgyAccounts(tmp_path)
    a = accounts.environment('pa_' + 'a' * 32, {'GOOGLE_API_KEY': 'ambient'})
    b = accounts.environment('pa_' + 'b' * 32, {'google_api_key': 'ambient'})
    assert a['GEMINI_HOME'] != b['GEMINI_HOME']
    assert 'GOOGLE_API_KEY' not in a and 'google_api_key' not in b
    assert a['SSH_CONNECTION'] == '127.0.0.1 0 127.0.0.1 0'
```

### Task 2: Durable Hub account operations

**Interfaces:** operator collection `/api/machines/<machine>/provider-accounts`; detail/action `/api/provider-accounts/<id>` and `/operations`; machine resolver/report `/api/supervisor/provider-accounts/resolve` and `/report`. Signed action `manage_provider_account` carries only account id, revision, operation, owner digest. OAuth response is separately encrypted and expires with command.

- [x] Write failing real SQLite/Flask tests for create/list, duplicate and conflicting token, authorization, wrong-machine resolve, secret redaction and report ordering.
- [x] Add schema version/shape checks and additive atomic migration on existing supervisor database. Extend queue insertion with an explicitly caller-owned connection so operation + command commit together.
- [x] Implement bounded owner-scoped DTOs and operation resolver; same-token replay returns original command/current status, reports cannot mutate newer revisions.
- [x] Verify `python3 -m pytest tests/test_provider_accounts.py tests/test_supervisor_control.py tests/test_native_task_api.py -q`.

```python
response = client.post('/api/machines/mac-local/provider-accounts', json={
    'name': 'Google work', 'client_token': 'create-work'})
assert response.status_code == 201
assert response.json['account']['provider'] == 'agy'
```

### Task 3: Node login and account lifecycle

**Interfaces:** `AgyAccountControl(cfg).execute(command, transport, headers) -> (status, reason)`; `.tick(...)` retries durable reports. Operations: create, login, submit_login, cancel_login, enable, disable, logout.

- [x] Build fixture CLI process that prints a validated Google URL, receives one line, creates a profile credential and exits; no network/model.
- [x] Implement async --login with bounded streams, 300-second TTL, durable state/report, one active flow per account, exact-flow response binding and ACP access verification.
- [x] Stop matching managed sessions and fence runtime admission before login/disable/logout. Acquire selected account's runtime lock before changing credentials; account B must remain active. Login interruption requires explicit retry.
- [x] Wire fixed signed command handling after signature/nonce checks and tick reports from normal control polling.
- [x] Verify fixture login, duplicate submission, cancel, timeout, restart, malformed output and logout retention tests.

### Task 4: Immutable task and continuation binding

**Interfaces:** optional `provider_account_id` on AGY task create and public task DTO; private runner task carries binding owner. Worker retains `--account-root` and `--account-id` through exact native continuation.

- [x] Test task replay conflict when account changes; unknown/wrong-owner/disabled account fails admission; older node rejects the explicit account selection.
- [x] Migrate task nullable binding column atomically, preserving old tasks and original submission replay. Runner prepares owned account environment and worker acquires account lease before spawning AGY. Continuations retain original account args.
- [x] Include public account id in managed session metadata without profile paths or tokens.
- [x] Verify task migration, runner, native continuation and ACP regression suites.

### Task 5: Review, documentation and release

- [x] Document exact request/response and credential/account lifetime, pending vs confirmed status, node capability and old-account behavior in native API authority; link testing entry and upstream pinned sources.
- [x] Run targeted suite and relevant existing tests, lint, `git diff --check`, UTF-8 BOM/scope audit. Add new tests to existing platform CI lists without changing deployment logic.
- [ ] Review concurrency/failure/permission boundaries, commit, push PR, confirm three-platform CI, rebase latest main, merge and inspect production deployment receipt.
- [ ] Record exact test counts and live version; distinguish fixture/vendor behavior and user-required Google authorization.

## Central integration evidence (2026-11-25)

- Fresh broad local regression: 726 passed, 2 existing platform skips, 49 subtests.
- Full macOS CI selection during integration: 780 passed, 2 platform skips, 103 subtests.
- Fresh account-specific HTTP/SQLite/node suites: 104 passed before adding three existing Windows contention-branch checks.
- Final local account/journey/client-release regression: `python3 -m pytest tests/test_agy_accounts.py tests/test_provider_accounts.py tests/test_provider_account_binding.py tests/test_provider_account_control.py tests/test_provider_account_e2e.py tests/test_journey_gate.py tests/test_round7_client_release.py -q`: 146 passed in 11.61 seconds, including the three contention checks.
- Central tests add real signed HTTP login + two worker isolation, account metadata, account operation SQL-trigger rollback, create superseded before delivery, and unconfirmed logout fencing. No real vendor, Google or model calls.
- Windows first physical run: 53 passed, 3 failed, 1 Hub platform skip. NTFS credential inheritance probe passed. Two failures came from a preinstalled third-party tests package; the other observed local waiting before the async report. New test imports use pytest's test-module path and login fixture waits for the report boundary. Fresh Windows run retained separately.
- Final physical Windows account/binding/control/E2E selection: 57 passed, 2 Hub platform skips in 51.17 seconds. The explicit `python tests/fixtures/providers/windows_agy_accounts_probe.py` check confirmed inherited private NTFS DACLs. Logs are retained separately from the first run.
- First PR Windows CI found four profile tests using the runner's pre-existing broad temporary root instead of a new private account-store child. The unchanged product correctly rejected the root. Those fixtures now create their own account directory, and the explicit NTFS probe also grants a temporary fixture directory broad write access and proves it is rejected before checking private credential inheritance.
- Fresh verification after the fixture correction: local account/journey/client-release selection 146 passed in 12.01 seconds; physical Windows selection 60 passed, 2 Hub platform skips in 50.09 seconds (including all three contention cases), followed by passing broad-root rejection and inherited-private-DACL checks.
- Source/lint checks: Ruff E9/F63/F7/F82/F401, Python 3.10 syntax, UTF-8 without BOM, git diff --check, no frontend product changes.
- Recovery maintenance visits at most 64 directories per tick with a retained scanner. Account stop inspects at most 10,000 managed manifests and refuses beyond that bound; no history deletion or unbounded cleanup.
- OAuth restart interrupts the original flow and retains no response code. Non-OAuth cleanup interrupted between fence and receipt requires a new operation/reconciliation; durable supervisor nonce rules never authorize blind replay of an unknown effect.
- Release/production results will be recorded after the exact branch passes CI and is merged. Real Google authorization, vendor quota and frontend account-picker acceptance remain separate boundaries.
