# Push-Only Agent Fleet Implementation Plan

> **Status (2026-09-08): historical / implemented.** Open checkboxes below are the original plan and were not kept in sync after merge. Do not re-implement from unchecked boxes. Remaining work is recorded in `docs/HANDOFF.md` §六 as `done` or `deferred-with-condition`.

> **For agentic workers:** Execute this plan task-by-task with verification after every task. SSH is not part of the runtime architecture.

**Goal:** Convert agent-fleet to a push-only architecture where every machine runs a local probe and HK only authenticates, stores, reconciles, and displays reports.

**Architecture:** `tools/agent-self-report.py` becomes the single agent-side probe and collects system, Hermes, Claude Code, Codex, and generic process state locally. HK `hub/web.py` remains the sole hub; `/api/ingest` is the only write path, and stale reports are marked offline by reconciliation without remote execution. SSH config, SSH probing, and central pull scanning are removed from the normal runtime path.

**Tech Stack:** Python 3.10+, Flask, PyYAML only for hub config, standard-library `urllib`, `subprocess`, `os`, `pathlib`, and unittest.

## Global Constraints

- No SSH commands, SSH config, or private-key dependency in normal runtime code.
- Every ingest request requires `AGENT_FLEET_INGEST_TOKEN` or `credentials/ingest-token`; startup fails closed when the hub token is absent.
- Machine names match `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`.
- State writes remain atomic and safe under concurrent ingest/reconciliation.
- Probe output contains only metadata and counts; do not upload session contents or credentials.
- Existing `hub.example.com` endpoint remains the central endpoint; deployment changes are documented but not performed in this code-only pass.

---

### Task 1: Local-only probe contract

**Files:**
- Modify: `connectors/probe.py`
- Modify: `connectors/base.py` only if type/docstrings reference SSH
- Test: `tests/test_push_only.py`

**Interfaces:**
- Produce `ProbeContext(machine)` with `run`, `run_python`, and `run_batch` executing only local subprocesses.
- Remove `_ssh_args`, `_probe_ssh`, SSH aliases, and SSH config resolution from the runtime context.

- [ ] Write a failing test asserting `ProbeContext("mac-local").run("printf ok") == "ok"` and that the module source contains no `ssh` command construction.
- [ ] Run `.venv/bin/python -m unittest tests.test_push_only -v`; expected failure because the current context still requires SSH-aware constructor behavior.
- [ ] Implement the local-only context with bounded subprocess timeouts and deterministic empty output on timeout.
- [ ] Run the same test and `python3 -m compileall -q connectors`; expected PASS.

### Task 2: Agent-side probe collectors

**Files:**
- Modify: `tools/agent-self-report.py`
- Create: `tools/probe_collectors.py`
- Test: `tests/test_push_only.py`

**Interfaces:**
- `probe_collectors.collect_all(agent_types=None) -> dict` returns `{hermes, claude_code, codex, generic}` metadata.
- `agent-self-report.py` calls `collect_all()` and sends payload `{machine, agents, system, ts}`.
- Each collector reads local metadata only: Hermes state/session count, Claude project file metadata, Codex rollout metadata, generic process count; no session contents.

- [ ] Add failing tests for macOS system metrics, Codex session discovery, and a payload containing `agents` rather than the old singular `agent` field.
- [ ] Run the focused tests and confirm failure against the current Hermes-only implementation.
- [ ] Implement collectors using standard library only; make `--agents` accept a comma-separated allowlist and default to all collectors.
- [ ] Add `--dry-run` to print redacted JSON without network access.
- [ ] Re-run focused tests and `python3 tools/agent-self-report.py --dry-run --name mac-local`; expected PASS and no credentials/session text.

### Task 3: Ingest-only hub and stale reconciliation

**Files:**
- Modify: `hub/scan.py` into a no-remote `reconcile_ingest()` module
- Modify: `hub/web.py` so startup and `/api/scan` call reconciliation only
- Modify: `hub/state.py` and `hub/events.py` only where needed for concurrent ingest/reconciliation
- Test: `tests/test_push_only.py`

**Interfaces:**
- `hub.scan.reconcile_ingest(now=None) -> list[dict]` reads current snapshots and marks reports older than `stale_after_s` as offline.
- It must never construct or execute an SSH context.
- `POST /api/ingest` is the only state-writing API; `POST /api/scan` is renamed/documented as reconciliation and requires the same token.

- [ ] Add a failing test that monkeypatches subprocess and asserts reconciliation performs zero subprocess calls.
- [ ] Add a failing test that a fresh ingest snapshot remains reachable and an expired one becomes offline.
- [ ] Implement reconciliation using atomic state writes already present in `hub/state.py`.
- [ ] Make `make_app()` reject missing token in production mode; tests may pass an explicit token.
- [ ] Run focused tests and Flask client checks for 403 without token, 200 with token, and 400 for traversal names.

### Task 4: Push-only configuration and deployment docs

**Files:**
- Modify: `hosts.yaml` so every registered node uses `transport: ingest` and no SSH aliases
- Modify: `README.md`
- Modify: `docs/HANDOFF.md`
- Modify: `docs/architecture-v3.md`
- Modify: `deploy/nginx-expose.md`
- Create: `deploy/agent-self-report.cron.example`

**Interfaces:**
- A node is discovered automatically from a valid ingest payload; `hosts.yaml` is optional display metadata and stale TTL configuration.
- HK runtime command is `AGENT_FLEET_INGEST_TOKEN='<secret>' python3 hub/web.py --host 0.0.0.0 --port 8790`.
- Agent runtime command is `python3 tools/agent-self-report.py --endpoint https://hub.example.com --token '<secret>' --name '<node>'`.

- [ ] Remove SSH setup instructions and replace them with token provisioning and cron examples.
- [ ] Document that the probe must be installed on each machine; no reverse SSH, private key, or central credentials are required.
- [ ] Document the remaining limitation: v2 control commands are not implemented in push-only mode.

### Task 5: Full verification

**Files:**
- Test: `tests/test_push_only.py` and existing `tests/test_regressions.py`

- [ ] Run `.venv/bin/python -m unittest discover -s tests -v`.
- [ ] Run `python3 -m compileall -q connectors hub tools tests`.
- [ ] Run `git grep -n -E 'ssh|ssh_alias|credentials/config' -- ':!docs/architecture-review.md'` and confirm matches are historical docs only, not runtime paths.
- [ ] Run Flask test-client checks for auth, traversal rejection, ingest persistence, and reconciliation.
- [ ] Run `python3 tools/agent-self-report.py --dry-run --name mac-local` and verify all agent keys are metadata-only.
- [ ] Report deployment as pending until the HK container receives the token and each target machine receives the probe.

---

## Self-review

- Spec coverage: removes SSH runtime dependency, adds direct probes, keeps HK hub, protects ingest, handles stale reports, and documents deployment.
- No private key or token is written to source, tests, or docs.
- Push-only control is intentionally out of scope and explicitly documented rather than silently pretending SSH control still works.

