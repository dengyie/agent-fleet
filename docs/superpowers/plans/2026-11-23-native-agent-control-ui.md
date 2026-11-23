# Native Agent Control UI Implementation Plan

> Execute inline in the existing isolated `codex/native-agent-control-ui` branch. Reuse public HTTP contracts and existing UI components; backend work remains independent.

**Goal:** Make the native computer/VM Agent's read, message injection, configuration and handoff capabilities accessible from the web conversation workspace.

**Architecture:** The HTTP client remains the only transport boundary. Native chat owns command admission and receipts; a focused operations panel consumes server capability declarations and registered local profiles. Reuse the existing handoff panel rather than duplicate its evidence and idempotency protocol. Observed machines and cloud execution registrations remain distinct.

**Tech Stack:** Vanilla JavaScript ES modules, existing UiIcon/ChatPromptInput/StreamMarkdown/ToolCallBadge and native dialog inspector, Flask/SQLite browser fixtures.

## Constraints

- Do not modify backend policy, native adapters or CLI execution.
- No inferred permissions, synthetic model choices or direct node filesystem access.
- A 202 response means queued; receipt success means delivery, not an assistant answer.
- No automatic replay of writes. Preserve drafts and pending tokens across panel updates.
- Stop and abort reads on route suspension/disposal; do not publish stale results.
- Use UTF-8 without BOM and existing responsive light/dark theme tokens.

## Tasks

- [x] Add browser acceptance scenarios for registered local-profile selection, exact control payload/receipt, permission revocation, handoff preview/create, and node-to-directory links. Use real Hub services for admission and route interception only for node observations/evidence that cannot execute in CI.
- [x] Add `views/agent-chat/operations.js`, consuming `sessionControls`, `getMachine` and chat callbacks. Render availability, node/authorization links, registered profiles and declared lifecycle actions. Preserve selection on polls and confirm destructive controls.
- [x] Integrate operation and handoff tabs in `inspector.js`. Extend chat control callback with explicit payload and human-readable labels; disable new writes after failed authorization reads. Reuse the handoff component with cancellable preview lifecycle and task links.
- [x] Connect the target picker to existing node sessions and node management. Add directory capability filtering/summary without fabricating project or filesystem facts.
- [x] Run native-chat, capability, navigation, handoff, separation and release layout regression suites. Inspect desktop/mobile browser evidence, package resources and latest main before publishing.
- [ ] Rebase on current main, push and merge after CI. Verify main deployment identity and actual logged-in page behavior before claiming availability.

## Validation

```sh
FLEET_PLAYWRIGHT_MODULE=/Users/mango/.codex/worktrees/openpet-final-acceptance/OpenPet/node_modules/playwright python3 -m pytest tests/test_native_chat_frontend.py tests/test_agent_capability_frontend.py tests/test_frontend_separation.py tests/test_release_layout.py -q
git diff --check
```

Local evidence: native chat, handoff service/HTTP/browser, capability and console acceptance **58 passed**; follow-up offline-target/refresh/navigation acceptance **3 passed**. Frontend contracts, DOM-safe rendering, navigation, session contracts, release layout and separation **195 passed, 5 skipped, 2 subtests passed** (the five optional navigation browser cases were not enabled in that command). The separate Chrome suite exercised the actual chat and console pages. Independent release packaging produced **102 files**, including the operations module. JavaScript syntax, UTF-8 without BOM and whitespace checks passed. Screenshots inspected at 1440px/light and 390px/dark. These checks use temporary stores and signed queue admission; real vendor execution and production verification remain release acceptance boundaries.

Review follow-up: the original nested confirmation failed the new keyboard regression with “Escape must preserve the inspector” (the inspector closed). Mount the confirmation as a sibling native dialog, so its Escape handling cannot reach the inspector. The unchanged regression plus local-profile, revocation and offline-target acceptance then passed **4 tests** at both widths. Additional navigation/authorization/settings-overlay Chrome regression: **55 passed**.
