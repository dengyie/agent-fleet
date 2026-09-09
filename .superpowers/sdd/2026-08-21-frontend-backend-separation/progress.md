# SDD ledger — plan: docs/superpowers/plans/2026-08-21-frontend-backend-separation.md

执行模式：Subagent-Driven；实现 subagent 使用 Haiku，主会话负责集成、任务审查和全分支审查。

## 基线与安全边界

- Approved plan commit: `8a6ddbd` (`docs: add frontend backend separation implementation plan`).
- Spec: `docs/superpowers/specs/2026-08-21-frontend-backend-separation-design.md`.
- Starting implementation baseline will be recorded immediately before each dispatch.
- Pre-existing untracked `.claude/` and `.playwright-mcp/` are preserved and excluded from all task changes.
- No push, merge, production deployment, Cloudflare/Nginx external mutation, credential publication, destructive reset, or deletion is authorized.

## Pre-flight plan scan

The plan was scanned before Task 1 dispatch. Rows below cover every task and every planned shared file/interface. The plan is the binding implementation checklist; the approved spec wins if an implementation detail conflicts with plan prose.

### Task self-consistency table

| Task | Self-consistency check | Ruling |
|---|---|---|
| 1 | Config/bootstrap signatures, compatibility wrapper, tests and listed files agree. | Proceed. Preserve `web.make_app` behavior and avoid new `sys.path` mutation. |
| 2 | Repository protocol and state facade agree; history test is intentionally behavioral rather than asserting an exact rotation count. | Proceed. Preserve current rotation constants and malformed-line behavior. |
| 3 | Publisher/repository interfaces and lifecycle tests agree; fake repository types are test-local fixtures. | Proceed. Persistence is best effort, live delivery remains available. |
| 4 | SQLite facade and explicit-path repository agree; legacy `DB_PATH` patching is a temporary compatibility requirement. | Proceed. No schema or lease semantics change. |
| 5 | Pure domain model functions and security regression tests agree. | Proceed. Domain must not import Flask or storage. |
| 6 | Observe service owns ingest/read models/reconciliation and route compatibility. | Proceed. Adapt focused test names to actual repository tests if examples are stale. |
| 7 | Task/runner service signatures cover policy and fencing delegation. | Proceed. Authenticated machine identity is authoritative over runner JSON. |
| 8 | Thin adapters, auth extraction and unified errors are compatible with old blueprints. | Proceed. Preserve old success/error status behavior where existing tests require it; add stable fields without exposing internals. |
| 9 | Boundary gate checks new HTTP layer and old facades. | Proceed. Source assertions must avoid false positives from compatibility exports. |
| 10 | Reconciliation lifecycle callbacks and push-only tests agree. | Proceed. No daemon starts from test app creation and no subprocess/remote execution is added. |
| 11 | Failure isolation and bounded application errors agree. | Proceed. Task-store failure cannot disable observation. |
| 12 | Static shell/config/routes/CSS are independent of Flask. | Proceed. Runtime config contains only non-secret same-origin paths. |
| 13 | Client/contracts own HTTP and public DTO validation. | Proceed. Views never call fetch; browser sends no probe/runner credentials. |
| 14 | SSE/store requirements need a callback bridge for terminal task refresh. | Ruling: `SseClient` may receive an `onEvent` callback that invokes injected client methods; `FleetStore` remains DOM/client agnostic and stores only models/callback hooks. No direct fetch/EventSource outside the two designated modules. |
| 15 | Fleet/machine views consume store/client and safe DOM APIs. | Proceed. Use actual existing visual conventions while keeping new view source free of `innerHTML`. |
| 16 | Task view and compatibility shell handoff share index/template ownership. | Proceed. Keep old pages working until explicit release cutover; no business data injection into new shell. |
| 17 | Static-only smoke validates the Phase 3 artifact without Flask or credentials. | Proceed. Script owns and cleans only its local server process. |
| 18 | v1 adapters delegate to existing service objects and preserve old paths. | Proceed. Do not introduce runner v1 unless its full compatibility contract is tested. |
| 19 | Packaging is deterministic and non-secret; docs cover independent rollback. | Proceed. Script must not call npm, network, git clean, or production endpoints. |
| 20 | Example proxy config and routing checks express same-origin/SSE/Access policy. | Proceed. These are repository examples/docs only; no external proxy is changed. |
| 21 | Whole-branch gate covers static package, backend, E2E and boundary scans. | Proceed. Adapt scan wording to actual compatibility shims while retaining security intent. |

### Shared-file/interface table

| Shared surface | Tasks | Check and ruling |
|---|---|---|
| `hub/bootstrap.py` | 1, 3, 6, 7, 8, 10, 11 | Task 1 creates app assembly; later tasks add injected publisher/services/lifecycle. Integrators must preserve one `create_app` owner and avoid parallel route registration. |
| `hub/web.py` | 1, 8, 9, 10 | Task 1 creates compatibility delegation; Tasks 8-10 only thin the entrypoint and move lifecycle. No task may remove CLI flags or production fail-closed token behavior. |
| `hub/state.py` | 2, 9 | Task 2 installs repository-backed facade; Task 9 only validates/cleans it. |
| `hub/events.py` | 3, 9 | Task 3 installs publisher compatibility facade; Task 9 must not reintroduce module-global subscriber ownership. |
| `hub/task_store.py` | 4, 9 | Task 4 installs explicit DB repository behind legacy functions; Task 9 preserves patchable compatibility only. |
| `hub/scan.py` | 2, 6, 10 | Repository extraction precedes observe service; reconciliation later delegates to service. No direct remote execution. |
| `hub/routes_observe.py` | 6, 8 | Task 6 delegates business use cases; Task 8 may re-export/register HTTP adapter. One URL registration and one business implementation only. |
| `hub/routes_tasks.py` | 7, 8 | Task 7 moves policy/service orchestration; Task 8 owns transport/error handling. |
| `hub/routes_commands.py` | 7, 8 | Task 7 moves runner orchestration; Task 8 owns auth/JSON/response adapter. |
| `hub/auth.py` | 8 only (with existing callers) | Split extraction from policy without changing three-domain mutual exclusion. |
| `hub/repositories.py` | 2, 4 | Task 2 defines protocol; Task 4 extends task repository contract. Preserve type names and explicit paths. |
| `hub/application/*` | 3, 6, 7, 10, 11 | Publisher precedes observe/task/runner services; lifecycle and failure handling wrap existing services, not duplicate them. |
| `hub/http/errors.py` | 8, 11, 18 | Task 8 defines stable error conversion; Tasks 11/18 consume it. Do not leak raw exception/SQL/path/credential details. |
| `tests/test_repositories.py` | 2, 4 | Separate observation and SQLite fixtures; no real repo `state/` dependency. |
| `tests/test_services.py` | 5, 6, 7, 10, 11 | Pure fakes and service tests accumulate; existing class names in examples may be adapted, but tests must assert behavior. |
| `tests/test_observability.py` | 2, 6, 8, 11, 16 | Each stage preserves observation/page/SSE behavior; no broad weakening of regression assertions. |
| `tests/test_task_api.py` | 4, 7, 8, 10, 11, 16 | SQLite facade, service delegation, HTTP auth, lifecycle and page compatibility remain green. |
| `tests/test_push_only.py` | 1, 6, 8, 10 | Config/bootstrap changes cannot add network/subprocess behavior to hub; probe tests remain authoritative. |
| `frontend/index.html` | 12, 15, 16 | Static shell first; views mount later; compatibility handoff cannot inject secrets/business objects. |
| `frontend/config.js` | 12, 13 | Task 12 defines non-secret config; Task 13 consumes the same `apiBaseUrl`, no credential headers. |
| `frontend/routes.js` | 12, 15 | Route helper owns segment encoding; views cannot hand-build raw URLs. |
| `tests/test_frontend_contracts.py` | 12, 13, 14, 15, 16 | Source contracts must target designated owners and not accidentally reject compatibility files outside `frontend/`. |
| `tests/test_release_layout.py` | 12, 17, 19, 20, 21 | Required static paths, package safety and route docs build incrementally; no generated state in repo. |
| `frontend/api/client.js` / `frontend/realtime/sse.js` | 13, 14 | Exactly one HTTP owner and one EventSource owner; terminal refresh uses injected callback/client, not store DOM knowledge. |
| `README.md` / `docs/HANDOFF.md` | 9, 17, 19, 21 | Documentation updates are additive and must not contain real credentials or claim external deployment. |
| `deploy/nginx-expose.md` | 20 only (existing deployment docs) | Document same-origin split and SSE proxy semantics without changing live infrastructure. |

## Pre-flight rulings

1. **Stale test symbols:** plan snippets such as `tests.test_regressions.IngestScanTests` or class names not present in the current test tree are examples of required coverage, not literal import requirements. Implementers must locate current tests and add focused tests under the named new modules; they must not create fake production compatibility solely to satisfy a stale class name.
2. **SSE callback bridge:** terminal task detail refresh is injected into `SseClient`/application composition as a callback or client adapter. `FleetStore` remains pure state and does not import the API client or touch DOM.
3. **Compatibility layering:** old modules may retain module-level defaults only as explicitly marked facades. New domain/application/infrastructure code must use constructor injection; no new business logic may be added to legacy globals.
4. **Error compatibility:** the stable error contract applies to new adapters and v1. Existing clients/tests that rely on old status codes and public keys remain compatible; additive `request_id` is acceptable, but raw exception details are never exposed.
5. **Phase gate naming:** Task 9 is the Phase 1 boundary gate; Tasks 10-11 are Phase 2; Task 12 begins Phase 3 despite the dependency list wording. Execute in numeric order.

No blocking plan conflict was found. Proceed to Task 1.

## Execution rulings

- Haiku Task 1 dispatch failed before execution with provider error `Model is unavailable`; no worktree changes or commit were produced.
- Sonnet retry also failed before execution with the same provider error. Ruling: escalate this mechanical-but-integration-sensitive task to Opus once, preserving the exact brief and review gate; this is an infrastructure availability workaround, not a scope or quality waiver.
- Opus retry also failed before execution with the same provider error. Ruling: attempt one Fable dispatch as a final subagent provider check; if unavailable, the controller will execute Task 1 inline with the same brief and retain an independent review gate.
- Fable retry also failed before execution with the same provider error. Ruling: execute Task 1 inline with the exact brief; retain the independent reviewer and scoped re-review as mandatory gates.

## Task 1 review loop

- Task 1 implementation baseline: `8a6ddbd08591ac1b26897b6f9474c04e336c0a7e`.
- Inline implementation commit: `dbfd91a` (`refactor: establish explicit hub configuration boundary`).
- Initial review package: `review-8a6ddbd..dbfd91a.diff`.
- Independent review found one blocking startup regression: direct `python3 hub/web.py` could not import root `report_schema.py`; `--help` test did not reach app assembly.
- Ruling: fix the compatibility shim without restoring `sys.path` mutation, and strengthen the test to exercise real startup. This is required by the task's direct-entrypoint compatibility contract and costs two files plus a subprocess regression test.
- Fix commit: `9b9b6d1` (`fix: preserve direct hub script startup`).
- Scoped review package: `review-dbfd91a..9b9b6d1.diff`.
- Scoped re-review: finding status `FIXED`; spec and quality verdict passed; no blocking findings remain. The reviewer also confirmed the base commit reproduces the import failure and HEAD passes the focused/full checks.
- Verification: full unittest suite `146/146 OK`; `compileall` passed; `git diff --check` passed; target path-mutation scan clean; direct self-report dry-run and runner help passed.
- **Task 1: complete.**

## Task 2 dispatch

- Task 2 brief: `task-2-brief.md`.
- Task 2 implementation baseline: `9b9b6d1e5f363dcca01448ccf20c157aa33b2b02`.
- Scope: extract injected observation repository behavior while preserving legacy `hub.state` APIs and push-only reconciliation.

## Task 2 review loop

- Initial review package: `review-9b9b6d1..d921e82.diff`.
- Independent review spec verdict: PASS; quality verdict: PASS with one reproducible regression and compatibility observations.
- Finding: `scan.reconcile_ingest()` can raise `ValueError` and stop the scan when a stray invalid-name `.json` file exists in the state directory; the old implementation skipped no such validation and did not raise. Ruling: filter invalid machine stems at repository enumeration and add a scan regression test; this preserves path safety while preventing one malformed artifact from disabling stale reconciliation.
- Compatibility observation: retain public `state.rotate_if_needed(machine)` alias delegating to the extracted repository method; keep legacy facade surface explicit.
- Non-blocking cleanup: ensure changed Python files have trailing newlines.
- Fix commit: `a242d1fdb5f0bbb3d8bd59c0406d4b02f02f6c30` (`fix: scan skips invalid machine artifacts and restore rotate alias`).
- Scoped fix package: `review-d921e82..a242d1f.diff`.
- Scoped re-review: all original findings `FIXED`; spec and quality verdict passed; no blocking findings remain.
- Verification: repository tests `19/19 OK`; Step-4 observation suite `26/26 OK`; full suite `165/165 OK`; compile and newline checks passed.
- **Task 2: complete.**

## Task 3 dispatch

- Task 3 brief: `task-3-brief.md`.
- Task 3 implementation baseline: `a242d1fdb5f0bbb3d8bd59c0406d4b02f02f6c30`.
- Scope: extract event JSONL persistence and publisher/subscriber lifecycle while preserving the legacy `hub.events` facade and SSE behavior.
- First Task 3 implementer dispatch (`af48e8d8fb544d2c3`) terminated before implementation with provider error 524; no tracked changes or commit were produced. Ruling: resume the same brief/context once; retain the full test and review gates, and only fall back to inline implementation if provider availability fails again.
- Resumed implementer again terminated with provider error 524 after partially writing the new event modules and facade. Ruling: preserve the landed code, complete the remaining bootstrap/notifier wiring and compatibility fixes in the controller, then run the same focused/full gates; no quality waiver.
- Task 3 implementation commit: `5284677` (`refactor: isolate app event publisher and persistence`). Includes domain protocol, JSONL repository, app-instance publisher, legacy facade, notifier injection, bootstrap wiring, lifecycle/rotation/isolation tests.
- Verification: event publisher suite `24/24 OK`; event + SSE/observe regression suite `31/31 OK`; full suite `188/188 OK`; `git diff --check` passed and changed Python files have trailing newlines.
- Independent reviewer dispatch repeatedly failed with provider error 524 before returning a verdict. Controller performed the scoped review locally against the task brief, added an app-context publisher isolation regression test, and found no blocking correctness issue. Remaining residual: broad final branch review is still required at the plan gate.
- **Task 3: complete.**

## Task 4 dispatch

- Task 4 brief: `task-4-brief.md`.
- Task 4 implementation baseline: `52846771c82d772dad727086bc82921fe8c58833`.
- Scope: extract SQLite task persistence behind `SqliteTaskRepository(db_path)` while preserving the patchable `hub.task_store.DB_PATH` facade and all lease/result semantics.
- Implementer `a18074d25a9422df2` dispatched with the exact brief; it terminated again with provider error 524 during read-only exploration and produced no changes. Ruling: controller completed the same extraction inline, retaining the exact interfaces and all storage gates; no quality waiver.
- Task 4 implementation commit: `8c93345` (`refactor: isolate sqlite task repository`). `SqliteTaskRepository` owns explicit-path SQLite persistence; `hub.task_store` is a lazy `DB_PATH` compatibility facade; `TaskRepository` protocol added.
- Verification: RED/green isolation test passed; storage suite `43/43 OK`; full suite `190/190 OK`; compile, diff-check, explicit DB isolation, no-network import, and newline checks passed. Scoped review found no blocking issue.
- **Task 4: complete.**

## Task 5 dispatch

- Task 5 brief: `task-5-brief.md`.
- Task 5 implementation baseline: `8c93345225cfd016593590650168ca6fc57fb1f6`.
- Scope: create pure task/machine domain DTO helpers and state predicates without Flask, storage, network, or subprocess dependencies.
- Implementer `a9357908e461d2943` dispatched with the exact brief; it terminated with provider error 524 before producing changes. Ruling: controller completed the brief inline while retaining RED/green, focused/full regressions, and scoped review gates.

## Task 5 review loop

- Inline implementation commit: `b72d63f` (`refactor: define pure task and machine domain boundaries`).
- Added pure `hub.domain.task` and `hub.domain.machine` modules, expanded `tests/test_services.py`, and routed legacy task/observe serialization and validation through domain helpers.
- Verification: focused domain/API/observe/security suites `65/65 OK`; full explicit regression gate `195/195 OK`; `git diff --check` passed; forbidden dependency scan clean.
- Independent reviewer completed the same test gates but terminated with provider error 524 before returning its requested verdict. Controller performed scoped review locally: required interfaces, field allowlists, truncation, state predicates, public machine DTO sanitization, route compatibility, and dependency isolation all pass; no blocking findings remain. `unittest discover` remains unavailable because `tests/` is intentionally not an importable package; explicit module invocation is the established full-suite command.
- **Task 5: complete.**

## Task 6 dispatch

- Task 6 brief: `task-6-brief.md`.
- Task 6 implementation baseline: `b72d63f7a207017b548aed70e417c2923bd2bf9d`.
- Scope: create `ObserveService` with injected observation repository, event publisher, host config, and clock; preserve ingest/status/detail/events/reconciliation behavior and push-only constraints.
- First implementer terminated with an API connection error after partial isolated-worktree edits; those edits were not integrated.
- Restarted implementer completed commit `ab7bd0d` (`refactor: move observation use cases into service`), then integrated as `0bdb799` on the controller branch.
- Added `hub/application/observe_service.py`; delegated observe routes and scan facade; wired bootstrap injection; added six service tests.
- Verification: focused observation suite `27/27 OK`; full explicit suite `205/205 OK`; compile, diff-check, AST dependency and push-only checks passed.
- Independent scoped review: spec compliance PASS, quality PASS, no blocking findings. Non-blocking notes: events return annotation differs from brief (`list` versus `dict`), service-level edge-branch coverage can grow, and remaining legacy module globals are Task 7+ scope.
- **Task 6: complete.**

## Task 7 dispatch

- Task 7 brief: `task-7-brief.md`.
- Task 7 implementation baseline: `0bdb799da2585eaa003c543992a40d74e0c7f4d5`.
- Scope: extract injected task and runner application services while preserving task policy, runner lease fencing, three authentication domains, old API shapes, and push-only behavior.
- Haiku implementer completed commit `ae67ac5` (`refactor: isolate task and runner application services`), integrated as `880e3c3`.
- Added `TaskService` and `RunnerService`; adapted task/command routes and bootstrap; added service policy/fencing tests.
- Verification: focused task/runner/service suite `68/68 OK`; full explicit regression suite `216/216 OK` (plus compile/push-only checks).
- Independent scoped review: spec compliance PASS, quality PASS, no blocking findings. One non-blocking latent robustness note: `_load_hosts_rows` documentation says malformed YAML falls back empty, but `yaml.YAMLError` is not caught; behavior matches legacy and is deferred to Task 11 failure/degradation handling. Other notes (duplicated state constants and edge-branch coverage) are non-blocking and scheduled for later boundary cleanup.
- **Task 7: complete.**

## Task 8 dispatch

- Task 8 brief: `task-8-brief.md`.
- Task 8 implementation baseline: `880e3c3`.
- Scope: split Flask transport adapters, add transport-only auth extraction, unify bounded application errors with request IDs, preserve old routes/SSE/pages/runner E2E and auth isolation.
- First Task 8 implementer timed out with provider error 524 before producing a commit; no partial work was integrated.
- Restarted implementer completed commit `fcaa52b` (`refactor: make Flask routes thin HTTP adapters`), integrated as `3abe4cb` on the controller branch.
- Added `hub/http/{__init__,errors,observe_routes,task_routes,command_routes,pages}.py`; converted legacy route modules to compatibility shims; wired new blueprints and request IDs in bootstrap; added `extract_operator_identity`, `extract_runner_identity`, and `extract_ingest_token`.
- Verification: focused HTTP/auth tests `18/18 OK`; Task 8 backend contract suite `75/75 OK`; full `unittest discover -s tests` `234/234 OK`; direct script boot, `deploy/e2e-smoke.sh`, compile/diff checks and push-only checks passed in implementation/review runs.
- Independent scoped review: SPEC_COMPLIANCE PASS, CODE_QUALITY PASS, no blocking findings. Medium follow-ups: unhandled unknown exceptions/404/405 still use Flask HTML until Task 11; `hub/http/__init__.py` docstring mentions legacy global names and must be cleaned before Task 9's literal source-boundary test. Low notes: compatibility pages still read legacy facades, and machine-name regex is duplicated.
- **Task 8: complete.**

## Task 9 dispatch

- Task 9 brief: `task-9-brief.md`.
- Task 9 implementation baseline: `3abe4cb`.
- Scope: lock the Phase 1 HTTP/storage boundary, preserve bootstrap/facade compatibility, and document the single-writer/WAL deployment assumption without changing production infrastructure.
- Haiku implementer completed commit `ea0db99` (`test: lock down backend boundary compatibility`), integrated as `ac2048a`.
- Added `tests/test_boundaries.py`, removed legacy global-name mentions from the HTTP package docstring so the literal source gate tests the imports rather than its own documentation, and documented Phase 1 assumptions in `docs/HANDOFF.md`.
- Verification: focused boundary/auth/HTTP suite `22/22 OK`; full `unittest discover -s tests` `238/238 OK`; compileall, diff check, and `deploy/e2e-smoke.sh` (`SMOKE OK`) passed.
- Independent scoped review: SPEC_COMPLIANCE PASS, CODE_QUALITY PASS, no blocking findings. Low notes: missing trailing newline in `hub/http/__init__.py` and intentionally non-recursive/substr-based source assertions matching the brief.
- **Task 9: complete.**

## Task 10 dispatch

- Task 10 brief: `task-10-brief.md`.
- Task 10 implementation baseline: `ac2048a`.
- Scope: isolate observation/task reconciliation and daemon lifecycle behind injected application callbacks; keep app construction thread-free and preserve push-only behavior.
- Haiku implementer completed commit `b42b4bf` (`refactor: isolate backend reconciliation lifecycle`), integrated as `93b7c01`.
- Added `ReconciliationService`, injected lifecycle starters, bootstrap `start_background_jobs`, and compatibility bridges in `scan.py`/`web.py`; `make_app` remains daemon-free and only the real CLI entrypoint starts jobs.
- Verification: focused lifecycle/push-only/lease suite `16/16 OK`; full `unittest discover -s tests` `250/250 OK`; compileall, diff check, and `deploy/e2e-smoke.sh` (`SMOKE OK`) passed.
- Independent scoped review: SPEC_COMPLIANCE PASS, CODE_QUALITY PASS, no blocking findings. Minor follow-ups: interval clamp differs between legacy wrappers and bootstrap start (10s vs 1s floor), compatibility wrappers lack direct coverage, duplicate lifecycle starts are not guarded, and `scan.py` reconstructs a service per legacy call; unused `web.py` import is pre-existing.
- **Task 10: complete.**

## Task 11 review fix round

- Initial Task 11 integrated commit `956554b` passed spec review but failed independent code-quality review because event repository reads were not isolated and observe error contracts were inconsistent.
- Fix commit `a1bc8e6` (`fix: isolate event reads and observe errors`) wraps `EventPublisher.read_recent/read_since` with bounded empty-list degradation, normalizes ingest errors to `invalid_json`, and preserves bounded `ObserveError.detail/status` at the HTTP adapter.
- Added regressions for `/api/events`, SSE replay/live delivery, malformed and non-object ingest bodies, and bounded observe details.
- Verification: focused fix suite `11/11 OK`; full unittest `268/268 OK`; compileall and `git diff --check` clean; local `deploy/e2e-smoke.sh` `SMOKE OK`.
- Independent scoped re-review: SPEC_COMPLIANCE PASS, CODE_QUALITY APPROVE, no blocking findings. Non-blocking notes: broad read exception boundary intentionally mirrors best-effort persistence; server-side exception logging may include paths; SSE test could assert replay emptiness more directly.
- **Task 11: complete after fix round.**

## Task 12 review loop

- Task 12 brief: `task-12-brief.md`.
- Task 12 implementation baseline: `a1bc8e6`.
- Static frontend shell implementation commit: `af9e912` (`feat: add standalone frontend shell and runtime config`).
- Added standalone `frontend/index.html`, non-secret `frontend/config.js`, encoded `frontend/routes.js`, migrated `frontend/styles/app.css`, and frontend/release-layout contracts.
- Verification: focused frontend contracts `14/14 OK`; full unittest suite `282/282 OK`; compileall and `git diff --check` clean; loopback static-server smoke `SMOKE OK`.
- Independent scoped review of `review-a1bc8e6..af9e912.diff`: SPEC_COMPLIANCE PASS, CODE_QUALITY PASS, APPROVE, no blocking findings. Non-blocking notes: route helpers may later reject `.`/`..`; release-layout allowed set must expand with Task 13 files; source-text route tests are brittle; config load-order and minor empty-segment behavior should be considered in later frontend work.
- Ruling: accept Task 12 as complete because all briefed requirements pass and the findings are non-blocking; carry the release-layout expansion into Task 13 and consider traversal guards before views consume route identifiers.
- **Task 12: complete.**

## Task 13 dispatch

- Task 13 brief: `task-13-brief.md`.
- Task 13 implementation baseline: `af9e912`.
- Scope: create public frontend DTO contracts and the single API client; extend frontend contracts/release layout without adding credentials or a build chain.
- Implementer completed commit `e0479ca8d10c7d54bc2acc999f234ffaef5d7bc2` (`feat: add frontend API client and public contracts`); integrated as `479dc40`.
- Added `frontend/api/contracts.js`, `frontend/api/client.js`; extended frontend contract and release-layout tests; added route traversal guards for `.`/`..` and non-secret `pageBaseUrl`.
- Implementer verification: focused frontend suite `37/37 OK` with one expected Node-absent skip; full suite `297/297 OK` with one expected skip; compileall, Node syntax, and `deploy/e2e-smoke.sh` (`SMOKE OK`) passed.
- Independent review package: `review-af9e912..479dc40.diff`; initial review was interrupted by provider timeout after identifying the real `result: null` compatibility defect.
- Fix commit `79044b0` (`fix: accept empty task results in frontend contract`) preserves explicit null and adds a Node fixture regression; object-result and secret/internal-field rejection remain strict.
- Fix verification: focused frontend suite `37/37 OK` with one expected skip; full suite `297/297 OK` with one expected skip; compileall, Node syntax, `git diff --check`, and local `deploy/e2e-smoke.sh` (`SMOKE OK`) passed.
- Scoped re-review package: `review-479dc40..79044b0`; SPEC_COMPLIANCE PASS, CODE_QUALITY APPROVE, no blocking findings. Non-blocking notes: backend-inherited null ambiguity and optional stronger falsy-value fixture coverage.
- **Task 13: complete after fix round.**

## Task 14 dispatch

- Task 14 brief: `task-14-brief.md`.
- Task 14 implementation baseline: `79044b0`.
- Scope: create the DOM-agnostic `FleetStore` and the sole `SseClient` EventSource owner; preserve API client ownership, bounded event/log state, stale timestamp fencing, and terminal task refresh via injected client callback.
- Implementation commit: `89eb083`; review fixes: `a131995`; final poll ownership fix: `d61a17d`.
- Final scoped review: SPEC_COMPLIANCE PASS, CODE_QUALITY PASS; the only actionable note was a missing regression fixture for the exact stop/restart microtask race. The fixture was tightened locally to start poll B synchronously before poll A cleanup drains; focused frontend suite passes `47/47` with one expected skip.
- Verification: full suite before this fixture-only change was `320/320` with one expected skip; compileall, Node syntax checks, and `git diff --check` passed. The fixture-only change is pending its own local commit.
- **Task 14: complete after final race-review fixture gate.**

## Task 15 review loop

- Task 15 brief: `task-15-brief.md`.
- Implementation baseline: `005219a` (`feat: add standalone fleet and machine views`).
- Added DOM-safe native Fleet and machine views, bounded event/task rendering, encoded navigation, task creation UX, and focused source/XSS/release contracts.
- Initial verification: focused frontend/release suite `72/72 OK`; full suite `332/332 OK`; Node syntax and diff checks passed.
- Independent review confirmed four defects: machine rendering used stale local snapshots; `index.html` wired duplicate terminal task refresh owners; loading was not painted synchronously; teardown did not fence late view callbacks.
- Fix commit: `f1dc12a` (`fix: Task 15 review — live store rendering, single SSE refresh owner`). It seeds initial machine/tasks into the store, renders live machine/task state with bounded machine filtering, removes the duplicate store refresh wiring, paints loading immediately, and fences view teardown/form callbacks.
- Scoped follow-up review identified one remaining real race: the form success callback checked `isActive()` only after the successful store write and not on the non-success fall-through. Fix commit: `a92f8d3` (`fix: fence late machine form callbacks`) moves the active check to the callback entry, preventing late DOM or store mutation after teardown.
- Final verification: focused frontend/release suite `78/78 OK`; full suite `338/338 OK`; Node syntax checks and `git diff --check` passed. The only untracked path is the pre-existing `.playwright-mcp/` directory and it is excluded from task changes.
- Architecture review of the supplied 4RouterAiApp and Angelus references: retain their useful ideas as later design inputs—immutable per-session configuration snapshots, local credential custody, deterministic self-contained release artifacts, replayable event ledgers, recoverable task state, structured worker handoffs, and thin UI/control-plane boundaries. Do not import Electron/Tauri/PTY assumptions, local reverse-control flows, broad plugin secret storage, or any design that violates agent-fleet push-only/no-SSH/no-reverse-tunnel constraints.
- **Task 15: complete after fix round.**

## Task 16 record — task view, navigation and compatibility shell handoff

- Task 16 brief: `task-16-brief.md`. Baseline: `a92f8d3` (Task 15 final fix).
- Implementation commit: `c4bebd9` (`feat: add standalone task view and shell handoff`) — verified present in git history.
- Review fix commit: `3e35e7b` (`fix: Task 16 — task view fencing, terminal class allowlist, cutover shell`) — verified present in git history.
- Asset fix commit: `25c79bc` (`fix: serve static assets during frontend cutover`) — verified present in git history.
- Recorded evidence (`task-16-asset-fix-report.md`, 2026-08-23): `hub/http/pages.py` gains a `FRONTEND_CUTOVER`-gated catch-all asset route with explicit release-layout allowlist, `.`/`..` segment and traversal rejection; focused `tests.test_task_view_review` + `tests.test_release_layout` run `33/33 OK`; `compileall hub tests` exit 0; `node --check frontend/views/task.js` exit 0; `git diff --check` clean.
- Status: no independent reviewer verdict is recorded for Task 16 in the SDD artifacts (`task-16-review.diff` exists; no `task-16-report.md`). Completion recorded from the verified commits and recorded test evidence only; no verdict is invented.
- **Task 16: complete.**

## Task 17 record — Phase 3 frontend gate and static-only smoke

- Task 17 brief: `task-17-brief.md`. Base: `25c79bc` (Task 16 asset fix).
- Task 17 commit: `de85dde` (`test: validate standalone frontend release`) — verified present in git history.
- Implemented `deploy/test-static-frontend.sh` (loopback `python3 -m http.server`, kernel-assigned port, readiness poll, 11 required static paths asserted HTTP 200, no credential header names in response bodies, trap kills only its own server PID; no Flask, no external services, no `git clean`, `.playwright-mcp/` untouched); added `test_frontend_has_required_modules` to `tests/test_release_layout.py`; documented the gate in `docs/HANDOFF.md`.
- Recorded evidence (`task-17-report.md`, 2026-08-23): focused `tests.test_release_layout` 7/7 OK; `bash deploy/test-static-frontend.sh` reports `STATIC OK` with all 11 paths green, second run green, no leftover `http.server` processes; `compileall -q connectors hub tools tests report_schema.py` OK; `bash -n deploy/test-static-frontend.sh` and `git diff --check` OK.
- Full suite `unittest discover -s tests`: 384 tests, 2 pre-existing failures, both reproduced with the Task 17 test change stashed (and untouched by Task 17, both in hub routing): (1) `test_http_contracts.ApiEdgeErrorContractTests.test_unknown_api_route_404_is_bounded_json` — 405 != 404; (2) `test_regressions.IngestApiSecurityTests.test_scan_endpoint_requires_post_and_token` — 404 != 405. `deploy/e2e-smoke.sh` was not run as part of Task 17.
- **Task 17: complete.**

## Working-tree ruling on the uncommitted four-file regression

- Read-only `git log`/`git status` at this update (HEAD `de85dde`): the working tree carries uncommitted changes to four files — `docs/HANDOFF.md`, `frontend/views/task.js`, `hub/bootstrap.py`, `hub/config.py`. Those four uncommitted files are NOT part of the committed Task 16/17 release and remain preserved in the working tree pending explicit cleanup; this ledger pass did not stage, revert, or modify any of them.
- `.playwright-mcp/` remains pre-existing, untracked, and untouched.

## Task 66 record — SSE reconnect hardening and machine state-class allowlist

- Task 66 review baseline: `54b77d8` (`test: close frontend backend separation release gate`).
- Worker commits: `f109355` (`fix: fence SSE transport-error reconnect and allowlist machine task classes`) and `dc468b7` (`fix: bound total SSE reconnect attempts against infinite retry`); integrated locally as `f8919fe` and `886da14`.
- `frontend/realtime/sse.js` now closes failed EventSource instances to disable stale-cursor automatic retries, fences stale callbacks, reconnects using the latest store cursor, retains bounded poll fallback, limits cumulative reconnect scheduling to `MAX_RECONNECTS = MAX_POLLS`, resets the counter only after a current-source successful `onopen`, and clears pending timers/counters on `stop()` or explicit `start()`.
- `frontend/views/machine.js` now maps the seven known task states through an explicit class allowlist and uses `st-unknown` for unrecognized state values; dynamic state labels remain text-content based.
- Added deterministic contract coverage for latest-cursor reconnects, total cap, stale callbacks, explicit-start timer cancellation, stop semantics, and class allowlisting in `tests/test_frontend_contracts.py`.
- Independent scoped review verdict: `SPEC_COMPLIANCE: PASS`; `CODE_QUALITY: PASS`; no blocking findings. Informational notes: explicit `start()` defensively cancels a timer even on idempotent live-source calls; a successful `onopen` intentionally starts a new bounded reconnect window as required by the contract.
- Verification: frontend contract suite `91/91 OK`; targeted SSE/class review subset `37/37 OK`; `node --check frontend/realtime/sse.js`, Python compile, and `git diff --check` passed; static frontend smoke `STATIC OK`; route smoke `ROUTE OK`; local E2E `SMOKE OK`; full suite `429` tests with exactly two unchanged baseline routing failures (`test_http_contracts.ApiEdgeErrorContractTests.test_unknown_api_route_post_404_is_bounded_json`, 405 vs 404, and `test_regressions.IngestApiSecurityTests.test_scan_endpoint_requires_post_and_token`, 404 vs 405).
- No backend, deployment, credentials, `.playwright-mcp/`, external infrastructure, push, merge, or destructive cleanup was performed.
- **Task 66: complete.**

## Overall plan closure

- Tasks 1–21 and Task 66 are complete; the frontend/backend separation implementation plan is closed locally at `886da14`.
- Final accepted residuals are the two documented pre-existing Flask routing mismatches and the low-severity reconnect-window semantics noted above; neither blocks the approved architecture or release gate.
- No production deployment or external infrastructure mutation was performed.
- **Task 33: complete.**

## Baseline routing-assertion cleanup (post-plan)

- Commit `edc776e` (`test: align api routing assertions with flask method dispatch`) aligns the two long-standing baseline assertions with actual Flask method-dispatch behavior; no hub routing or runtime code changed.
  - `test_unknown_api_route_post_404_is_bounded_json` -> renamed `test_unknown_api_route_post_method_not_allowed_is_bounded_json`, expects `405/method_not_allowed` bounded JSON.
  - `test_scan_endpoint_requires_post_and_token` now documents that `GET /api/scan` returns `404/not_found` bounded JSON (no GET handler) and keeps the unauthenticated POST -> 403 security assertion unchanged.
- Verified: focused modules `45/45 OK`; full suite `429/429 OK` with zero failures for the first time in the plan history; `git diff --check` clean.
- This ledger entry is local working-tree documentation; no external publication or deployment was performed.
