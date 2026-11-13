# Request metadata and production UX

Implementation in `codex/request-metadata`, based on public main `975b611`.

## Contract and reference evidence

Windhub authenticated DOM and shipped client code inspected on 2026-10-05. Its five values are input, cache read, reasoning, output, cache write; output is visible output, with reasoning included in billed output. Compact muted monospace chips wrap on narrow screens; focus/hover cost explanation shows quantity × rate and total. Adopt information hierarchy and light motion using existing UiIcon/StreamMarkdown/AutoScrollAnchor.

Request metadata must come from the provider boundary, not current catalog selection or browser guesses. Record every HTTP attempt including retries. Persist start before dispatch and finish after parsing, using existing lease-fenced run events. Never persist prompts, headers, credentials or raw provider bodies. Missing values remain null; historical requests are not fabricated. UTC epoch timestamps and monotonic elapsed milliseconds have different jobs. Provider-reported USD cost takes precedence over explicitly configured per-million rates; estimates are labelled and frozen with their calculation. No hardcoded market prices.

## Tasks

1. Add provider-neutral request observation and strict metadata normalization. Extend the provider completion contract with an explicit request observer; adapt deterministic provider and test doubles. Instrument OpenAI HTTP attempts, parse usage details/response identity/cost, request streaming usage, and close response iterators on all paths. Tests: independent JSON/SSE fixtures, UTF-8 chunk split, success/retry/timeout/malformed stream, missing/invalid numbers, decimal pricing and redaction.
2. Persist through existing run events, project requests in owner-scoped Run and Conversation reads with one batched query, expose frozen public model and message linkage. Mark unfinished requests unknown for terminal runs. Tests: restart recovery, cross-owner reads, immutable prices/model, multi-step/retry identities, pending and failed runs, bounded query count.
3. Reproduce slow provider lease expiry, keep both leases renewed during blocking execution with an owned heartbeat and stop/join cleanup. Preserve fencing and unknown boundaries; no automatic replay. Tests: slow call crosses TTL, stolen lease, cancel while waiting, heartbeat failure, cleanup.
4. Render compact per-request metadata below assistant messages, including active/failed turns. Stable keyed updates preserve text selection, detail expansion and scroll; a single lifecycle-owned timer updates active elapsed time. Native details provide keyboard/touch access; reduced motion disables decorative animation. Tests: real Chromium history/reload/multi-request/retry, XSS, focus, mobile/dark/reduced motion, incremental update identity, timer teardown.
5. Add a complete request-metadata test matrix and link into test entry, journey gate and release acceptance. Run focused tests, full `--require-journeys` suite, security/production review. Rebase latest main, publish via established CI deployment, verify actual release hashes and live requests, update canonical Obsidian entries and scan links.

Verification uses `/tmp/agent-fleet-ui-testenv/bin/python`, Playwright 1.63.0 and Chromium. Full command in `docs/testing/README.md`. External provider errors are recorded separately from local passing tests; lack of pricing must be visible, never silently zero.
