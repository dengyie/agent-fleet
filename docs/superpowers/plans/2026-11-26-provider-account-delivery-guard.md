# Provider account delivery guard implementation plan

> **For agentic workers:** Execute inline with the executing-plans workflow, task by task. No delegated work is needed.

**Goal:** Prevent a queued AGY account task from being leased after its account or owner loses admission, without consuming the task or blocking other work.

**Architecture:** The application service holds the existing supervisor account writer fence while the task repository atomically selects and leases an eligible task. The account repository supplies machine/account/owner bindings; the task repository compares them using an indexed connection-local temporary relation. HTTP contracts and node commands remain unchanged.

**Tech stack:** Python 3.10+, Flask, SQLite, pytest, existing signed node transport.

## Global constraints

- Backend only; no `frontend/` product edits, real Agent CLI, external model or Google calls.
- Reuse the existing application/repository layers and error responses.
- Work in the existing isolated worktree on `codex/provider-account-delivery-guard`.
- UTF-8 without BOM. No credentials, paths or owner identifiers in new public DTOs.
- Preserve task token replay, TTL, nonce, task gates and legacy runner behavior.
- Lock order is supervisor account store, then task store; task repository code never opens the supervisor store.

## Evidence and scope

On main `3b177ac`, `python3 -m pytest tests/test_provider_account_e2e.py -q -k delivery_rechecks` produces **4 failed**: disable/logout accepted before node delivery, revoked owner, and unavailable account service all return the private queued instruction and create a lease. The node still has ready credentials in the first two scenarios. `RunnerService.poll` trusts the runner capability and sets `allow_provider_accounts=True` without consulting current account admission.

Already running executions, session continuation controls, frontend account management and automatic cancellation on administrator revocation are outside this fix. The delivery boundary rechecks current grants; revocation after a lease has already committed cannot recall that response.

## Task 1: Fence account-bound task delivery

**Files:**
- Modify `hub/application/runner_service.py`: inject and use the existing provider account service during poll.
- Modify `hub/application/provider_account_service.py`: translate delivery-fence storage errors using the existing error type.
- Modify `hub/infrastructure/provider_account_repository.py`: expose a context manager yielding ready machine/account/owner bindings; cache the current authorization result once per owner in that poll. Keep its writer lock held through task lease commit. Add a covering index in the existing atomic initialization transaction.
- Modify `hub/infrastructure/task_repository.py`: replace the capability-only bypass with explicit account/owner bindings; use a temporary primary-key relation to avoid parameter-count limits and blocked-head scans.
- Modify `hub/composition/extensions.py`: inject the same account service into tasks and runner.
- Test `tests/test_provider_account_e2e.py`, `tests/test_provider_account_binding.py`, `tests/test_provider_accounts.py`.

**Interfaces:**
- `ProviderAccountService.task_delivery(machine)` yields an iterable of `(account_id, owner_id)` under the account fence.
- `ProviderAccountRepository.task_delivery(machine, authorize=None)` owns that fence and never opens task storage.
- `SqliteTaskRepository.lease_task(..., provider_account_bindings=())` accepts application-authorized bindings; the default can only lease unbound tasks.

- [x] Add and run the four failing HTTP regressions above.
- [x] Implement the five narrow backend integrations.
- [x] Replace the direct repository test's old bypass with `provider_account_bindings=[(ACCOUNT, OWNER)]`.
- [x] Verify default deny, owner/machine matching, large binding sets, blocked-head progress, concurrent single leasing, durable task/lease/audit preservation and account disable serialization.
- [x] Verify real account-mode administrator demotion/deactivation, missing/corrupt account storage and bounded errors without receipt/instruction leakage.

Run:

```sh
python3 -m pytest tests/test_agy_accounts.py tests/test_provider_accounts.py tests/test_provider_account_binding.py tests/test_provider_account_control.py tests/test_provider_account_e2e.py -q
```

Expected: original four regressions pass, existing multi-account behavior stays green. Temporary relations do not persist in `sqlite_master`; repeated initialization and old/future schema tests remain green.

## Task 2: Document, review and publish

**Files:** `docs/native-provider-accounts.md`, `docs/testing/README.md`, this plan.

- [x] Document capability plus current admission, queued recovery, lock linearization and already-leased limits.
- [ ] Run related task/runner/source/HTTP tests and Ruff; inspect diff scope, Python 3.10 syntax and UTF-8/BOM.
- [ ] Commit and push the focused change; create and attach a PR with exact regression evidence.
- [ ] Verify current-head three-platform CI, rebase latest main before merge, and follow existing main deployment.
- [ ] Verify production release bytes, deployment receipt, databases and public endpoints; node update is unnecessary because this change only touches Hub code.
- [ ] Update the existing Obsidian authority and run its documentation checks.

Commands:

```sh
ruff check --select E9,F63,F7,F82,F401 hub tools connectors fleet_core
git diff --check
git fetch origin
git rebase origin/main
```

No deployment implementation changes or new persistent fields are required. The additional index is created atomically by the existing schema initialization and does not change schema version 1 or existing account records.

## Local verification

- Account/provider plus account HTTP, assistant tasks, workflow recovery, task files, legacy bridge and account composition: **238 passed in 33.53s**.
- Task storage/API, cancellation, migrations, runner and source delivery: **322 passed, 5 subtests passed in 25.06s**. These selections overlap and are not a summed coverage count.
- No real Agent CLI, Google login, external model, production task or provider-account mutation was used.
