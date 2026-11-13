# Node command control and bounded renewal

> Execute inline with evidence-driven-bugfix and verification-before-completion. Preserve the earlier immutable Linux acceptance freeze and all existing feature work. Return to the [development ledger](../../platform/development-completion.md).

**Goal:** Stop an owned workspace process when its command is cancelled, its execution deadline is reached, Node shutdown is requested or ownership is lost; renew active short leases without extending the original command or execution budget.

**Architecture:** Add an authenticated Node control request to the existing command delivery/repository transaction. Return only command identity, state, continue flag, execution deadline and remaining lease duration. Existing cancelled/expired/unknown command facts stay immutable. A Node-owned bounded heartbeat observes control separately from backend cancellation checks; those checks perform no HTTP or SQLite writes. Reuse the backend's existing process group / Windows Job cleanup. Older Hubs and custom executors retain their existing protocol, while local shutdown and signed expiry apply to controlled executors. No new queue, process identity supplied by a caller, or remote numeric-PID kill.

**Stack/constraints:** Python 3.10+, Flask, SQLite WAL, existing Transport.request_bounded_json; keep `hub -> fleet_core <- tools`, UTF-8 without BOM and the protected primary worktree. This is execution control, not permission to install software or invoke external models.

## 1. Reproduce and preserve evidence

Files: `tests/test_node_command_control.py`.

- [x] Add actual Node runtime / Python process shutdown and real HTTP command cancellation tests. A marker written after cancellation demonstrates the failure; capture and reap owned fixture processes.
- [x] Add a backend `unknown/process_stop_unconfirmed` receipt regression and exercise the missing authenticated control endpoint and renewal API.
- [x] Run the new module before production edits and preserve its red XML/log.

## 2. Transactional command control

Files: `hub/infrastructure/command_repository.py`, `hub/application/command_delivery_service.py`, `hub/http/node_routes.py`.

Interface:

```python
def renew_command(self, command_id: str, worker_id: str, *, owner_id: str,
                  node_id: str, lease_s: float = 15.0,
                  credential_fingerprint: str | None = None) -> dict: ...
# POST /api/platform/v1/nodes/commands/control
# JSON request: command_id, worker_id, lease_s
# JSON response: {ok: true, control: {command_id, status, continue,
#                                  execution_deadline, valid_for_s}}
```

- [x] Validate finite 1..3600-second leases; authenticate and recheck the current Node credential inside the command transaction before any mutation.
- [x] Scope exact owner/node/worker, settle the original queue/lease/receipt deadline, and observe Run cancellation in the same transaction. Return stop for terminal commands without reopening or rewriting them.
- [x] For active commands, extend only the lease up to its original hard/receipt limit; never change attempt, args, signature, queue deadline or execution deadline. Preserve SQLite causes.
- [x] Advertise protocol version 1 outside the signed command envelope. Older clients ignore it.

## 3. Owned Node execution lifecycle

Files: `tools/platform/command_control.py`, `tools/platform/node_client.py`, `tools/platform/node_executor.py`, `tools/platform/node_runtime.py`, `tools/platform/workflow_remote_test.py`.

Interfaces:

```python
def execute_controlled(self, command, *, should_cancel): ...
class CommandControl:
    def start(self): ...
    def should_cancel(self) -> bool: ...
    def close(self): ...
```

- [x] Negotiate only for supported controlled execution and a bounded transport. Check ownership before spawn; heartbeat requests use a fixed transport deadline, no redirects/retries, bounded JSON and an owned thread joined on exit.
- [x] Combine Node shutdown, monotonic signed expiry, server execution deadline and lease/control loss. Cancellation polling only reads local state; it must not renew on each backend poll.
- [x] Pass the callback into workspace execution and fixed-test execution. Preserve `unknown` from unconfirmed cleanup. Do not treat arbitrary adapters as cancellable.
- [x] Keep the original Node journal identity and pending receipt replay. Hub terminal uncertainty remains unchanged even after a local stop; stopping cannot undo an earlier side effect.

## 4. Verification and recording

- [x] Repeat the exact failing module; cover valid renewal, expired/wrong/revoked scope, lost transport, terminal precedence, shutdown, execution cutoff and clean thread/process teardown.
- [x] Run Node HTTP/runtime, command timing/cancellation, native suspension, credentials, process cleanup, fixed-test and boundary regressions.
- [x] Register implemented selectors, update ledger/contract and preserve red/green evidence. Run lint, Python 3.10 AST and diff checks. Complete strict Linux/browser, native Windows and real two-node/model acceptance remain independent requirements.


## Evidence on 2026-11-11

- Initial red: `/tmp/node-command-control-red-20261111.xml`, 8 failures. The real Hub cancel and Node shutdown cases both observed the late file write; the unknown receipt was incorrectly mapped to failed, and renewal/control were absent.
- Expanded red: `/tmp/node-command-control-expanded-red-20261111.xml`, a delayed one-second lease response incorrectly granted a fresh local lease. The same report also contains two invalid timing fixtures (their envelope expiry did not equal timing.hard_deadline); those fixtures were corrected separately and are not production failures. The latency regression now charges the entire request round trip against its original lease.
- Shutdown admission red: `/tmp/node-command-control-shutdown-admission-red-20261111.xml`, one failure: a stopped Node still entered polling. Both the runtime and sequential client loop now stop new admission while preserving the current command's journal/receipt path.
- Final associated verification: `/tmp/node-command-control-final-20261111.xml`, 245 passed / 23.39s in fifteen modules, zero skips. The new module contains 17 functions / 33 parameter cases, including actual owned process exit on cancel, shutdown, execution cutoff and loss of control transport, continued execution through short-lease renewal, local-only backend checks, timing immutability, credential rotation, rollback/cause, terminal precedence and old Hub compatibility.
- Additional integration: `/tmp/node-command-control-integration-20261111.xml`, 336 passed / 55.97s in twelve artifact/capability/HTTP/transport/fixture-two-node modules, zero skips. These results are recorded separately, not added to a full-suite total.
- Ruff E9/F63/F7/F82/F401, Python 3.10 AST and diff checks passed. The later independent freeze is `0d11d8b4474d439faa08c91ee03ef3fc3b2af5a9`; `4f4b6478` predates this implementation.
- Native Linux verification on the later freeze: 17 modules, 288 passed / 75.83s, zero skips. All 1134 source digests remained unchanged and the checkout stayed clean. Evidence: `/Users/mango/.cache/agent-fleet/verification/node-command-control-20261111/node-control-native-0d11d8b4/`.
- Native home-win verification of that same freeze: the original ten Windows CI modules plus Node control, command timing and Windows artifact path policies (13 modules), 289 passed / 31.56s and two documented POSIX-only skips. A private environment built from the host's existing working Python 3.12.7 replaced the broken relocated Python 3.13 interpreter; no global Python, PATH or service changes. All 1134 source digests remained unchanged and the checkout stayed clean. Evidence: `windows-ci-and-control/` in the same evidence directory. An earlier broader run remains failed (68 failed / 338 passed / 2 skipped), including interpreter stderr, POSIX service fixtures and artifact fixtures without native NTFS ownership prerequisites. The passing subset does not supersede that report or establish full Windows artifact receive/export acceptance.
- Original packagers produced 1058 backend and 67 frontend files with no digest mismatch. Full collection resolved 6974 cases and all 2021 selectors across 44 journeys; collection is not execution. The complete Linux/browser gate, real two-node/model acceptance and arbitrary external adapter stop remain open. Large browser/dependency download approval is still pending.
