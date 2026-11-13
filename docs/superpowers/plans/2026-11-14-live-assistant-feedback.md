# Live Assistant Feedback Implementation Plan

> **For agentic workers:** Use executing-plans to implement this plan inline, task by task.

**Goal:** Stream assistant narration, inline tool status and bounded retry progress without replaying completed tools.

**Architecture:** Provider observers emit bounded text deltas and retry notices. The existing lease-fenced event transaction updates a bounded public Run progress projection; authenticated SSE delivers events and initial snapshots through the existing API/service boundary. Existing Markdown, tool badge and scroll components render progress.

**Tech Stack:** Python/Flask/SQLite, native ES modules/Web Components, pytest/Playwright.

## Global Constraints

- Keep private checkpoints, credentials, reasoning content and tool arguments out of public progress.
- Honor explicit retry settings; default new configurations to three retries and streaming.
- Retry explicit transient HTTP rejection only; uncertain accepted requests/tools never replay automatically.
- Preserve partial output on failure/reload, bound bytes/events/connections, release resources on navigation.
- Work in the existing isolated branch; UTF-8 without BOM.

### Task 1: Provider and runtime feedback

Files: `hub/platform/providers/openai_compatible.py`, `fleet_core/request_metadata.py`, `hub/platform/runtime/native.py`, `tests/test_assistant_live_feedback.py`.

- [x] Add tests proving a first delta arrives before response EOF, narrated tool responses reach events before execution, and two 503 responses emit retry notices before succeeding. Run `python3 -m pytest tests/test_assistant_live_feedback.py -q` and observe failures.
- [x] Emit batched bounded `assistant_text_delta`, `assistant_response` and `provider_retry_scheduled` events through the existing observer and runtime sink. Preserve safe checkpoint recovery and cancellation.
- [x] Rerun provider, transport and checkpoint regressions.

### Task 2: Durable public progress and stream

Files: `hub/domain/run_progress.py`, `hub/infrastructure/platform_db.py`, `hub/infrastructure/platform_db_runs.py`, `hub/domain/run.py`, `hub/application/conversation_service.py`, `hub/http/run_stream.py`, `hub/http/conversation_routes.py`.

- [x] Add failing owner isolation, lease, UTF-8 bound, terminal retention and SSE replay/close tests.
- [x] Add an additive `progress_summary` Run column updated in the event transaction. Expose only a bounded public projection in Run DTOs and conversation history.
- [x] Add owner-authenticated bounded SSE with an initial snapshot, durable cursor, finite lifetime and guaranteed slot cleanup.
- [x] Run repository, DTO, retention and HTTP regressions.

### Task 3: Live conversation interaction

Files: `frontend/assets/api/client.js`, `frontend/assets/api/platform.js`, `frontend/assets/views/assistant/service.js`, `frontend/assets/views/assistant.js`, `frontend/assets/views/assistant/panels.js`, `frontend/assets/views/assistant/progress.js`, `frontend/assets/views/settings/catalogs.js`.

- [x] Add browser regression coverage for text before completion, inline tool state, retry status, retained partial output, refresh recovery and mobile layout.
- [x] Implement authenticated fetch SSE at the sole HTTP boundary, abort on suspend/dispose, reconnect through bounded polling fallback.
- [x] Render ordered text/tool segments with existing components; retain error information and partial text without duplicate final answers. Update retry/stream settings copy and defaults.
- [x] Run browser/API architecture tests and inspect desktop/mobile screenshots.

### Task 4: Verify and release

- [ ] Run the complete local test suite and production-quality self review; fix confirmed failures.
- [ ] Rebase onto latest main, publish through normal PR/CI, merge and verify deployed revision and health.
- [ ] Back up production model profiles; enable streaming and three retries for the active profiles using the owner API. Do not rewrite frozen Runs or replay historical requests.
- [ ] Verify live feedback with a controlled production smoke run and update the canonical operations record. Report exact results and any external upstream blocker.

## Execution evidence

- Provider/worker/SSE/cancellation/retry regressions: 10 passed; real browser suite: 16 passed; Python lint and diff checks passed.
- Red evidence covered missing pre-EOF text, cancellation dispatching another retry, SSE continuation, stale history overwriting live output, and retry notices disappearing at dispatch.
- Full local suite and official Linux/Windows CI remain release gates; no production deployment or profile changes claimed yet.
