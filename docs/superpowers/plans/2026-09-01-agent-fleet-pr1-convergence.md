# Agent Fleet PR #1 Review, Convergence, and Documentation Plan

> **Status (2026-09-08): historical / implemented.** Open checkboxes below are the original plan and were not kept in sync after merge. Do not re-implement from unchecked boxes. Remaining work is recorded in `docs/HANDOFF.md` §六 as `done` or `deferred-with-condition`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Review the complete discovery/adoption lifecycle in PR #1, fix the verified non-blocking correctness gaps before merge, merge only after independent tests and security review pass, and synchronize repository plus Obsidian documentation with the merged behavior.

**Architecture:** Keep the PR’s additive, default-off architecture: discovery publishes bounded metadata; adoption is operator-explicit and stored in an isolated SQLite database; attach/control uses signed pull-mediated supervisor commands; detach never signals; exact capture is a separate operator upgrade. Before merge, close the anonymous frontend contract mismatch, provide a recoverable pending-adoption path, serialize revoke-to-detach issuance, and exclude runtime `var/` storage from source control.

**Tech Stack:** Python 3, Flask, SQLite/WAL, Ed25519, pytest, native ES modules, GitHub pull request #1.

**Spec:** `docs/superpowers/specs/2026-08-29-agent-instance-discovery-adoption-design.md`; implementation baseline `docs/superpowers/plans/2026-08-30-agent-discovery-adoption-plan.md`.

## Global Constraints

- Work from `/opt/agent-fleet/.claude/worktrees/agent-discovery-adoption` for PR changes; do not edit the main worktree in parallel.
- No production mutation, merge, or push is performed by a read-only hakus audit. Main reviews and applies fixes.
- Do not print or inspect credential values, private keys, cookies, raw transcript data, full process command lines, or unbounded HTTP bodies.
- New public responses remain bounded and must never include raw pid, pgid, executable path, command line, native transcript path, exception text, or secret material.
- `adopt` remains operator-authenticated; supervisor poll/receipt remains machine-credential-authenticated; ingest token is never reused for operator actions.
- `detach` must not signal a target process; `terminate` requires a fresh identity guard and is never triggered by stale or drifted identity.
- Session, adoption, observation, and task stores remain physically separate; a store failure must not poison unrelated routes.
- All new gates remain default-off, explicit false wins, and each gate can be rolled back independently.
- Every fix is TDD: failing regression test, minimal implementation, focused tests, full suite, independent commit.

---

## File Structure

PR implementation files:

- `tools/probe/discovery.py`, `connectors/generic.py`: local process discovery and bounded identity metadata.
- `report_schema.py`, `tools/probe_collectors.py`: instance allowlist, truncation, and additive ingest payload.
- `hub/application/adoption_service.py`: candidate validation, idempotent adoption, revoke, control, drift reconciliation.
- `hub/domain/adoption.py`: state model and legal transitions.
- `hub/infrastructure/adoption_repository.py`: isolated SQLite/WAL persistence and compare-and-set status transitions.
- `hub/http/adoption_routes.py`: operator HTTP adapter and bounded projections/errors.
- `hub/bootstrap.py`, `hub/config.py`, `hub/web.py`: feature gates and service wiring.
- `tools/supervisor/supervisor.py`, `tools/supervisor/control_client.py`, `tools/session/bridge.py`: attach, identity guard, detach, and transcript tail.
- `hub/http/observe_routes.py`, `hub/application/observe_service.py`, `hub/domain/machine.py`: public instance projection.
- `frontend/api/contracts.js`, `frontend/views/machine.js`: bounded frontend contracts and UI.

Tests:

- Discovery/schema/ingest: `tests/test_discovery.py`, `tests/test_instance_schema.py`, `tests/test_instance_ingest.py`, `tests/test_probe_collectors.py`, `tests/test_report_schema.py`.
- Adoption store/service/API: `tests/test_adoption_repository.py`, `tests/test_adoption_service.py`, `tests/test_adoption_http.py`, `tests/test_adoption_control_http.py`.
- Attach/control/lifecycle: `tests/test_supervisor_attach.py`, `tests/test_attach_bridge.py`, `tests/test_adoption_control_guard.py`, `tests/test_adoption_lifecycle.py`, `tests/test_adoption_e2e.py`, `tests/test_exact_capture_upgrade.py`.
- Gate/frontend regressions: `tests/test_feature_gate_runtime.py`, `tests/test_frontend_adoption_contracts.py`.

---

## Phase 1: Review baseline and freeze the merge surface

### Task 1: Reproduce PR #1 and classify changed commits

**Owner:** hakus (read-only audit), then main validates.

**Files:** no modifications.

**Interfaces:**
- Consumes: PR #1 `worktree-agent-discovery-adoption` and its 24 commits.
- Produces: a bounded review matrix mapping each commit to feature area, tests, gate status, storage path, auth domain, and rollback unit.

- [ ] **Step 1: Verify branch and merge base**

```bash
git show-ref --verify refs/heads/worktree-agent-discovery-adoption
git merge-base main worktree-agent-discovery-adoption
gh pr view 1 --json number,state,headRefName,baseRefName,mergeStateStatus
```

Expected: PR #1 is open, based on `main`, and merge state is reported without relying on an obsolete PR number.

- [ ] **Step 2: Review the diff by boundary, not only by file count**

```bash
git diff --name-status main..worktree-agent-discovery-adoption
git diff --stat main..worktree-agent-discovery-adoption
```

Classify changes into discovery, schema, observation, adoption, supervisor, session bridge, frontend, gates, and docs. Flag any change outside those boundaries.

- [ ] **Step 3: Run the PR suite without cache artifacts**

```bash
cd /opt/agent-fleet/.claude/worktrees/agent-discovery-adoption
python3 -m pytest -q -p no:cacheprovider
```

Expected baseline: `1357 passed`, `2 skipped`, `148 subtests passed` or a newer fully explained count. Any failure blocks merge.

- [ ] **Step 4: Produce the review matrix**

For each boundary record exact files/lines, tests, default-off behavior, public fields, and rollback. Do not copy raw process/credential data into the matrix.

**Rollback:** none; read-only review.

### Task 2: Add source-control hygiene for runtime stores

**Files:**
- Modify: `.gitignore`
- Test: `tests/test_release_layout.py` or a focused repository hygiene test

**Interfaces:**
- Consumes: `FleetConfig` default paths under `<root>/var`.
- Produces: a source tree that cannot accidentally stage adoption/session runtime databases, transcript roots, or related WAL files.

- [ ] **Step 1: Write the failing test**

```python
def test_runtime_var_storage_is_ignored(tmp_path):
    ignored = tmp_path / "var" / "adoptions" / "meta.db"
    ignored.parent.mkdir(parents=True)
    ignored.write_bytes(b"runtime")
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", str(ignored)],
        cwd=REPO_ROOT, text=True, capture_output=True,
    )
    assert result.returncode == 0
```

- [ ] **Step 2: Verify failure**

```bash
python3 -m pytest -q tests/test_release_layout.py -k runtime_var
```

Expected: FAIL because `var/` is not ignored.

- [ ] **Step 3: Implement the minimal ignore rule**

Add `var/` to `.gitignore`, preserving the existing explicit exception for any checked-in frontend state module if one exists. Do not ignore source directories named `frontend/state`.

- [ ] **Step 4: Verify pass and release safety**

```bash
python3 -m pytest -q tests/test_release_layout.py -k 'runtime_var or package'
git status --short --ignored | grep -E 'var/|meta\.db|\.db-wal|\.db-shm' || true
```

Expected: runtime stores are ignored and no release package includes them.

- [ ] **Step 5: Commit**

```bash
git add .gitignore tests/test_release_layout.py
git commit -m "fix: ignore runtime adoption and session stores"
```

**Rollback:** revert this commit; runtime stores remain outside the release archive by package-layout checks.

---

## Phase 2: Fix operator-visible correctness gaps

### Task 3: Make anonymous instance projections frontend-safe

**Files:**
- Modify: `frontend/api/contracts.js`
- Modify: `frontend/views/machine.js` only if the contract adapter needs a separate metadata-only view
- Test: `tests/test_frontend_adoption_contracts.py`

**Interfaces:**
- Consumes: observe API’s anonymous instance projection `{agent_family, attachable}` and authenticated bounded instance rows.
- Produces: `parseMachine` that renders a machine page when instance identity fields are intentionally absent, while adoption controls remain unavailable until an authenticated full candidate is present.

- [ ] **Step 1: Write failing contract tests**

```javascript
test("anonymous instance projection is metadata-only", () => {
  const machine = parseMachine({
    name: "m1",
    instances: [{ agent_family: "codex", attachable: false }],
  });
  assert.equal(machine.instances.length, 1);
  assert.equal(machine.instances[0].pid, null);
  assert.equal(machine.instances[0].agent_family, "codex");
  assert.equal(machine.instances[0].attachable, false);
});

test("authenticated candidate still requires bounded identity fields", () => {
  assert.throws(() => parseInstanceRow({ agent_family: "codex" }));
});
```

Adapt syntax to the existing Node fixture style; do not weaken authenticated adoption validation.

- [ ] **Step 2: Verify failure**

```bash
python3 -m pytest -q tests/test_frontend_adoption_contracts.py
```

Expected: FAIL because a missing `pid`/`pgid` currently rejects the entire machine payload.

- [ ] **Step 3: Implement the metadata-only branch**

Make the list parser distinguish `metadata-only` from `candidate`. Normalize absent identity fields to `null`, preserve only `agent_family` and `attachable`, and mark the row non-adoptable. Keep strict rejection for malformed authenticated candidate rows and unknown family values.

- [ ] **Step 4: Verify pass**

```bash
python3 -m pytest -q tests/test_frontend_adoption_contracts.py tests/test_instance_ingest.py
bash deploy/test-static-frontend.sh
```

Expected: machine rendering contract passes and static assets remain credential-free.

- [ ] **Step 5: Commit**

```bash
git add frontend/api/contracts.js frontend/views/machine.js tests/test_frontend_adoption_contracts.py
git commit -m "fix: tolerate metadata-only instance projections"
```

**Rollback:** revert this commit; backend projection and adoption auth remain unchanged.

### Task 4: Make pending adoption recoverable and safe

**Files:**
- Modify: `hub/application/adoption_service.py`
- Modify: `hub/http/adoption_routes.py` if an explicit retry endpoint is selected
- Modify: `hub/domain/adoption.py`, `hub/infrastructure/adoption_repository.py` only for the legal transition needed
- Test: `tests/test_adoption_service.py`, `tests/test_adoption_http.py`, `tests/test_adoption_lifecycle.py`

**Interfaces:**
- Consumes: existing pending/adopted/revoked state model and supervisor enqueue/receipt callbacks.
- Produces: no permanent pending dead-end after an enqueue failure or terminal rejected adoption; retries are explicit, bounded, idempotent, and never create a second session id.

- [ ] **Step 1: Write failing tests for enqueue failure and explicit retry**

```python
def test_enqueue_failure_does_not_leave_unrecoverable_pending(service, repo, supervisor):
    supervisor.enqueue.side_effect = SupervisorServiceError("transport_error")
    with pytest.raises(SupervisorServiceError):
        service.adopt("m1", 10, "2026-08-30T00:00:00Z", "op@example.com")
    record = repo.find_by_candidate("m1", 10, "2026-08-30T00:00:00Z")
    assert record is None or record.status == "retryable"

def test_retry_pending_reuses_session_and_enqueues_once(service, repo, supervisor):
    pending = seed_pending(repo)
    result = service.retry(pending.session_id, "op@example.com")
    assert result.session_id == pending.session_id
    assert supervisor.enqueue.call_count == 1
```

Also add a terminal-rejection test asserting the operator receives a bounded retryable state, not a silent dead-end.

- [ ] **Step 2: Verify failure**

```bash
python3 -m pytest -q tests/test_adoption_service.py -k 'enqueue_failure or retry_pending'
```

Expected: FAIL because `adopt()` currently leaves `pending` and a repeated adopt returns it without re-enqueueing.

- [ ] **Step 3: Implement one recovery contract**

Choose the minimal contract consistent with existing API conventions: on enqueue failure, atomically mark the record `retryable` (or delete only the newly created pending row if the repository already guarantees no audit gap); add an operator-authenticated retry action that reuses the existing session/adoption id, increments no unbounded counter, and emits only one new signed command per explicit request. A probe rejection must preserve a bounded retryable/rejected projection and must never auto-signal.

- [ ] **Step 4: Verify pass and idempotency**

```bash
python3 -m pytest -q tests/test_adoption_service.py tests/test_adoption_http.py tests/test_adoption_lifecycle.py
```

Expected: repeated retry of an already executing/succeeded adoption is idempotent and no duplicate session id is created.

- [ ] **Step 5: Commit**

```bash
git add hub/application/adoption_service.py hub/http/adoption_routes.py hub/domain/adoption.py hub/infrastructure/adoption_repository.py tests/test_adoption_*.py
git commit -m "fix: make pending adoption recovery explicit"
```

**Rollback:** revert this commit; existing revoke/re-adopt remains available, but the fix must not be removed after gates are enabled without first disabling adoption.

### Task 5: Serialize revoke-to-detach issuance

**Files:**
- Modify: `hub/application/adoption_service.py`
- Modify: `hub/infrastructure/adoption_repository.py`
- Test: `tests/test_adoption_service.py`, `tests/test_adoption_e2e.py`

**Interfaces:**
- Consumes: repository status transitions and existing detach enqueue path.
- Produces: exactly one detach command for concurrent revoke requests; losers return the already-revoked/idempotent bounded result and never enqueue.

- [ ] **Step 1: Write the failing concurrency test**

```python
def test_concurrent_revoke_enqueues_one_detach(service, repo, supervisor):
    record = seed_adopted(repo)
    barrier = threading.Barrier(2)
    results = []
    def run():
        barrier.wait()
        results.append(service.revoke(record.session_id, "op@example.com"))
    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert supervisor.enqueue.call_count == 1
    assert repo.get(record.session_id).status == "revoked"
```

- [ ] **Step 2: Verify failure**

```bash
python3 -m pytest -q tests/test_adoption_service.py -k concurrent_revoke
```

Expected: FAIL or demonstrate two detach enqueues under the current read-then-update sequence.

- [ ] **Step 3: Implement compare-and-set revoke**

Add a repository method that changes `adopted`/`pending` to `revoked` and returns the winning row count. Only the caller with row count `1` enqueues detach. A row count of `0` reads the now-revoked record and returns the existing bounded result. Preserve the rule that detach itself never calls `killpg` or sends a signal.

- [ ] **Step 4: Verify pass**

```bash
python3 -m pytest -q tests/test_adoption_service.py tests/test_adoption_e2e.py tests/test_supervisor_attach.py
```

Expected: one detach command, no process signal, and drift auto-revoke remains idempotent.

- [ ] **Step 5: Commit**

```bash
git add hub/application/adoption_service.py hub/infrastructure/adoption_repository.py tests/test_adoption_service.py tests/test_adoption_e2e.py
git commit -m "fix: serialize adoption revoke detach"
```

**Rollback:** revert this commit and disable adoption before deploying the reverted lifecycle code.

---

## Phase 3: Merge gate and post-merge verification

### Task 6: Run independent security and compatibility review

**Owner:** hakus read-only; main resolves every finding.

**Files:** no modifications unless a finding is confirmed.

- [ ] **Step 1: Verify auth matrix**

Exercise only status codes against a temporary app: anonymous, ingest token, operator header, runner credential, and supervisor credential. Assert each route accepts exactly its intended credential domain.

- [ ] **Step 2: Verify redaction matrix**

Use synthetic values such as `token=synthetic` and `/synthetic/path`; assert no raw pid/path/cmdline/secret appears in public projections, bounded errors, audits, frontend assets, or test failure output.

- [ ] **Step 3: Verify identity and signal matrix**

Run the existing E2E fixtures for pid reuse, executable drift, permission loss, detach, terminate, and drift auto-revoke. Assert `signals == []` for adopt/detach/revoke/drift and only the guarded terminate path may issue the configured signal.

- [ ] **Step 4: Verify store isolation**

Force adoption SQLite failure, session repository failure, and observation repository failure independently. Assert unaffected routes remain available and no half-initialized SQLite connection leaks.

- [ ] **Step 5: Record bounded review result**

Merge is blocked by any confirmed auth, redaction, signal, identity, isolation, test, or gate-default failure.

### Task 7: Merge PR #1 in staged commits

**Files:** PR branch and `main`; no unrelated untracked artifacts.

- [ ] **Step 1: Re-run all tests after fixes**

```bash
cd /opt/agent-fleet/.claude/worktrees/agent-discovery-adoption
python3 -m pytest -q -p no:cacheprovider
python3 -m compileall -q hub tools connectors tests
bash deploy/test-static-frontend.sh
bash deploy/test-release-routing.sh
```

Expected: all pass; record the exact counts.

- [ ] **Step 2: Verify default-off route surface**

```bash
python3 -m pytest -q tests/test_feature_gate_runtime.py -k 'default or explicit_false'
```

Expected: `/api/sessions`, `/api/supervisor/poll`, and `/api/adoptions` are absent when gates are unset or explicitly false.

- [ ] **Step 3: Verify release archive membership**

```bash
bash deploy/package-release.sh /tmp/agent-fleet-release.tgz
 tar -tzf /tmp/agent-fleet-release.tgz | grep -E '(^|/)tools/session/|(^|/)tools/supervisor/|agent_profiles\.py$'
```

Expected: runtime import files are present; credentials, state, `var/`, and generated frontend artifacts are absent.

- [ ] **Step 4: Merge only after checks are green**

```bash
gh pr checks 1
gh pr merge 1 --merge --delete-branch=false
```

If repository policy requires a user click or prohibits CLI merge, stop after the checks and provide the exact PR URL/status; do not bypass branch protection.

- [ ] **Step 5: Verify main after merge**

```bash
git fetch origin main
 git log -1 --oneline origin/main
 git diff --exit-code origin/main -- .
```

Use a clean main worktree for post-merge tests. Do not include `.playwright-mcp/`, `frontend-dist/`, or `frontend-v2/` unless they are explicitly part of the chosen release artifact.

**Rollback:** close gates first, then revert the merge commit if a regression appears; preserve adoption/session databases and task leases.

### Task 8: Synchronize repository and Obsidian documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/HANDOFF.md`
- Modify: `docs/architecture-v4-control-plane.md`
- Modify: `docs/superpowers/plans/2026-08-30-agent-discovery-adoption-plan.md` to mark completed task boundaries, not to erase history
- Modify Obsidian canonical docs only after rereading `00.MOC/AI-DOC-ROUTER.md`

- [ ] **Step 1: Document merged behavior and gate defaults**

State that discovery metadata is additive, adoption/session/supervisor are independently gated off by default, operator auth is separate from ingest auth, and exact capture requires explicit upgrade.

- [ ] **Step 2: Document actual route names and bounded errors**

List `/api/adoptions`, `/api/adoption/<session_id>/source/control`, `/api/adoptions/<session_id>/capture-exact`, `/api/sessions`, `/api/supervisor/poll`, and receipts, with fixed error-code semantics and no raw identity disclosure.

- [ ] **Step 3: Update the canonical Obsidian router and MOC**

Use one agent-fleet router row, point to the canonical deployment manual, add the project index link if missing, and preserve the incident as historical/closed evidence. Do not create a new timestamped orphan note.

- [ ] **Step 4: Run documentation health checks**

```bash
cd "$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents/obsidian-note"
python3 .local/bin/scan-stale-docs
```

Expected: no broken links or stale topology claims.

- [ ] **Step 5: Commit repository docs separately**

```bash
git add README.md docs
 git commit -m "docs: record merged adoption lifecycle and gates"
```

**Rollback:** revert repository docs and use Obsidian history for note rollback; do not roll back code by deleting runtime databases.

---

## Completion Gate

- PR #1 is merged only after the full PR suite, static/release checks, default-off gate tests, and hakus security review pass.
- Anonymous metadata projections do not crash the frontend; authenticated candidate validation remains strict.
- Pending adoption has an explicit bounded recovery path; concurrent revoke produces one detach command.
- `var/` runtime stores are ignored and excluded from release archives.
- Main contains the complete lifecycle code while all new gates remain off.
- Repository and Obsidian docs agree on route names, auth domains, storage isolation, and rollback.
