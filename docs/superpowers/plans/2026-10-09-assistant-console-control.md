# Assistant Console and Control Delivery Plan

**Goal:** Remove the blocking login-check interstitial, add a persistent draggable split between conversation history and compute nodes, provide owner-scoped conversation rename/archive/restore/delete operations, and expose the safe supported controls to the main assistant through the existing broker boundary. Permanent deletion stays behind an explicit operator confirmation.

**Architecture:** The Hub remains authoritative for operator identity, owner-scoped conversations, Runs, and policy. The browser may refresh its session display at most once per minute, while every API route keeps its existing server-side authentication. Conversation lifecycle changes stay in PlatformRepository and ConversationService; the UI uses the existing same-origin API boundary. Assistant management tools stay fixed-schema and owner-scoped inside ToolBroker. VM provisioning is not implemented without a real provider contract: existing Nodes are endpoints, and the repository has no cloud VM lifecycle adapter.

**Tech Stack:** Existing Flask application, SQLite platform database, ES modules, CSS, pytest, existing browser journey harness.

## Constraints

- Preserve the protected primary worktree and any unrelated worktree changes.
- Keep server authentication on every protected API request; browser polling changes only session-check cadence and presentation.
- Enforce owner scoping in every conversation query and mutation; return 404 for another owner's conversation.
- Default history shows active conversations. Archived history is explicitly requested, and permanent deletion is allowed only after archive.
- The assistant may list, rename, archive, and restore conversations; permanent deletion is not a model tool and requires sidebar confirmation.
- Permanent deletion is blocked by non-terminal Runs and unresolved linked commands, including commands with legacy `owner_id IS NULL`; terminal command children are cleaned atomically.
- Delete linked Run events, legacy task links, Runs, messages, browser metadata, and execution-window records atomically with the archived conversation. Workspace artifacts and usage accounting remain independently owned resources.
- Keep the main assistant's system controls fixed-schema, owner-scoped, lease-checked, bounded, and mediated by ToolBroker; do not add arbitrary shell, credential, provider URL, or host access.
- Do not misrepresent registered Nodes as VMs. Do not add an inert or fake VM create/delete implementation.
- Keep sidebar sizing in browser storage and support pointer, keyboard, and touch input without affecting mobile drawer behavior.

## Implementation

- [x] Add the conversation lifecycle schema migration, owner-scoped repository operations, application validation, and authenticated HTTP routes.
- [x] Add API tests for rename, archive/restore, archive filtering, archived-only deletion, active Run rejection, owner isolation, and transactional cleanup of related records.
- [x] Add archive navigation and row action menus with rename, archive/restore, and permanent delete; refresh both active and archived history after mutations.
- [x] Add a keyboard- and pointer-adjustable sidebar divider, with bounded persisted node-panel height and responsive fallback.
- [x] Change session presentation to remove the blocking verification page, run no more than one session check per minute, and keep API 401 logout handling intact.
- [x] Add fixed-schema conversation, defaults, and gated service-action tools to the local main-assistant broker and tests proving lease enforcement, owner isolation, revision fencing, and approval routing.
- [x] Update architecture and user-facing development notes with delivered control boundaries and the VM-provider limitation.
- [x] Run focused tests, browser regression, Python compile checks, JavaScript syntax checks, and git diff --check; the required-journey gate passed with 3,102 tests, 5 platform-conditional skips, 156 subtests, and 29/29 required journeys.

## Verification

Focused API and runtime regressions pass (56 tests), including a reproduced/fixed legacy-command owner gap. The local full gate passed before publication with 3,102 tests, 5 platform-conditional skips, 156 subtests, and 29/29 required journeys. The first Linux CI run exposed two test-gate issues: a duplicate PID-file test name left the matrix-required selector uncollected, and the browser search assertion failed once without enough DOM diagnostics. The PID-file assertions now live under the single required selector; the browser failure did not reproduce on the next complete Ubuntu run, and its assertion now prints the query, row data, hidden state, and computed display on failure. Python compilation, JavaScript syntax checks, and `git diff --check` pass.

## Production Release

Public `main` revision `a2146f07d4f576fa9066cc252bccf8508b719c88` was deployed by [CI 37869680567](https://github.com/dengyie/agent-fleet/actions/runs/37869680567). Ubuntu 24.04 completed 3,148 tests, 160 subtests, and all 29 required journeys with zero skips; the JUnit report contains 3,308 cases and zero failures or errors. Release-evidence validation, backend/frontend packaging, and restricted deployment all succeeded; `deployment.json` reports `deployed` for the exact revision. The public manifest reports the same SHA, `/healthz` returns 200, and an anonymous operator-session request returns 401. No real model task or VM lifecycle operation was run; browser and submit production gates remain disabled.
