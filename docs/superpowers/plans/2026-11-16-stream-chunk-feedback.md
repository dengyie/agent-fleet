# Stream Chunk Feedback Implementation Plan

Execute sequentially in the existing isolated worktree; no delegation is needed.

**Goal:** Publish complete public text deltas before waiting for another upstream transport chunk.

**Architecture:** Keep SSE framing in the provider and public batching in TextObservation. Drain pending narration at the transport read boundary, retaining coalescing within one read and all existing cancellation, clipping and error semantics. No timer thread, new UI state, retry layer or execution limit.

**Evidence:** Main CI 43753089148 and repeated local browser runs fail while the fixture waits for the second short delta and the generator waits for its final gate. TextObservation only checks elapsed time when append runs; it cannot flush while the next read blocks.

## Tasks

- [x] Freeze the observation clock and reproduce separate, coalesced and fragmented short deltas missing before the next transport read; cover coalesced-token batching.
- [x] Flush public text when the SSE decoder drains a received chunk, before advancing the upstream iterator. Keep EOF/error flushing and immediate first text.
- [x] Repeat real Chromium success and stream-error journeys; keep aria-busy checks before final release. Do not increase gate timeouts to conceal the defect.
- [x] Register regressions in ASSIST-02, update the existing review, run provider/worker/history/browser checks.
- [ ] Rebase onto latest main, preserve push guards, verify PR and exact main CI plus deployment receipt. Read-only online acceptance; no real model calls or historical Run replays.
- [ ] Update the existing canonical operations document on vault master after verified deployment.
