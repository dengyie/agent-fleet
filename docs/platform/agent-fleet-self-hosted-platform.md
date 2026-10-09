# agent-fleet Self-Hosted AI Platform

> Status: architecture and phased delivery proposal. Historical implementation checkpoints below describe their original worktrees and verification states. The locally verified offline submit integration and completion-audit repairs are merged into local `main` at `3a76ce2`; this does not establish production deployment or complete the remaining real-browser and operations roadmap.

## Product Goal

Turn agent-fleet from an operator dashboard and remote task runner into a self-hosted personal AI work platform. The default experience is one assistant conversation, a selected model and workspace, and an execution window that shows durable progress, tools, and artifacts. The platform can route bounded work to the Hub host or to purpose-specific nodes and can relate those nodes to monitored services.

The control plane remains the authority for identity, conversations, Runs, policy decisions, grants, service definitions, and audit events. A model proposes work; deterministic policy and a broker decide whether that work may cross a boundary.

## Product Boundaries

- A ModelProfile selects a provider, model, budget, and secret reference. A Run freezes that selection when it is created.
- A Workspace is durable user data. A sandbox is an execution boundary mounted against that data; a directory or Git worktree alone is not a sandbox.
- A Node is an execution or observation endpoint with an owner, explicit capabilities, credentials, and an outbound connection. A role preset does not grant permissions.
- A Service is a declared process/application relationship and health policy. Host metrics, process state, application health, and external reachability remain separate evidence dimensions.
- An ExecutionWindow observes or attaches to a Run. Closing a browser window does not cancel the Run.
- Monitoring does not imply permission to restart. Any mutation must name a fixed action, exact target, current service version, and approval policy.

## Assistant Operator Controls and Conversation Lifecycle (2026-10-09)

The browser no longer blocks on a dedicated “checking login” page. It validates the session on entry and at most once per minute afterward; a protected API response of 401 still logs the operator out immediately. Every protected route retains server-side authentication. Feature data is mounted only after the entry check succeeds. The sidebar history/node divider supports pointer and keyboard resizing and stores its bounded height in browser-local storage.

Conversation history is owner-scoped. Active and archived lists use the existing bounded inbox query. Operators can rename, archive, and restore a conversation; permanent deletion is available only from archived history and requires an explicit confirmation in the sidebar. The assistant broker deliberately has no permanent-delete tool, so a model response cannot bypass that confirmation. Deletion is rejected while any Run is non-terminal or any linked platform command is unresolved, including `unknown`; legacy commands without an owner column are still associated through the owner's Run. Once safe to delete, one SQLite transaction removes the conversation, messages, Runs, Run events, legacy task links, linked browser sessions/tickets/approvals, execution-window records, platform commands, outbox entries, reconciliation records, and post-checks. Workspace artifacts and usage accounting remain workspace-owned records.

The local main-assistant broker now exposes fixed-schema, owner-scoped controls to inspect and revision-update model/workspace/execution-node defaults, list and manage conversations, and request service actions when the service-action gate is enabled. A restart request only creates the existing one-time owner approval grant; model output cannot approve it. These Hub-local management tools are withheld from remote Node Runs. Existing workspace tools remain constrained by the configured backend and sandbox policy.

The repository has no cloud VM lifecycle provider. A registered Node is an execution endpoint, not proof of a provisioned VM. The assistant therefore cannot create or destroy cloud VMs until a provider, resource identity contract, credentials, lifecycle API, and bounded approval/reconciliation policy are selected and implemented; exposing Node registration as VM lifecycle would violate this boundary.

## Reference Architecture

```text
Browser / PWA
  | same-origin API, durable events, reconnect cursor
Fleet Hub
  |-- identity, defaults, conversations, Run state, policy, audit
  |-- SQLite platform.db: owner leases, Run leases, commands, incidents
  |-- model adapter --> external or self-hosted inference
  |-- local worker --> workspace backend --> ToolBroker --> artifacts
  |-- outbox / signed command --> outbound Node poll + local journal
  |-- read-only connectors --> Komari, HTTP probes, service collectors
```

The default deployment keeps the Hub as a small control plane. Model inference can be remote. A Node can specialize as a read-only observer, document worker, coding worker, browser worker, inference worker, or approved operations worker. These roles can coexist on machines; they do not require one full LLM process per VPS.

Hub-to-Node connections remain outbound from Nodes. Commands are durable and signed. The Node checks owner, target, expiry, payload hash, local policy, and its journal before execution. Unknown side effects are reconciled or surfaced for a human; they are not blindly replayed.

## Durable Run Scheduling

The local multi-owner scheduler is an additive, default-off slice:

1. `platform_worker_owners` persists owner enablement, fairness timestamp, and a short scheduler claim lease. Eligible owners are chosen oldest-claim-first under `BEGIN IMMEDIATE`.
2. The existing `runs` lease remains the authority for a Run attempt. It records `lease_id`, `lease_owner`, expiry, and monotonically increasing attempt; stale attempts cannot append events or finish the Run.
3. `platform_worker_slots` persists process-shared global/workspace concurrency slots. Slot leases expire after a Hub crash and are reclaimed by later scheduler ticks.
4. A heartbeat renews the slot and Run lease during execution. Capacity deferral happens before the model/tool boundary and returns the Run to `queued` without consuming an attempt.
5. Defaults remain one global Run and one Run per workspace. The scheduler gate is separate from the single-owner worker gate and remains off unless explicitly enabled.

SQLite-backed slots coordinate processes sharing the same platform database; they are not a distributed consensus service. Keep a single Hub host and local SQLite storage. Move to a dedicated queue/database only after a measured multi-host or write-throughput requirement exists.

## Durable Read-only Schedules (M4.1)

M4.1 adds an owner-scoped schedule control plane beside the existing Run and monitoring schedulers. The platform_schedules table stores a fixed action (service_health or http_probe), a single service_id target, bounded interval (5s to 24h), IANA timezone, next_run_at, and explicit missed_policy/overlap_policy. platform_schedule_triggers uses (owner_id, schedule_id, scheduled_at) as its unique idempotency key.

The scheduler has a separate platform_schedules_enabled gate and creates no repository, route, or background lifecycle when disabled. A due occurrence is claimed with a SQLite transaction and an expiring lease; a crashed Hub leaves a reclaimable trigger. missed_policy=skip records one skipped occurrence, while catch_up executes at most the one current due occurrence and advances directly to the first future interval. overlap_policy=skip records a skipped trigger when another occurrence is active; coalesce records a coalesced trigger. Failed read-only checks use bounded exponential backoff, and stale workers cannot finish a reclaimed lease.

The operator API is GET/POST /api/platform/v1/schedules, GET/PUT/DELETE /schedules/<id>, and POST /schedules/<id>/run. DTOs hide owner and lease material; targets cannot carry URLs, commands, paths, credentials, or arbitrary tool arguments. The bootstrap executor only reads the current service health summary or the already configured HTTP probe evidence. This slice does not send model prompts, notifications, service mutations, VPS commands, or provider traffic.

The completion audit adds service-level crash recovery: live duplicate ticks preserve the current occurrence; expired triggers can be reclaimed even after `next_run_at` advances. Completion is fenced using the actual return time, so an expired or replaced worker cannot publish a terminal result. Pending definitions and expired occurrences are selected in one owner-scoped bounded query, independent of the public catalog page. The background lifecycle pages enabled owners by an indexed cursor, so catalogs larger than a single page continue to receive ticks; per-owner failures produce redacted diagnostics and do not skip the rest of a batch. Its stop handle can signal and join the daemon. See `tests/test_platform_schedule_recovery.py`; the gate remains default-off.

## Explicit Memory Items (M4.2)

M4.2 adds an owner-scoped `platform_memory_items` store and a bounded SQLite FTS5 index for explicit assistant memory. A MemoryItem has a fixed kind (`fact`, `preference`, `decision`, or `note`), title, bounded content, tags, source, enabled state, timestamps, and an optimistic revision. The public API is `GET/POST /api/platform/v1/memory`, `GET/PUT/DELETE /memory/<id>`, and `GET /memory/search?q=...`; update and delete require the current `If-Match` revision.

The memory gate is `platform_memory_enabled` and remains off unless the platform gate is also on. Queries are reduced to bounded plain terms before FTS matching, every read is owner-scoped, and DTOs omit owner and SQL/rank fields. This slice does not extract memories from conversation text, inject search results into model prompts, call providers, send notifications, or perform node/VPS actions. The next integration decision is an explicit context-selection policy for a Run, with user-visible evidence and a size budget.

## Open-Source Research (checked 2026-09-26)

| Project | Useful evidence | Apply to Fleet | Do not infer |
|---|---|---|---|
| [Meta Muse](https://about.fb.com/news/2026/09/introducing-muse-personal-ai-agent/) | Persistent personal assistant, dedicated execution environment, background goals, memory, approval checkpoints, credential isolation | Product interaction contract: one assistant, durable work, visible status, scoped approvals | Muse is not an open-source runtime or an assurance that an ordinary container is a secure VM |
| [nanoMuse](https://github.com/nano-muse/nanoMuse) | On-device personal agent, goals, routines, editable memory, approval cards, mobile interaction | Personal identity, readable memory, background goals and approval UX | Android/on-device implementation is not Fleet's server control plane |
| [muselab](https://github.com/hesorchen/muselab) | Self-hosted Agent workbench, workspaces, task center, execution timeline, files/previews, provider and MCP/Skills integration | Dense execution window and workspace navigation | Its Claude Agent SDK and terminal assumptions should not become Fleet's data/permission authority |
| [OpenHands Agent Canvas](https://github.com/All-Hands-AI/OpenHands) | Backend switching across local, Docker, VM, and cloud; multiple agents; automations; any-agent direction | Runtime/backend contract and separate UI/control plane from execution backend | Running without a sandbox gives the agent host filesystem access; backend switching is not itself isolation |
| [Synapse](https://github.com/zai-org/Synapse) | Conversations as collaboration boundary, remote agent daemon over outbound connection, workspace-level resource grants, scoped sandbox | Durable conversation wakeups, explicit grants, outbound Node protocol | Early design status and fast-changing contracts require independent validation |
| [Dify](https://github.com/langgenius/dify) | Self-hosted LLM app platform with model providers, workflow canvas, RAG, agents, and observability | Provider catalog and optional workflow integration patterns | It is not a VPS/service control plane and should not own Fleet Run or host authorization state |
| [Komari](https://github.com/komari-monitor/komari) | Dedicated self-hosted server metrics and fleet dashboard | Keep host-level metrics in the existing monitor; ingest as evidence | Host online/metrics do not prove application health or authorize actions |
| [Coolify](https://github.com/coollabsio/coolify) | Self-hosted multi-server application/service lifecycle and deployment management over SSH | Learn from resource inventory and operational UX; integrate by API/webhook if needed | Do not duplicate a full PaaS in Fleet or treat SSH access as the Fleet Node security model |
| [OpenClaw](https://github.com/openclaw/openclaw) | Gateway, channels, tools, and connected nodes as one assistant product | Optional runtime/connector evaluation | A host tool exposed outside a sandbox is not an acceptable default boundary |
| [LangGraph](https://github.com/langchain-ai/langgraph) | Durable stateful workflows and human-in-the-loop graph orchestration | Re-evaluate when real branching, joins, and long external waits are required | Checkpointing does not replace command idempotency, Node journals, or side-effect reconciliation |

The sources above were read from project/authoritative README or official product pages. This is architectural comparison, not a benchmark or security certification. Project status, licenses, and API compatibility must be checked again against the exact release before adopting code or protocol dependencies.

## Delivery Roadmap

| Milestone | Outcome | Current evidence / exit gate |
|---|---|---|
| M0: baseline and policy | Preserve legacy Fleet while recording platform boundaries and gates | Existing branch baseline and feature gates; user state kept in the primary worktree |
| M1: local assistant | Main conversation, ModelProfile/defaults, persistent Workspace, bounded runtime, Run events, artifact references, recoverable Conversation inbox, and an explicit legacy task bridge | Local API/runtime/worker slices exist; assistant lists recent owner-scoped conversations, conversation-scoped artifacts, previews safe bounded text, and can explicitly associate one Run with one legacy coding task; the disposable encrypted backup/restore drill is complete, while an operator-facing workflow and production recovery procedure remain open |
| M1.5: durable multi-owner scheduler | Fair owner selection, reclaimable owner lease, global/workspace concurrency slots, bounded lifecycle | Implemented in local `main`; focused tests and full test suite pass; scheduler gate remains off |
| M1.6: real provider | SecretBroker-backed provider factory, streaming/structured tools, timeouts, usage, errors, cancellation and token budget | Implemented in local `main`: `openai_compatible` Chat Completions adapter, injectable transport, SSE/tool delta accumulation, bounded usage/errors/retries, non-secret provider snapshot, and secret-redaction fixtures. Real urllib network access remains behind `platform_provider_network_enabled` and is off by default; no production endpoint has been exercised |
| M1.7: Run-to-Node delivery | ToolRequest -> durable command/outbox -> Node journal -> receipt -> Run event | Implemented in local `main`: RemoteToolBroker creates one fixed `tool.<name>` command per call, NodeToolExecutor maps only the workspace allowlist, durable receipts can become terminal `unknown`, and unknown remote receipts finish the Run as `unknown`; `platform_remote_execution_enabled` remains default-off |
| M1.8: unknown reconciliation | Owner-scoped inspection, bounded evidence, and a visible operator recovery path | Implemented as an audit-only contract: `GET /commands/unknown`, `GET /commands/<id>`, and `POST /commands/<id>/reconcile`; sensitive command internals stay redacted, command/Run status remains `unknown`, and the assistant UI exposes bounded evidence buttons. Deterministic post-check state transitions remain a later policy gate |
| M1.9: deterministic workspace post-check | Read-only digest evidence for ambiguous workspace writes | Implemented for `tool.workspace.write`: Hub derives the original path and SHA-256 privately, emits one fixed `reconcile.workspace.digest` command, and records `matched`, `mismatch`, or `remains_unknown`. The original command and Run remain `unknown`; a digest match is current-state evidence, not causal proof |
| M1.10: deterministic service inspect post-check | Read-only process-state evidence for ambiguous `service.inspect` receipts | Implemented with service-version/node/resource fencing and fixed `reconcile.service.inspect`; successful bounded inspect evidence feeds process-state health, while failures remain unknown. The original command and Run remain `unknown` |
| M1.11: disposable provider canary | Exercise the real stdlib `urllib` transport and worker/provider gate against a loopback fixture | Implemented in tests only: non-stream JSON, fragmented SSE/tool-call, bounded HTTP failures, retry/timeout classification, authorization placement, redaction, and a gated local worker Run; no production endpoint or credential used |
| M1.12: service logs post-check | Reconcile an ambiguous read-only `service.read_logs` command with current bounded log evidence | Implemented with fixed `reconcile.service.logs`, service-version/node fencing, adapter-owned log reader, Redactor and byte bounds; original command/Run remain `unknown` |
| M2.1: fixed service log adapters | Adapter-owned systemd/Supervisor/Docker log argv, bounded timeout and UTF-8 output contract | Implemented as a disposable local fixture; real Node/VPS wiring remains gated off |
| M2.2: disposable Node runtime fixture | Explicit capability-scoped Node assembly, real local subprocess shim, journal idempotency, Hub poll/receipt/post-check closure | Implemented in tests with temporary workspace, journal, PATH shims, and in-process Flask transport; production Node/VPS wiring remains gated off |
| M2.3: Komari loopback read-only canary | Independently gated urllib GET, bounded status/body/timeout mapping, token-safe snapshots, monitoring evidence closure | Implemented against a disposable `127.0.0.1` HTTP fixture; `40` focused tests pass; production Komari endpoint, token, VPS and background sync remain disabled |
| M2.4: inspect-capability Node fixture | Capability-scoped `reconcile.service.inspect`, adapter-owned inspect argv, real subprocess shim, journal idempotency and post-check health closure | Implemented with temporary `systemctl`/`supervisorctl`/`docker` shims; focused runtime/action/post-check regression passes; production Node/VPS wiring remains gated off |
| M2.5: Komari schema fixture contract | Versioned, bounded, sanitized `schema_v1` payload contract with stable malformed/unsupported error classes and Incident unknown evidence | Implemented with checked-in fixture and backward-compatible unversioned injected requesters; `25` Komari tests pass; private fork/export schema and approved read-only node pilot remain open |
| M2.6: platform backup and restore contract | Offline SQLite online-backup, artifact inventory, integrity validation, atomic new-target restore, and no-overwrite behavior | Implemented with a local WAL/artifact fixture; encrypted backup and retention contract plus the isolated pxed restore drill are complete; production off-host durability, key escrow/rotation, scheduled retention, and restore against the live deployment layout remain open |
| M2.7: budget telemetry and rate limits | Normalize provider usage, persist a Run usage summary, and enforce opt-in owner/model sliding-window request/token limits before provider calls | Implemented with a SQLite usage ledger, idempotent `(run, attempt, step)` settlement, unknown-provider accounting, and default-off admission; `10` focused usage tests and `1843` full regressions pass. Production budget values, retention, alerting, and cost reconciliation remain open |
| M2.8: encrypted backup retention and restore | Encrypt the validated platform DB/artifact pair, fence key identity, plan bounded retention, and prove atomic restore in a disposable drill | Implemented with streaming AES-256-GCM payloads, strict root/payload structure checks, key-id checks, tamper/wrong-key rejection, dry-run retention planning, and no-overwrite restore; `7` focused M2.8 tests pass and the combined backup regression is `14 passed`. Full repository regression is `1850 passed, 2 skipped, 18 warnings, 148 subtests passed`. No production key, off-host copy, or live restore was used |
| M2.9: read-only service monitoring UI | Add a static `/monitoring` shell route that renders bounded service health, recent evidence, and incident summaries through operator-only read APIs | Implemented in local `main` with `platform.js` wrappers, a normalized shared API base boundary, DOM-safe `monitoring.js`, four health dimensions, bounded 100/5/50 rendering, responsive layout, and cutover route wiring. Focused monitoring/release/cutover regression is `57 passed, 2 subtests`; local seeded browser walkthrough covers populated, mobile, error, empty, and detail-failure states; full repository regression is `1854 passed, 2 skipped, 18 warnings, 148 subtests passed`; no service action, approval, Komari sync, scheduler control, provider, VPS, or deployment was enabled |
| M2.10: versioned HTTP probe evidence | Add a second read-only evidence contract after the Komari schema boundary is frozen | Implemented in local `main` as `HttpProbePayloadContract(schema_version=1)` and `HttpProbeClient` with default-off urllib, exact GET path, no redirects/path normalization, bounded body and stable HTTP/transport errors. A sanitized fixture and real `127.0.0.1` loopback canary exercise evidence ingestion into `application_health` (`19` probe tests; no route, scheduler, or production endpoint) |
| M2.11: scheduled HTTP probe evidence | Connect operator-owned per-service HTTP policies to a durable, source-isolated scheduler | Implemented with `checks.http_probe` ownership validation, origin allowlist, DNS address classification, explicit disposable loopback override, bounded interval/timeout/TTL, `http_probe_sync` leases/backoff, `application_health` unknown evidence, source/rule Incident fingerprints, deduplication and recovery streaks. Focused scheduling suite has `7 passed`; scheduler/network/production endpoint gates remain off by default |
| M2.12: pinned HTTP probe connect | Bind the validated DNS answer to the actual TCP/TLS connection | Implemented with one resolver pass, normalized validated socket addresses, direct `socket.connect(sockaddr)`, original-host HTTP Host header and HTTPS SNI, and no hostname-based fallback. The intentionally unresolvable-host loopback regression passes; production network remains off |
| M2.13: HTTP probe endpoint rotation fencing | Prevent an observation from an old service policy from writing current health or recovering a new endpoint Incident | Implemented with service-version-tagged HTTP evidence, version-aware due checks, atomic repository version checks, stale in-flight result discard, and version-isolated HTTP Incident fingerprints. Focused health/probe/config regression has `99 passed`; all gates remain off by default |
| M2.14: Komari capture verifier | Validate a bounded, versioned Komari JSON capture offline and emit only normalized node evidence | Implemented with `verify_komari_capture_bytes/file`, the existing `KomariPayloadContract`/`normalize_nodes`, a `512 KiB` bound, UTF-8/JSON/schema checks, legacy unversioned compatibility, symlink-safe regular-file reads, and stable secret-free errors; capture/Komari regression is `39 passed`; no network client is constructed |
| M2: service observability | Service catalog, fixed collectors, Komari read-only evidence, health dimensions, incidents and diagnosis | Local slices exist; M2.3 validates the transport contract with a loopback fixture. A real Komari fork/export and read-only test-node pilot remain open |
| M3: controlled actions and window | Approval grants, service-version fencing, fixed adapter actions, reconnectable execution window | T3.1-T3.5 local control-plane/UI slices exist, including owner-scoped window recovery, leases, event cursors, and bounded monitoring action/approval UI; real PTY/browser and real service mutation remain off |
| M4.1: durable read-only schedules | Owner-scoped schedule/trigger persistence, bounded missed/overlap policies, reclaimable leases, fixed service health/HTTP probe actions, operator API | Implemented in local `main`; 8 focused schedule tests and 40 focused/platform regressions pass; `platform_schedules_enabled` remains off by default |
| M4.2: explicit MemoryItems | Owner-scoped CRUD, optimistic revision fencing, bounded SQLite FTS search, default-off memory gate and operator API | Implemented in local `main`; focused memory/config regression is passing; `platform_memory_enabled` remains off by default and automatic prompt injection is not implemented |
| M4: operations maturity | Notifications, memory, backup/restore, metrics, rate/budget controls, upgrade and rollback | Design/test gates required before production use |

## T1.5 Assistant Artifact Interaction

The assistant workspace now completes the first artifact interaction slice. A conversation's persisted `workspace_id` is the only scope used for `GET /workspaces/<workspace_id>/artifacts`; changing the workspace selector after a conversation exists cannot widen that scope. The API wrapper clamps the manifest list to 1..1000 and the content URL encodes both the opaque artifact id and workspace query.

The view renders only bounded public metadata: filename, size, content type, and a short SHA-256 prefix. Values are inserted through DOM text nodes, and the download anchor uses the same-origin content route with `download` and `rel=noopener`. The backend rechecks owner/workspace scope and content integrity before returning bytes, sends `Content-Disposition: attachment`, and rejects control characters in display names.

The local worker keeps its lease resource key (`owner:workspace`) separate from the ArtifactStore workspace id (`workspace`), so assistant-created artifacts are visible to the same workspace list route. Focused evidence is `43 passed, 2 subtests passed` for frontend/artifact/release contracts and `21 passed` for artifact/sandbox/worker integration. Browser E2E, real providers, VPS nodes, and all production gates remain disabled.

## T1.8 Bounded Artifact Preview

T1.8 adds a separate owner/workspace-scoped JSON preview route for `text/plain`, `text/markdown`, `text/csv`, and `application/json`. `ArtifactStore.read_preview()` first performs the complete SHA-256 and size check, then returns at most `64 KiB` of UTF-8 text with `truncated`, byte count, and content type metadata. Binary and other MIME types remain download-only; corrupt content remains a `409 artifact_corrupt`.

The Assistant adds a preview toggle only for the fixed allowlist. Preview text is rendered as a bounded `<pre>` text node, so HTML-looking payloads are displayed literally and cannot execute or create DOM elements. Loading, error, and truncation states stay inside the artifact row, and changing the persisted conversation workspace clears stale preview state. Focused artifact/sandbox/route/frontend evidence is `16 passed`; Node syntax, compileall, and diff checks pass. A temporary threaded loopback Flask app plus headless Chromium verified literal `<script>`/`<b>` text, exact `65536` preview bytes, truncation messaging, binary download-only behavior, and wrong-workspace `404`; no provider, VPS, Komari, scheduler, mutation gate, or deployment was enabled.

## T1.6 Explicit Legacy Task Bridge

The platform Run can now opt into one owner-scoped legacy coding task through `POST /api/platform/v1/runs/<run_id>/legacy-task`. The bridge stores a durable `legacy_task_links` outbox row in `platform.db`, moves only a queued Run to `waiting_task`, and emits private `legacy_task_queued` / `legacy_task_update` Run events. The legacy task database remains separate, so the bridge never pretends the enqueue and legacy create are one transaction.

Retries derive the same `legacy-run-<sha256(owner + NUL + run_id)>` client token. A transient failure releases the bridge lease; the next worker pass queries or creates the same legacy task instead of creating a duplicate. Claims, projections, and lease attempts are owner/run fenced, and a late projection cannot overwrite a cancelling or terminal Run.

The public projection is bounded to task id, current attempt id, an already persisted valid session id, task state, a 2000-character summary, diff metadata (`stat`, `has_patch`, `patch_bytes`), allowlisted test counters/names, and at most 50 result-file metadata rows. It never exposes the client token, lease owner, raw logs, full patch, storage paths, credentials, or a fabricated session deep link. The assistant shows the form only after the operator explicitly opens `关联旧任务`; it does not infer machine or project from model text.

Bootstrap wires the bridge only with the platform gate. `start_platform_worker()` drains at most one bridge row per cycle, and the multi-owner scheduler gets the same bounded drain hook; app construction itself starts no thread. Focused bridge/route/frontend evidence is `16 passed`; the full repository regression is `1915 passed, 2 skipped, 18 warnings, 148 subtests passed`, with the existing guardian async warnings unchanged. No real provider, VPS, Komari endpoint, production database, or deployment was used.

## T1.7 Durable Conversation Inbox

The Assistant now exposes an owner-scoped `GET /api/platform/v1/conversations?limit=` inbox. Each row contains only the conversation id, title, workspace id, a 240-character last-message preview, and a latest Run summary with id, state, and a 500-character result preview. The repository query is bounded to 100 rows and does not return owner identity, overrides, client tokens, full messages, event payloads, credentials, or storage paths.

The view renders the inbox through the existing `platformRequest` boundary and DOM text nodes. Each conversation links to the encoded `/conversation/<id>` route, the active conversation is marked locally, and a `新对话` link returns to the empty Assistant composer. Creating a turn refreshes the inbox so the current work remains discoverable after a browser reload or a closed execution window. Invalid or non-positive limits return `invalid_limit` with HTTP 400; valid limits are clamped to `1..100`.

Conversation overrides and immutable Run snapshots use strict JSON serialization. An omitted `overrides` field defaults to an empty object; an explicitly supplied HTTP value must be a JSON object, including when false-valued. Non-finite numbers and invalid Unicode are rejected as `invalid_value` before SQLite writes and return HTTP 400 without a conversation or Run row; Python's non-standard NaN/Infinity tokens are not accepted as JSON. The application, repository, and domain boundaries enforce the same mapping contract for non-HTTP callers.

Focused conversation/frontend evidence is `6 passed`; the broader platform conversation, worker, frontend contract, syntax, compile, and diff checks pass. A loopback Playwright walkthrough then verified the real cutover shell: create a conversation, submit a deterministic Run, observe `succeeded` and the `run_started`/tool/finish event chain, reload `/conversation/<id>`, replay the terminal Run events, and download an owner-scoped `assistant-report.md` artifact. A 390px mobile viewport had no horizontal overflow. The walkthrough exposed and fixed a Chromium-only `textarea.type` assignment error and added terminal Run event replay on recovery. This remains a local recovery contract only; no provider, Node, VPS, Komari endpoint, background thread, or production deployment was enabled.

## Security and Reliability Gates

- Default all new worker, scheduler, Node mutation, attach, and automation gates off.
- Store secret references in configuration, resolve secret values only in the runtime SecretBroker, and never put them in Run snapshots, API DTOs, events, logs, or model prompts.
- Use fixed tools and fixed action adapters. No model-authored arbitrary host shell, root shell, SSH command string, PTY attach, or browser takeover.
- Bound concurrency, requests, transcript sizes, provider timeouts, token budgets, artifacts, logs, and event payloads.
- Require idempotency keys for submitted turns and commands. A lost receipt for a possible side effect becomes `unknown` until inspected.
- Require a deterministic post-check before marking service recovery. A model diagnosis is a hypothesis with evidence references, not health truth.
- Before enabling a production action, test owner isolation, replay, stale lease, revocation, service-version change, timeout, process restart, and failed post-check paths.
- Back up `platform.db` consistently with its artifact store; restore-test the pair before any production rollout.

## M1.6 Provider Slice

Local `main` now contains `tools/platform/providers/openai_compatible.py`. It implements a narrow Chat Completions boundary rather than claiming every compatible API behaves identically:

- `ProviderFactory` recognizes only `deterministic` and `openai_compatible`; unknown providers fail closed.
- `EnvironmentSecretBroker` resolves only explicit `env://NAME` references in this slice. The raw key exists only in the provider instance while a request is in flight; it is absent from ModelProfile public DTOs, Run snapshots, events, and provider errors.
- `provider_config` persists bounded non-secret endpoint, timeout, retry, streaming, and safe-header settings. A Run freezes those values under `provider_snapshot`; it does not copy `secret_ref`.
- Non-streaming responses normalize assistant text, one structured function call, finish reason, and prompt/completion usage. SSE preserves chunk order, concatenates text and tool argument deltas, and handles usage frames.
- `401/403`, rate limit, transient HTTP, request rejection, timeout, network, invalid response, and oversized response have stable secret-free classes. Retries are bounded and limited to explicitly retryable classes.
- The standard-library urllib transport is blocked unless `platform_provider_network_enabled` is explicitly enabled alongside the platform worker. Ordinary provider tests inject a fake transport; the M1.11 canary is the sole exception and contacts only its disposable loopback fixture.

Focused provider/runtime/worker/scheduler/defaults/conversation/config regression: `40 passed` after endpoint admission checks; the earlier provider-focused slice was `47 passed`. Full repository regression at the M1.6 checkpoint was `1753 passed, 2 skipped, 18 warnings, 148 subtests passed`. This is adapter evidence, not a live provider or production readiness claim.

## M1.7 Run-to-Node Slice

Local `main` now connects the model ToolBroker boundary to the existing durable command plane without widening host authority:

- `RemoteToolBroker` accepts only the existing `ToolBroker.TOOLS` names and emits one `PlatformCommand` with an adapter-owned `tool.<name>` action, fixed node/resource/run binding, bounded arguments, and the command id as its idempotency key. Run events contain command, node, tool, and status metadata only.
- `CommandRepository.mark_unknown` is transactional. A terminal command cannot be overwritten; a non-terminal command is fenced as `unknown`, its lease is cleared, and it never returns to the queue. `CommandDeliveryService.wait_for_receipt` polls bounded durable state and converts timeout into that same terminal state.
- `NodeToolExecutor` maps `tool.workspace.list/read/write/exec` to the supplied backend. It does not build shell strings, accept `tool.shell`, or trust arbitrary action/argv fields. Backend execution keeps its existing executable allowlist.
- `NodeClient` preserves a fixed executor's explicit `succeeded`, `failed`, or `unknown` state in the journal and uploaded receipt. A missing/expired/unknown receipt maps to a remote `ToolReceipt(unknown)` and then a Run `unknown`; there is no automatic side-effect replay.
- `platform_remote_execution_enabled` is forced off unless `platform_enabled` is on. Runs with a non-local execution node fail closed when the gate or delivery adapter is unavailable; they do not silently fall back to the Hub filesystem.

Validation in this slice: focused remote/delivery/runtime/config tests `45 passed`; full repository regression `1764 passed, 2 skipped, 18 warnings, 148 subtests passed`. No real provider, Node, VPS, restart, PTY, browser, or production network was used.

## M1.8 Unknown Reconciliation Slice

The first reconciliation contract deliberately records evidence without pretending that an operator click proves a side effect. The command repository adds an owner-scoped reconciliation audit table; it accepts only `confirmed_succeeded`, `confirmed_failed`, or `remains_unknown`, with a bounded source (`operator`, `node_health`, or `workspace_check`) and bounded reference. The API never accepts raw result bodies, paths, argv, signatures, leases, or secrets. A command must already be `unknown`, and a duplicate or later evidence record does not make it queueable again.

The operator surface is `GET /api/platform/v1/commands/unknown`, `GET /api/platform/v1/commands/<command_id>`, and `POST /api/platform/v1/commands/<command_id>/reconcile`. Responses expose command/node/action/resource/run metadata and a bounded unknown reason; arguments and execution internals are omitted. The `/assistant` view renders the pending list and submits an operator evidence reference. The response explicitly reports `status_effect: audit_only`, and both the command and Run remain `unknown` until a future deterministic post-check policy is implemented.

An in-process Flask transport fixture now drives the complete disposable path: Run worker -> RemoteToolBroker -> Hub command -> NodeClient `/poll` -> NodeToolExecutor/DirectoryBackend -> `/receipts` -> Hub receipt -> Run events. It proves one command per tool call, no Hub-local filesystem fallback, terminal receipt persistence, and NodeJournal duplicate suppression without opening a network socket.

## M1.9 Deterministic Workspace Post-check

M1.9 adds a narrow post-check for ambiguous workspace writes. An operator can request `POST /api/platform/v1/commands/<command_id>/postcheck` only for an owner-scoped `unknown` `tool.workspace.write` command. The Hub reads the private command arguments, derives the expected UTF-8 SHA-256 and path, and stores a durable post-check descriptor. Client-supplied paths and content are ignored; the generated command id is deterministic and idempotent.

The Node receives only the fixed `reconcile.workspace.digest` action with `{path}`. `DirectoryBackend.digest` streams at most 8 MiB and returns only `path`, `sha256`, and `size`; file content never crosses the receipt boundary. A matching digest records `confirmed_succeeded` workspace evidence and post-check state `matched`. A mismatch records `remains_unknown` evidence with state `mismatch`; read failure, oversized file, or unknown receipt records `remains_unknown` with state `remains_unknown`. In every case the original command and Run remain `unknown`, because the current protocol cannot prove that the observed file was caused by the original write.

The assistant shows `检查工作区` only for unknown workspace writes and labels the result as current evidence. The API exposes only bounded post-check metadata and never exposes the expected digest or original content.

## M1.10 Deterministic Service Inspect Post-check

M1.10 extends the post-check plane to an ambiguous read-only `service.inspect` command. The Hub reloads the current ServiceDefinition and requires the original node, service resource, adapter, target alias, inspect action, and service version to match. A mismatch returns `service_version_conflict` and does not enqueue anything. The API never accepts an alias, version, executable, or argv override.

The generated command is fixed to `reconcile.service.inspect` and carries only the adapter-owned inspect envelope. The Node dispatches it through `ServiceActionExecutor`, whose adapter owns the executable and uses `shell=False`. Raw stdout is not returned in the post-check DTO or persisted health evidence. A bounded healthy, unhealthy, degraded, or unsupported result records `process_state` evidence and post-check `matched`; interrupted, timed out, or otherwise unknown observations remain `remains_unknown`. The original command and Run remain `unknown`, since the observation is not causal proof of the old command's execution.

The assistant exposes `检查服务` for unknown `service.inspect` commands. The service post-check record is owner-scoped, deterministic, terminal-idempotent, and uses one reconciliation evidence id. Remote execution, service actions, and provider network gates remain default-off unless explicitly enabled in a disposable environment. The M1.10 focused suite has `4 passed`; the full repository regression is tracked in the implementation plan.

## M1.11 Disposable Provider Canary

M1.11 exercises the actual standard-library `urllib` path against a test-owned `ThreadingHTTPServer` bound to `127.0.0.1` on an ephemeral port. The fixture is started and stopped inside each test, captures only bounded non-secret metadata, and never contacts an external endpoint. The provider network gate is explicitly enabled only for these disposable tests; the default `platform_provider_network_enabled` configuration remains off.

The canary covers a non-stream OpenAI-compatible response, authorization header placement, model/message/tool payload shaping, usage normalization, deliberately fragmented SSE with text and structured tool-call deltas, and `[DONE]` handling. It also covers bounded `401`, `429`, and `503` classes, one transient retry, and socket timeout classification. A socket timeout is normalized to the existing secret-free `timeout` provider error rather than leaking transport details.

The worker integration creates a temporary `openai_compatible` profile whose secret is resolved from an environment reference, runs one queued local Run with the provider gate enabled, and verifies that the Run snapshot, result, and events contain no secret. The canary never enables remote execution, service actions, scheduler, or production network access. Focused evidence is `7 passed`; full regression and static checks are recorded in the M1.11 plan.

## M2.1 Fixed Service Log Adapters

tools/platform/services/logs.py defines an adapter-owned FixedServiceLogReader for disposable Node fixtures. systemd, supervisor, and docker each have a fixed argv template; aliases, windows, timeout, and byte limits are bounded before subprocess.run(shell=False). Missing binaries, permission errors, timeouts, and OS failures have stable local error codes. Extra envelope keys such as argv are rejected. The reader is a contract and test fixture, not permission to run commands on a real VPS; production Node wiring and the remote execution gate stay off.

The M1.12 post-check path rechecks adapter and alias at evidence time, so a same-version catalog edit remains unknown. The original command and Run remain unknown in all outcomes.

## M2.2 Disposable Node Runtime Fixture

`tools/platform/node_runtime.py` now provides an explicit `NodeRuntimeConfig` and `NodeRuntime` assembly for a local execution Node. Configuration requires a node-scoped credential (`node_id:secret`), workspace root, journal path, Hub URL, and an immutable capability set. The public manifest contains only node id, worker id, and boolean capability names; it never contains credentials, host paths, or environment values.

The runtime composes `DirectoryBackend`, `NodeJournal`, `NodeToolExecutor`, `FixedServiceLogReader`, and `NodeClient`. Post-check actions are capability-gated before any reader runs; the M2.2 fixture explicitly enables `reconcile.service.logs`. Fixed adapter executable names remain owned by `FixedServiceLogReader`; a temporary PATH prefix is test-only and cannot provide an argv override.

The disposable integration creates temporary `journalctl`, `supervisorctl`, and `docker` shims, executes them through the real `subprocess.run(shell=False)` path, and drives Hub poll -> Node receipt -> service-log post-check through the in-process Flask transport. The receipt is redacted and byte-bounded, the original command remains `unknown`, and a duplicate delivery is suppressed by `NodeJournal`. No production service manager, VPS, provider endpoint, credential, or default gate is enabled. The M2.2 runtime suite has `7 passed`; the related adapter, post-check, Node, and collector regression has `43 passed`.

## M2.3 Komari Loopback Read-only Canary

`KomariClient` now has an independent `allow_network` gate. It defaults to `False`, is forced off unless both the platform and Komari gates are enabled, and fails with `network_disabled` before `urllib` can open a socket. Injected requesters remain available for deterministic unit and integration tests.

When explicitly enabled in a disposable environment, the client canonicalizes the configured HTTP(S) origin, joins only the configured absolute node path, and sends one read-only `GET` with `Authorization: Bearer <token>` and an `Accept: application/json` header. Query strings, fragments, credentials in the endpoint, and path guesses are rejected. Response bodies are bounded at `512 KiB` before JSON decoding; token, raw body, response headers, and HTTP error bodies never enter snapshots or exception text. Stable classes are `unauthorized`, `rate_limited`, `upstream_error`, `http_error`, `source_unavailable`, `response_too_large`, and `invalid_response`.

The canary uses a temporary `ThreadingHTTPServer` on `127.0.0.1` and an ephemeral port. It proves the real standard-library urllib path, request shape, node normalization, no-socket default-off behavior, 401/429/5xx/other HTTP mappings, oversized and malformed responses, transport failure mapping, token isolation, and Komari-to-Incident monitoring evidence. Focused Komari/config/monitoring/incident regression is `40 passed`. No production endpoint, real token, VPS, remote execution, service action, scheduler, or background network loop is enabled.

## M2.4 Inspect-capability Node Fixture

The capability-scoped `NodeRuntime` now assembles `ServiceActionExecutor` only when `reconcile.service.inspect` is declared. `service_path_prefix` is validated as a bounded test fixture input and is used only to prepend a temporary lookup directory to the subprocess environment; it never appears in the public manifest or receipt. The adapter continues to own `systemctl is-active`, `supervisorctl status`, and `docker inspect` argv, with `shell=False`, bounded timeout, and bounded health metadata.

The disposable fixture creates executable shims for all three adapters, runs a deterministic service post-check through `NodeRuntime.poll_once()` and the in-process Hub transport, and confirms `matched` / `healthy` evidence while the original command remains `unknown`. A runtime without the inspect capability returns `capability_unavailable` before subprocess lookup. `NodeJournal` suppresses duplicate delivery, and no raw output, credential, workspace path, or PATH value crosses the receipt/API boundary. Focused M2.4 inspect/runtime/action/post-check regression is `9 passed`; no production Node, VPS, service manager, or mutation was used.

## M2.5 Komari Schema Fixture Contract

`KomariPayloadContract` is now the boundary between a Komari response and node normalization. It accepts an explicit integer `schema_version: 1` envelope and the existing allowlisted containers (`nodes`, `clients`, `data`, `items`, and `results`), returns only bounded mapping rows, and caps the row count at 1000. Unknown fields are ignored before `KomariNodeSnapshot` construction, so fixture sentinels such as `secret`, `token`, and `raw_output` never enter snapshots, DTOs, errors, or Incident detail.

The contract fails closed for an explicit unsupported or non-integer schema version with `unsupported_schema`. Malformed containers, scalar payloads, error envelopes, and versioned payloads without a valid node row return `invalid_response` before normalization; they cannot silently become an empty healthy node set or `node_missing`. Existing unversioned injected requesters remain compatible while the private fork schema is still unknown. Incident synchronization records these bounded schema failures as `unknown` evidence with `collector_status=invalid_response`.

The checked-in JSON fixture is test-owned and sanitized. The Komari suite now has `25 passed`, including the real normalization and Incident path, unknown-field isolation, schema-version rejection, malformed response handling, and legacy fixture compatibility. This is a contract fixture, not evidence that it matches the private Komari fork: a captured export and an explicitly approved read-only test-node pilot remain required before enabling production network access or background synchronization.

## M2.6 Platform Backup and Restore Contract

`PlatformBackupService` treats `platform.db` and the content-addressed artifact store as one recoverable pair. It copies SQLite with `Connection.backup`, runs `PRAGMA integrity_check`, records only a versioned envelope with relative artifact paths, sizes, and SHA-256 hashes, and rejects symlinks, malformed manifests, path traversal, size/hash mismatches, and unbounded inventories.

Restore validates the complete source pair before writing anything, stages a fresh target sibling, revalidates the staged copy, and publishes it with a no-overwrite rename. Existing destinations remain untouched, and a corrupt database or artifact cannot create a partial restore. The local WAL/artifact round-trip and failure-path suite is `7 passed`; no route, scheduler, provider, network, VPS, Komari endpoint, secret broker, or production gate was enabled.

This proves the local data contract only. The combined regression is `90 passed`; the full repository is `1833 passed, 2 skipped, 18 warnings, 148 subtests passed`. Before production use, the operator still needs an off-host encrypted copy policy, retention and key rotation, a restore window, and a rollback drill against the real deployment layout.

## M2.7 Budget Telemetry and Rate Limits

The platform now records normalized provider usage without storing prompts, completions, endpoints, or secrets. `UsageRepository` adds four bounded SQLite tables: owner/model policies, short-lived admission reservations, settled provider events, and per-Run aggregates. Each provider boundary reserves one `(run_id, attempt, step)` key before calling the provider and settles it exactly once after a response. Duplicate admission or settlement returns the original reservation/event and cannot double count.

Policies can cap requests, total tokens, or both over a bounded sliding window. Admission runs inside `BEGIN IMMEDIATE`, reclaims expired reservations, counts committed events plus active reservations, and raises `usage_limit_exceeded` before provider transport when a limit would be crossed. Provider exceptions settle an `unknown` request with zero known tokens before the worker rethrows, so retries remain conservative. Provider totals are normalized to at least input plus output to avoid undercounting inconsistent upstream usage fields.

The usage ledger is initialized whenever the platform database is enabled. Telemetry remains available by default, while policy enforcement is disabled unless `platform_usage_limits_enabled` is explicitly set. Run terminal rows and public DTOs expose only `input_tokens`, `output_tokens`, `total_tokens`, and `provider_requests`; the detailed ledger remains owner-scoped. No new route, scheduler, provider endpoint, VPS connection, or production limit value was enabled.

The focused usage suite is `10 passed`; runtime/worker/conversation/config integration is `33 passed`; the full repository regression is `1843 passed, 2 skipped, 18 warnings, 148 subtests passed`. Before production use, choose owner/model budget values, retention and aggregation policy, alerting semantics, cost reconciliation, and an operator procedure for unknown provider calls.

## M2.8 Encrypted Backup Retention and Restore

`PlatformBackupService` now has an additive encrypted contract. `create_encrypted_backup` first creates the existing validated SQLite/artifact pair in a private temporary directory, then encrypts each bounded file with AES-256-GCM and publishes only `encrypted-backup.json` plus the `payload/` ciphertext tree. The public manifest contains a version, non-secret `key_id`, creation time, relative allowlisted paths, plaintext/ciphertext sizes, and SHA-256 digests; it never contains key material or plaintext content.

`inspect_encrypted_backup` can verify the public ciphertext hashes without a key, or decrypt into a private temporary directory and re-run the complete unencrypted backup validator with a key. `restore_encrypted_backup` rejects key-id mismatches, wrong keys, ciphertext tampering, symlinks, path changes, and existing destinations before publishing a new target with a no-overwrite rename. Temporary plaintext and partial files are removed on success and failure paths.

`BackupRetentionPolicy` is bounded by `keep_last`, optional age, and optional total bytes. `plan_retention` is always dry-run and returns only names, counts, bytes, and fixed skip reasons. `prune_retention(..., apply=True)` revalidates each encrypted marker immediately before deleting it; unmarked files, incomplete backups, and symlink entries are never selected. The encrypted root now rejects extra files/directories and symlinked markers before reading the manifest. The focused M2.8 suite has `7 passed`; together with the existing unencrypted backup tests the backup regression is `14 passed`.

This is a local format and disposable restore drill. No production key source, off-host encrypted copy, retention scheduler, VPS, provider, Komari endpoint, or live deployment directory was accessed. The full repository regression after the strict root-structure check is `1850 passed, 2 skipped, 18 warnings, 148 subtests passed`. A production rollout still needs an operator-owned key rotation/escrow procedure, off-host storage, retention schedule, and a restore window against the real deployment layout.

The 2026-09-29 pxed drill exercised the same contract against `/data/agent-fleet-platform-test/var/platform/platform.db` and an empty artifact catalog. It created an AES-256-GCM backup with a process-local key, verified the public ciphertext manifest without a key, decrypted and revalidated it with the key, restored to a new target, and confirmed a second restore returned `destination_exists` without overwriting the target. The source database hash was unchanged and the temporary key, backup, restore target, and helper were removed. The drill exposed and fixed an empty-catalog bug in `_decrypt_to_plain`: the required `artifacts/` root was not recreated when there were no artifact files. The regression now covers the zero-artifact round trip.

## M2.9 Read-only Service Monitoring UI

The static shell now exposes `/monitoring` when frontend cutover is enabled. The view calls only the operator-scoped `GET /api/platform/v1/services`, `GET /api/platform/v1/services/<service_id>`, and `GET /api/platform/v1/incidents` wrappers in `frontend/api/platform.js`; it does not call mutation endpoints or build API paths in the view.

`frontend/views/monitoring.js` renders service rows, overall health, the four health dimensions (`host_reachability`, `process_state`, `application_health`, `external_availability`), up to five recent evidence rows for the selected service, and up to fifty incident summaries. Service responses are capped at 100 rows. Malformed or failed responses render bounded loading/error/empty states and keep the rest of the shell usable. Dynamic values are inserted through DOM nodes and `textContent`; raw command data, credentials, paths, and process output are not part of the view contract.

The shell adds a `Service Monitoring` navigation item and breadcrumb plus a cutover-only `/monitoring` Flask route. CSS uses the existing design tokens and stacks the service/detail columns on narrow screens. `platformRequest` now normalizes paths built with `apiPath()` before dispatch, preventing a duplicated `/api/api/...` prefix when the default same-origin base is used. During the same full-suite verification, a deterministic reserved-marker regression in generated execution-window IDs was fixed by using hex IDs; the window ID now remains valid under the platform validator. This is a local read-only UI contract: no real Komari endpoint, VPS, provider, service action, approval grant, scheduler control, or deployment was contacted.

The browser walkthrough used temporary seeded stores on loopback only. It verified the populated service/detail/Incident view, active navigation, four dimensions and bounded evidence, a 600px single-column layout, monitoring-disabled error state, empty catalog state, and a detail request failure that preserved the service list. No fixture data contained credentials, paths, command arguments, or process output.

## M1.12 Service Logs Deterministic Post-check

M1.12 adds the next read-only evidence policy for an ambiguous `tool.service.read_logs` command. The Hub accepts only an owner-scoped unknown command, reloads the current ServiceDefinition, and fences the original target node, service id, adapter, target alias, and service version. It derives a deterministic `reconcile.service.logs` command with only the fixed `window_s` and `max_bytes` bounds; clients cannot provide an executable, argv, path, unit, or replacement service identity.

The Node accepts the exact log envelope and calls an injected adapter-owned reader. The returned text is passed through the existing Redactor and then bounded by UTF-8 bytes. The receipt carries only redacted text, truncation, bounded redaction metadata, and an optional observation timestamp. Reader interruption, malformed output, failed/unknown receipt, or a service-version change records `remains_unknown`; a successful current read records `matched` as current evidence. The original command and Run never change from `unknown`, and no service health dimension is inferred from log text.

The disposable in-process integration covers matching evidence, secret/path redaction, malformed envelopes, version fencing, owner isolation, duplicate reads, interrupted readers, adapter mutation, and target-alias mutation. The hardened M1.12 suite has `7 passed`; the related post-check and diagnostics regression has `15 passed`. M2.1 now supplies the adapter-owned systemd, Supervisor, and Docker contract, while production Node/VPS wiring and remote execution remain gated off.

## M2.10 Versioned HTTP Probe Evidence

The second read-only source is deliberately a small contract rather than an assumption about a third-party monitor API. `HttpProbePayloadContract(schema_version=1)` accepts only a `probe` object with a bounded id, one of `healthy`/`degraded`/`unhealthy`/`unknown`, optional HTTP status and latency, and an optional finite observation timestamp (the client supplies bounded current time when it is absent). Unknown fields, including secret/token/raw-output sentinels, are discarded before the immutable snapshot is exposed. Unsupported versions and malformed values become `unsupported_schema` or `invalid_response` without returning the payload.

`HttpProbeClient` treats the configured URL as an origin and joins one explicit path. It sends one `GET` with `Accept: application/json`, rejects query/fragment/path traversal and redirects, bounds the body at `64 KiB`, and maps 401/403, 429, 5xx, other HTTP failures, transport failures, malformed JSON, and oversized bodies to fixed secret-free codes. Injected requesters remain a deterministic test seam; the real urllib path requires `allow_network=True`, which has no FleetConfig or bootstrap wiring in this slice.

The sanitized `tests/fixtures/platform/http_probe_schema_v1.json` fixture is served by a temporary `ThreadingHTTPServer` on `127.0.0.1` and an ephemeral port. The canary asserts the exact path/header, normalizes the response into `application_health` evidence with source `http_probe_v1`, and persists only `probe_id`, status, and latency metadata. Focused probe/collector/service-health regression is `33 passed`, including `19` probe tests. No production endpoint, credential, external network, background scheduler, Komari sync, Node, service action, or deployment was used.

## M2.11 Scheduled HTTP Probe Evidence

HTTP probes now have an explicit service-owned policy under `checks.http_probe`. Registration normalizes one exact origin and path plus bounded `interval_s`, `timeout_s`, `ttl_s`, `probe_id`, and an opt-in `allow_loopback` marker. The application accepts a policy only when its origin is in the operator-provided allowlist; a model, HTTP request, node client, or scheduler call cannot supply or replace the URL. Non-HTTP service adapters cannot attach this policy.

Before the real urllib request, the scheduler resolves the configured hostname and rejects loopback, private, link-local, multicast, unspecified, and reserved addresses. A loopback origin is accepted only when both the stored policy and the disposable-fixture gate allow it. Query strings, fragments, encoded path escapes, duplicate slashes, traversal segments, credentials, redirects, oversized bodies, malformed schema, and stable HTTP/transport failures fail closed. The default app constructs no HTTP probe scheduler and opens no probe socket.

`HttpProbeMonitoringService` claims the durable `http_probe_sync` job, reloads each service policy, observes only due services, and applies bounded failure backoff. Every observation is written through `IncidentService` as `application_health` evidence with source `http_probe_v1`; a source failure is bounded `unknown` evidence. HTTP incidents fingerprint `service_id + source + rule`, so repeated failures deduplicate while Komari incidents remain independent. Healthy samples use the existing recovery streak and cannot close a Komari incident.

The disposable suite covers DNS/private-address rejection and explicit loopback, allowlist ownership, default-off bootstrap/status, exact loopback GET, evidence persistence, scheduler backoff, repeated unhealthy deduplication, healthy recovery, and Komari isolation (`7 passed`). Full repository regression is `1880 passed, 2 skipped, 18 warnings, 148 subtests passed`. This is a local contract and fixture only; real endpoints, production DNS pinning, off-host schedulers, VPS nodes, service actions, Komari sync, and deployment remain gated off.

## M2.12 Pinned HTTP Probe Connect

M2.11's address policy now feeds the connection itself. A network-enabled probe resolves its operator-owned hostname once, rejects every returned address that is loopback/private/link-local/multicast/unspecified/reserved unless the disposable loopback override is active, and normalizes the accepted rows to the configured port. The request adapter opens a socket with the resolved family and calls `socket.connect(sockaddr)`; it never passes the hostname to `socket.create_connection` or urllib after validation.

HTTP still uses the configured hostname in the `Host` header. HTTPS wraps the pinned socket with the original hostname as SNI and certificate-verification name, so direct-IP connection does not weaken TLS identity checks. HTTP status/error mapping, redirect denial, body limits, injected requesters, and public evidence contracts are unchanged. A disposable fixture uses an intentionally unresolvable hostname plus an injected resolver and proves a single resolver call reaches loopback.

M2.12 focused probe/scheduling regression is `27 passed`; the broader focused health/incident/config set is `50 passed`. This closes the local resolution-to-connect race contract but is not production network approval; endpoint rotation, resolver telemetry, real DNS, external endpoints, VPS nodes, Komari sync, and deployment remain off.

## M2.13 HTTP Probe Endpoint Rotation and Version Fencing

Service definitions already carry a monotonically increasing `version`; M2.13 makes that version part of the HTTP probe observation contract. The scheduler captures the current service version with the normalized operator-owned policy, and `application_health` evidence stores only the bounded integer `detail.service_version`. Evidence from a prior version is ignored by due checks and health aggregation, so a fresh sample from an old endpoint cannot suppress the first probe for a rotated endpoint.

The evidence repository checks `service_definitions.version` inside its SQLite write transaction. Incident writes perform the same version check, and HTTP Incident fingerprints include `service_id + source + rule + service_version`; Komari fingerprints remain unchanged. If an endpoint rotates while a probe is in flight, the result is returned as bounded `stale_policy`, no Incident is opened or recovered, and any sample written before the second fence is removed with a version-conditional cleanup. Stale observations do not increment scheduler failure backoff.

The disposable regression covers version-tagged evidence, old-version due behavior, in-flight rotation, post-evidence rotation cleanup, distinct version fingerprints, and existing M2.10-M2.12 behavior. The focused health/probe/config set is `99 passed`; after the T1.5 artifact slice, the full repository regression is `1898 passed, 2 skipped, 18 warnings, 148 subtests passed`. This is a local version-fencing contract only. Real endpoint rotation, DNS telemetry, production HTTP network, VPS nodes, Komari sync, provider network, and deployment remain disabled.

## M2.14 Komari Capture Verifier

`hub.integrations.komari_capture` adds an offline evidence gate for a sanitized Komari JSON capture. It accepts only bounded UTF-8 bytes or a non-symlink regular file no larger than the existing `KomariClient.MAX_BODY_BYTES` (`512 KiB`); an optional `schema_version` must be integer `1`, while legacy unversioned payloads keep the existing compatibility path. It delegates row validation/normalization to the existing `KomariPayloadContract` and `normalize_nodes`. The report is fixed to `ok`, `schema_version`, `observed_at`, `node_count`, and normalized `KomariNodeSnapshot` dictionaries; secret, token, raw output, envelope fields, request URLs, and exception text cannot cross the boundary.

File verification uses `lstat`, `O_NOFOLLOW` where available, `fstat`, and a bounded single read. Stable errors are `invalid_capture`, `unsupported_schema`, `capture_too_large`, `invalid_capture_path`, and `capture_read_failed`. The helper never instantiates `KomariClient`, calls `urlopen`, or changes `komari_network_enabled` / `komari_sync_enabled`. The fixture and Komari regression is `39 passed`.

This proves only that a supplied, sanitized capture satisfies the local schema and normalization contract. It does not prove compatibility with the private Komari fork/export format, node identity mapping, endpoint behavior, or production monitoring. The next evidence gate is an operator-obtained sanitized export followed by a separately approved read-only test-node pilot; production network and background synchronization remain off.

## M4.3 Explicit Memory Context Selection

M4.3 connects the explicit M4.2 MemoryItem data plane to the main conversation without turning memory into hidden prompt state. platform_memory_context_enabled is a separate default-off gate and is effective only when both platform_enabled and platform_memory_enabled are enabled. A turn must send memory_context.enabled=true and exactly one selector. The alternative selector is query, which uses the existing owner-scoped FTS search and freezes the bounded result set before the Run is inserted. IDs, query results, and optional revision fences are resolved for the authenticated owner only.

max_items is bounded to 1..20 and max_bytes is bounded to 256..32768 UTF-8 bytes. The resolver truncates only the final selected item at a valid UTF-8 boundary and stores the bounded item DTO, revision, truncation marker, item count, and rendered byte count in the immutable Run config snapshot. A later MemoryItem update cannot change a queued or reclaimed Run.

The local worker rebuilds one reference-only system message from that snapshot and prepends it to the copied conversation messages. The framing tells the model to treat the data as reference material and not follow instructions found inside it. A defensive worker-side size/evidence check fails the Run before provider transport if a snapshot is malformed or over budget. The private memory_context_selected event records only mode, bounded memory IDs, item count, rendered bytes, and configured budgets; it never stores title, content, tags, query text, or owner identifiers. The public Run DTO exposes only the same counts and budgets.

M4.3 is explicit selection and replay evidence only. It does not extract memories from conversations, automatically search on every turn, inject memory into old Runs, or grant MemoryItems permission to call providers, mutate a workspace, or reach a VPS. The context gate, provider network, remote execution, service actions, schedules, and production deployment remain independently gated. Focused resolver/config/conversation/worker validation is recorded in the M4.3 plan; full regression is required before merge.

## T3.4 Assistant Execution Window UI

T3.4 connects the existing default-off ExecutionWindow control plane to /assistant without introducing a PTY, WebSocket, browser takeover, host command, or VPS mutation path. The new owner-scoped GET /api/platform/v1/execution-windows?run_id=&limit= collection is bounded to 50 rows, optionally filters by Run, expires stale rows before projection, and returns only public window metadata. It exists so a page refresh can recover the latest window instead of relying on browser memory.

frontend/api/platform.js now exposes bounded wrappers for create/list/get, reconnect, one-use ticket attach, writer acquire/renew/release, close, and event reads. After a turn is accepted, /assistant creates the window, redeems the attach ticket, and attempts a holder-specific writer lease. On recovery it lists the latest Run window, reconnects for a fresh ticket, attaches it, and reacquires the lease. A lease conflict leaves the UI in read-only mode while event polling continues.

Run events and window events have separate cursors. Window reads use the durable after cursor and render only status, text, notice, input_ack, and output fields through DOM text nodes. Lease renewal is bounded and failure downgrades to read-only; explicit close releases the lease when possible and closes only the window. It never calls cancelRun, and no unload handler cancels a Run. The assistant shows loading, empty, recovering, read-only, writable, error, and closed states.

Focused execution-window/frontend evidence is 17 passed; compileall, Node syntax checks, and git diff --check pass. This is a local control-plane/UI contract only. execution_windows_enabled remains default-off, and real PTY/browser attach, host process control, remote VPS operations, provider networking, and deployment remain unimplemented.

The October 6 completion audit adds authenticated HTTP regressions for oversized numeric cursors/TTLs, malformed Unicode event text and credentials, and explicit non-object event payloads. These inputs now return their existing bounded 400 error codes before persistence instead of 500 or a false success. Event cursors are restricted to `0..9007199254740991` for exact JavaScript/JSON round trips; TTL, event-byte and page limits are unchanged. Rejected requests preserve the current writer lease and do not reserve event sequence/idempotency entries. Application errors retain their repository and conversion causes while public responses stay bounded. The focused window/frontend/conversation suite passes 45 tests; full-revision verification is recorded in the development-completion plan.

The October 7 audit also makes window metadata serialization strict JSON: NaN and positive/negative Infinity return `invalid_metadata` (HTTP 400) before a window row is created. The default Python encoder previously accepted these non-standard constants. WINDOW-01 now covers the repository and authenticated HTTP boundaries; full-revision verification remains tracked in the development-completion plan.

## Backup and Rollback Drill Evidence

The isolated pxed pre-release exercise is complete. It proves the encrypted backup/restore contract on the deployed test database, including an empty artifact root, public-manifest inspection, authenticated plaintext validation, atomic publication, source immutability, and no-overwrite behavior. It does not prove off-host storage durability, operator key escrow or rotation, retention scheduling, or rollback against HK production.

## Next Engineering Slice

 T1.7 Conversation inbox, T1.8 artifact preview, M2.14 offline Komari capture verification, M4.4 assistant MemoryItem context UI, T3.4 assistant execution-window UI, and T3.5 monitoring service-action UI are complete in local `main`. The isolated pxed encrypted backup/restore and rollback drill is also complete, including the zero-artifact recovery regression. T3.5 adds only a bounded operator UI over the existing M3 action contract: `inspect` can be queued when explicitly allowed by the service definition; `restart` creates a one-time approval grant and requires an explicit approve/reject decision. The page polls approval state at most five times and keeps the read-only health surface usable when the action gate or route is unavailable. The next evidence gate is an operator-obtained sanitized export and separately approved read-only Komari/HTTP probe pilots against explicitly approved test nodes. Production still needs operator-owned key escrow/rotation, off-host backup retention, a real rollback window, and explicit test-node approval. Keep production provider, `komari_network_enabled`, `komari_sync_enabled`, HTTP probe network, remote execution, service actions, schedules, memory, and execution-window gates off until budget values, rate-limit alerts, backup retention, key rotation, endpoint rotation, revision conflicts, lease recovery, approval expiry, and operator rollback are exercised.

## T3.5 Monitoring Service Actions UI

The `/monitoring` detail view now exposes bounded controls only when the service DTO declares `allowed_actions`. `inspect` sends no user arguments and reports only public command metadata after the existing fixed-adapter policy accepts it. `restart` sends a browser idempotency key, renders the pending one-time grant, and requires an explicit `approve` or `reject` click; the UI never treats chat text, Incident state, or model output as approval.

Approval status is polled through the owner-scoped approval route with a fixed five-attempt budget and bounded delay. `consumed`, `rejected`, and `expired` are terminal UI states; an unavailable route, gate-closed `404`, malformed response, or exhausted poll budget becomes a local notice and leaves service health, evidence, and Incident summaries usable. Dynamic values use DOM text nodes, and the UI never renders argv, raw output, credentials, leases, or secret references. This is a local UI contract only: `service_actions_enabled`, remote execution, real VPS restart, provider network, and deployment remain default-off.

## T3.6 Browser Node Data-plane Contract

T3.6 adds a default-off browser contract to the existing Hub/Node command plane. It does not create a second bridge, reverse SSH path, browser sidecar, or central credential store. The Hub remains the control plane for Run, command, lease, policy, artifact ticket, and durable session metadata; the Node remains the browser execution data plane and connects only through the existing poll/receipt/push transport.

The effective gates are `platform_browser_enabled` and `platform_browser_network_enabled`, each forced off unless the platform gate and its parent browser gate are enabled. External navigation additionally requires an exact configured origin, an HTTP(S) URL without embedded credentials, and global DNS/IP addresses. Loopback is allowed for disposable local fixtures. The later T3.7 slice implements redirect-chain validation and DNS-to-sockaddr pinning in the offline transport contract; that transport is not wired into a real browser driver, so browser-process egress enforcement and production network enablement remain out of scope.

The only browser capability is `browser.session`. It expands to the fixed browser tool allowlist only when the Node is enabled and has a configured driver; a declared capability without a driver is omitted from the runtime manifest and local tool allowlist. Remote Run assembly checks the selected owner-scoped Node catalog entry before exposing browser tools to the model; the local runtime repeats its own availability check and has no Hub workspace-executor fallback. Arbitrary JavaScript, CDP evaluation, shell, raw commands, host PIDs, profile upload, cookies, credentials, and secrets are not part of the contract.

Hub admission and Node execution both validate bounded browser arguments. Deterministic policy/backend errors are terminal `failed` receipts with stable codes; an exception after dispatch whose outcome cannot be determined remains `unknown` and is never automatically replayed. Screenshot output must be a real PNG with the `89 50 4e 47 0d 0a 1a 0a` signature and is limited to `256 KiB`. It is uploaded through a one-time ticket using the node credential and separate upload headers, then replaced by bounded artifact metadata before journal, receipt, Run event, or model serialization. The ticket stores only a token digest and enforces owner/workspace/run/node/command scope, expiry, size, hash, idempotency, reset, and consumed replay semantics.

A successful `browser.open` registers an opaque durable session through a Node-authenticated, leased, command-bound endpoint. Hub derives owner, workspace, Run, and target Node from the command; the Node cannot choose those scopes. Locally, the Node binds each opaque session to the signed command `run_id` and rejects cross-run reuse. If Hub registration fails after local open, the Node closes and removes that driver session while the command remains `unknown`. `browser.close` is terminal-idempotent, and Hub rejects re-registration of a terminal session. Session metadata excludes profile paths, CDP endpoints, cookies, credentials, and secrets. `LocalBrowserBackend` is an injected driver contract used by tests and local assembly; result values have aggregate byte and element-count budgets as well as per-field/depth bounds. Malformed URL parsing is rejected as deterministic `invalid_url`. In-process HTTP regressions cover command/action/worker/lease/expiry scope, ticket replay/concurrency/cleanup, and invalid or oversized uploads. No real Chromium/Playwright or Browserbase adapter is implemented, and no production browser gate is enabled.

The implementation is confined to `hub/config.py`, `hub/web.py`, `hub/bootstrap.py`, `hub/application/run_worker_service.py`, `hub/http/node_routes.py`, `hub/infrastructure/browser_repository.py`, `tools/platform/browser_policy.py`, `tools/platform/browser_url.py`, `tools/platform/browser_backend.py`, `tools/platform/browser_transport.py`, `tools/platform/remote_tool_broker.py`, `tools/platform/tool_broker.py`, `tools/platform/node_executor.py`, `tools/platform/node_client.py`, `tools/platform/node_runtime.py`, `tools/platform/artifacts.py`, `tools/transport.py`, and their focused tests. Fresh focused evidence covers browser policy/backend, remote admission, capability visibility, artifact upload, session lifecycle, and the offline transport contract. The offline transport tests now include shared canonical URL/authority normalization, explicit empty/zero-port and duplicate-origin rejection, canonical redirect-loop detection, unsupported/overlong Location rejection, a real loopback TLS handshake with test CA/SAN validation, SNI and pinned-port assertions, untrusted-CA/hostname-mismatch rejection, exact redirect/Location and malformed-authority boundaries, representative unsafe address classification, same-resolution connection fallback without re-resolution, request-to-request pin refresh, the 17th-request concurrency boundary, advancing-clock proof that DNS, connect, TLS handshake, redirect, header, and body phases share one absolute deadline, duplicate request/response header semantics, real-parser header-budget boundaries, cross-origin header stripping, bounded transport/Node receipt error secrecy, expired-certificate rejection and two-way IP-literal identity checks with the local test CA (a matching IP SAN is accepted without SNI; a DNS-only certificate is rejected for an IP-literal host), an in-process spy proving no OS hostname resolution beyond the injected resolver across request, redirect, and real-TLS paths, end-to-end marker traces proving URL/IP/Location/body/TLS failure markers and screenshot pixels stay off Node journals, Hub command receipts, Run events, model transcripts, artifact registry rows, Hub response bodies, and the captured log stream, while page content and uploaded bytes remain confined to their approved data planes (model transcript, command receipt, Node journal, wire receipt, artifact store). The execution-window continuation now also provides bounded `browser.frame` artifact/event publication and writer fencing with Take Control/Return Control UI coverage. T3.8-T3.9 observation policy, Browserbase, SecretBroker integration, real browser lifecycle, process-level egress enforcement, and production deployment remain unimplemented. T3.7 is approved for the offline policy/pinned-transport contract only: [T3.7 specification](../superpowers/specs/2026-10-04-t3.7-browser-navigation-network-boundary.md). The specification tracks individual offline tests as covered, partial, or blocked; it does not claim the full acceptance matrix passes. The public transport constructor fails closed without a bounded resolver; resolver/socket/connection seams are private test fixtures and are not wired into `NodeRuntime`. Hub admission URL checks are an early precheck and are not transport enforcement evidence. Real browser integration remains blocked because browser-wide request interception and process-level socket enforcement are not proven. This status does not authorize external network access, gate activation, production credentials, or deployment.

## M4.4 Assistant Memory Context UI

The `/assistant` view now exposes an explicit, per-turn MemoryItem picker. It loads the authenticated operator's bounded MemoryItems through `GET /api/platform/v1/memory?limit=50`, supports a user-entered search through `GET /api/platform/v1/memory/search?q=...&limit=20`, and renders loading, empty, error, selected, remove, and mobile-responsive states using DOM-safe text nodes. Search responses are fenced by a local request sequence so an older response cannot replace a newer query.

The picker keeps only reference fields (`memory_id`, `title`, `kind`, and `revision`) in browser state; returned MemoryItem content is discarded before rendering. When the checkbox is enabled and at least one item is selected, the next turn sends an opt-in payload like:

```json
{
  "text": "检查默认工作区状态",
  "client_token": "...",
  "memory_context": {
    "enabled": true,
    "memory_ids": ["memory-policy"],
    "revisions": {"memory-policy": 2},
    "max_items": 8,
    "max_bytes": 8192
  }
}
```

The UI enforces the M4.3 item budget of at most 8 selected references by default, while the server remains authoritative for the 1..20 and 256..32768 bounds, owner scope, enabled state, and revision fence. Memory content is never copied into the turn request, event list, or public Run DTO. Disabling the checkbox leaves the picker available for inspection but omits `memory_context` from the turn. If the MemoryItem gate is disabled or unavailable, the assistant keeps its normal conversation composer and shows a bounded error state.

## 2026-09-29 Review Remediation Contract

The local integration branch `codex/platform-review-fixes` combines the platform snapshot with `codex/root-cause-fixes` at `15c41a7`. The original source worktrees and deployed instances are not changed by this work. The [initial repair evidence](../superpowers/plans/2026-09-29-review-remediation.md) and [production review fixes and verification](../superpowers/plans/2026-09-29-production-review-fixes.md) track the regression cases.

- **Local execution:** the worker uses `SandboxBackend`. With no launcher, `workspace.exec` returns `sandbox_unavailable`; confined directory list/read/write, quota checks and artifact publication remain available. Supply an administrator-owned, independently validated isolation wrapper through `FleetConfig.platform_sandbox_launcher` or `AGENT_FLEET_PLATFORM_SANDBOX_LAUNCHER` as a JSON argv array. The wrapper receives the tool argv with the workspace as its working directory and must enforce filesystem/network isolation itself. Merely configuring an executable is not evidence of production isolation; no production wrapper is installed by this repair.
- **Interrupted Runs:** a durable `run_started` event precedes all provider/tool calls. Expired claims that crossed that boundary become terminal `unknown`, including cancellation/crash races; no tool or provider request is replayed. A cancellation request also cannot replace an `unknown` remote execution outcome: if a Node writes successfully but its receipt is lost, both Command and Run remain `unknown`, with the cancellation request recorded separately. Claims that never began execution can still be reclaimed. This is conservative recovery, not checkpoint-based continuation.
- **Backup consistency:** artifact publication and backups share the sibling kernel lock `.<artifact-root-name>.snapshot.lock`. Keep that file in place. Under the lock, backup captures SQLite first, then inventories and copies immutable artifacts. Future artifact deletion/GC must use the same barrier. Writers publish artifacts before committing references. This guarantees the backup contains the artifact closure of its database snapshot, while allowing unreferenced artifacts. Quiesce old binaries before using this new online backup contract; older writers do not participate in the barrier.
- **Code rollback:** both rsync and minimal-image paths restore release code while preserving current `state`, `var`, `credentials`, `hosts.yaml`, and the bind-mounted LIVE directory. Copying live runtime files into a code snapshot is not a consistent database backup. Data recovery is a separate operation requiring stopped writers and a verified backup.
- **Node trust and execution:** `NodeRuntimeConfig.public_key` accepts raw 32-byte Ed25519 public-key bytes. The assembled `NodeClient` always requires a valid signature; no key means every command is rejected before the journal or executor. Configure the matching Hub signing key before enabling delivery. Explicitly empty capability sets permit no workspace tools. Node execution also uses `SandboxBackend`; a signed, capability-authorized `workspace.exec` still returns `sandbox_unavailable` without `NodeRuntimeConfig.sandbox_launcher`. This immutable administrator-owned argv uses the same isolation contract as the local worker; confined file operations do not require a launcher.
- **Conversation ordering and rollback:** platform schema version 2 assigns `messages.turn_sequence` through a SQLite insert trigger in the same transaction as the user turn. Both current writers and the supported pre-sequence rollback writer omit the column and receive the next per-conversation sequence. Migration preserves existing insertion order, appends any default-zero rows left by an earlier rollback after existing positive sequences, and retains the unique order index. Startup rejects future schema versions before schema migration. Workers serialize turns within a conversation and cut inputs off at the trigger sequence; different conversations remain independently claimable.
- **Run results:** prior successful assistant answers are projected from `runs.result_text`, so UI/history and execution share one stored answer. `run_finished` carries state, steps and optional usage metadata, never the full answer. Long CJK, emoji or JSON-escaped answers therefore cannot overflow the event JSON limit of 64 KiB and turn a successful completion into a failed Run.
- **Replay and submission:** the unified static console uses `frontend/assets/realtime/sse.js` and `FleetStore`. Failed sources close before reconnecting with the latest sequence; replay pages to a fixed watermark and deduplicates overlap. The old SSR renderer, its timestamp cursor and duplicate polling lifecycle are deleted. The client stops on page hide and restarts after bfcache restoration. The composer retains an uncertain submission’s token, model override and memory selection for retry, locks editable submission controls while pending, and shows the Run’s actual model profile. The server reconciles existing tokens before revalidating mutable catalogs.
- **Release closure:** `deploy/package-release.sh` includes the required top-level `platform_schema.py`; clean extracted releases must start with platform gates either disabled or enabled. The local smoke copy includes that module and the shared `frontend` assets. Packaging reads committed `HEAD`, so final release verification must run after the repair commit as well.

These changes are verified with disposable local databases, local HTTP fixtures, subprocesses and failure injection. HK rollback, off-host backup durability/key escrow, and a production isolation launcher remain deployment validation work; all production feature gates retain their defaults.

## Unified frontend console (2026-09-29)

The UI now has one static entry with external ES modules, responsive navigation, light/dark/system themes and adapted awesome-ui components. API clients and the bounded shared store remain separate from view rendering. The Hub optionally serves identical static bytes; it no longer renders templates or injects business data. Production backend artifacts exclude frontend files and use `--no-serve-frontend`; a separate frontend artifact is served by the same-origin edge. Guardians check `/api/status` independently of the frontend. Markdown is parsed and sanitized locally; no runtime CDN is needed.

See [release/development contract](../../deploy/frontend-release-layout.md) and [implementation and verification plan](../superpowers/plans/2026-09-29-frontend-console.md). Earlier milestone descriptions above record historical implementation stages; their cutover paths are superseded by this contract. This is a local implementation, not a production deployment.

## References

- [Meta Muse announcement](https://about.fb.com/news/2026/09/introducing-muse-personal-ai-agent/)
- [nanoMuse README](https://github.com/nano-muse/nanoMuse)
- [muselab README](https://github.com/hesorchen/muselab)
- [OpenHands README](https://github.com/All-Hands-AI/OpenHands)
- [Synapse README](https://github.com/zai-org/Synapse)
- [Dify README](https://github.com/langgenius/dify)
- [Komari README](https://github.com/komari-monitor/komari)
- [Coolify README](https://github.com/coollabsio/coolify)
- [OpenClaw README](https://github.com/openclaw/openclaw)
- [LangGraph README](https://github.com/langchain-ai/langgraph)


## Browser Submit Approval Contract (offline)

The continuing [development completion plan](../superpowers/plans/2026-10-06-development-completion.md) keeps all outstanding development streams visible. Its first review reproduced Node journal replay on crash/concurrent redelivery and browser outcome budget/serialization gaps. Node admission now uses a short SQLite transaction and returns one execution claim; a previously running command becomes durable `unknown` without replay, terminal outcomes are immutable, and the client returns the persisted outcome even when its original execution finishes late. The initial journal metadata preserves a submit approval ID through interruption. Database connections close at the transaction boundary, before driver execution.

Browser structured outcomes retain the 16 KiB, depth/item/node ceilings. Byte accounting includes compact UTF-8 JSON quotes, escapes, commas and colons; scalar encoding is bounded before allocation, without serializing an entire tree just to discover overflow. Non-finite floats, invalid Unicode, non-string keys and raw bytes in structured results fail with `invalid_backend_receipt`. Integers outside JavaScript's safe range must be represented as decimal strings by the driver. The separate screenshot path requires PNG-signature bytes and retains its 256 KiB ceiling; structured strings/maps cannot be reported as successful screenshots.

The [submit approval specification](../superpowers/specs/2026-10-05-browser-submit-approval-contract.md) is implemented as a default-off, offline-verifiable extension of T3.6/T3.7. Owner grant/revoke APIs bind durable approvals to the exact run/session/selector. Consumption, final command signing, command persistence and outbox insertion share a transaction; Node dispatch validates the signed approval ID and retains it in journals, receipts and Run events, including unknown outcomes. Scoped lifecycle cleanup and fixed TTL/rate bounds are enforced by the SQLite store.

`AGENT_FLEET_PLATFORM_BROWSER_SUBMIT_ENABLED` defaults off and requires the browser/network parent flags; model visibility also requires the Node's submit capability. Local direct submit is disabled. The pinned transport permits one bounded form POST only within an approved driver dispatch, never follows POST redirects and retains T3.7 budgets. See specification §9 for the complete regression mapping and API details.

This is an injected-driver contract, not a real-browser adapter or production browser release. Browser process egress enforcement, credential use, UI takeover and deployment remain out of scope; no runtime gate is enabled by this implementation.
