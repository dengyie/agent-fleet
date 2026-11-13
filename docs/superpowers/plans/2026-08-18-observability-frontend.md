# Agent Fleet Observability Frontend Implementation Plan

> **Status (2026-09-08): historical / implemented.** Open checkboxes below are the original plan and were not kept in sync after merge. Do not re-implement from unchecked boxes. Remaining work is recorded in `docs/HANDOFF.md` §六 as `done` or `deferred-with-condition`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a complete operator-facing dashboard for agent-fleet while preserving the local probe -> HTTPS ingest -> HK hub -> state/events data chain.

**Architecture:** Keep the Flask hub as the only backend and expose read-only, sanitized JSON for machines and recent events. Replace the inline HTML with a small static frontend served by Flask: an overview strip, machine status table, per-machine agent chain, system metrics, stale/error states, and responsive polling. The frontend never connects to agents directly and never receives session identifiers or file paths.

**Tech Stack:** Flask, Jinja2, vanilla HTML/CSS/JavaScript, Python standard library, unittest. No frontend build tool or third-party runtime dependency.

## Global Constraints

- Runtime architecture remains push-only: agents report with `POST /api/ingest`; the hub never runs commands on agents.
- Public API output must pass `report_schema.py` allowlists; no session IDs, display names, file paths, command lines, prompts, tokens, or raw collector output.
- Existing `/api/status` remains backward compatible for machine data consumed by the dashboard.
- Frontend polling must use `cache: no-store`, bounded rendering, and a visible stale/error state.
- Mobile layout must remain usable at 390px width; desktop layout must remain usable at 1440px width.

---

### Task 1: Extract the dashboard shell from `hub/web.py`

**Files:**
- Create: `hub/templates/dashboard.html`
- Create: `hub/static/dashboard.css`
- Create: `hub/static/dashboard.js`
- Modify: `hub/web.py`
- Test: `tests/test_frontend.py`

**Interfaces:**
- `GET /` renders `dashboard.html` with no machine-specific data embedded in HTML.
- `GET /static/dashboard.css` and `GET /static/dashboard.js` return HTTP 200.
- The template contains `data-testid="fleet-summary"`, `data-testid="machine-list"`, and `data-testid="agent-chain"` hooks for browser-free contract tests.

- [ ] Write failing Flask client tests asserting `/` references the CSS/JS assets and contains the three stable test hooks.
- [ ] Run `.venv/bin/python -m unittest tests.test_frontend -v`; expected failure because the assets do not exist.
- [ ] Move the current inline page into the template, retain refresh and scan controls, and load the static assets through Flask `url_for`.
- [ ] Add CSS for a restrained operational dashboard: summary strip, dense machine rows, status dots, metric values, and responsive single-column mobile layout.
- [ ] Add JavaScript that fetches `/api/status` every 15 seconds, renders loading/error/empty states, and updates the last-refresh timestamp without a full page reload.
- [ ] Run the focused tests and assert all asset responses return 200.

### Task 2: Add a sanitized recent-events API

**Files:**
- Modify: `hub/events.py`
- Modify: `hub/web.py`
- Modify: `report_schema.py`
- Test: `tests/test_frontend.py`

**Interfaces:**
- `GET /api/events?limit=50` returns `{events: [{ts, machine, changes, kind}]}`.
- Limit is clamped to `1..100`; malformed event lines are skipped.
- Event payloads contain only machine names, event kinds, change names, and timestamps.

- [ ] Write failing tests for limit clamping, malformed JSONL lines, and removal of `snapshot`, `session_id`, `file`, and `cmd` fields.
- [ ] Run the focused tests and confirm failure against the current API surface.
- [ ] Implement a read-only event tail reader with a fixed allowlist and newest-first ordering.
- [ ] Add the route without changing `/api/ingest` write semantics.
- [ ] Run the focused tests and verify a seeded event file produces the expected sanitized response.

### Task 3: Render the agent chain and machine detail views

**Files:**
- Modify: `hub/templates/dashboard.html`
- Modify: `hub/static/dashboard.js`
- Modify: `hub/static/dashboard.css`
- Test: `tests/test_frontend.py`

**Interfaces:**
- Each machine row renders the chain `Local probe -> HTTPS ingest -> HK hub -> state -> dashboard` with the machine's current heartbeat status.
- Agent rows render `hermes`, `claude_code`, `codex`, and `generic` from the existing `agents` map, including absent/error/ok states and count fields.
- A machine filter and agent-type filter work entirely on the already-fetched sanitized status payload.

- [ ] Write failing DOM-contract tests that assert the chain labels and all four agent types are present for a fixture payload.
- [ ] Implement deterministic rendering functions for summary counts, machine rows, agents, stale states, and system metrics.
- [ ] Add a selected-machine detail drawer/section that shows only allowlisted fields and a recent event list.
- [ ] Add keyboard-accessible filter controls and an explicit retry action for API errors.
- [ ] Run tests and manually inspect the generated HTML at 390px and 1440px using the local Flask test server.

### Task 4: Production hardening and frontend verification

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture-v3.md`
- Modify: `docs/HANDOFF.md`
- Test: `tests/test_frontend.py`, `tests/test_push_only.py`, `tests/test_regressions.py`

**Interfaces:**
- Dashboard documentation names `/api/status` and `/api/events` as read-only frontend inputs.
- The docs explicitly state that the chain remains probe push -> authenticated ingest -> HK state/events -> UI.

- [ ] Add tests for missing state, stale state, empty agent map, API 403/500, and event payload privacy.
- [ ] Run `.venv/bin/python -m unittest discover -s tests -v`.
- [ ] Run `python3 -m compileall -q connectors hub tools tests report_schema.py`.
- [ ] Run `git diff --check` and a source scan proving frontend/backend runtime code contains no SSH execution path.
- [ ] Start the local Flask server with a test token and verify `/`, `/api/status`, `/api/events`, CSS, and JS all respond successfully.
- [ ] Record desktop/mobile screenshots during implementation review; do not claim the frontend complete until both viewport checks pass.

## Self-review

- The plan preserves the existing ingest contract and does not add a central pull path.
- Sensitive collector details are blocked at both probe and hub boundaries.
- The frontend is split into template, CSS, and JavaScript files so the Flask backend remains focused on API/state behavior.
- The plan does not include command dispatch; push-only control remains intentionally out of scope.
