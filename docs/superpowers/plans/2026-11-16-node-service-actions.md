# Node Service Action Routing Plan

**Goal:** Make the existing approved service inspect/restart path execute through the standard signed Node runtime and retain its real receipt, instead of failing with `unknown_tool`.

**Evidence:** `ServiceActionService.request/decide` emits `service.inspect` and `service.restart`, but `NodeRuntimeConfig` rejects both capabilities and `NodeToolExecutor` dispatches only `reconcile.service.inspect` before rejecting non-`tool.*` actions. Existing action tests stop at enqueue or call `ServiceActionExecutor` directly; they do not prove normal Hub-to-Node execution.

**Architecture:** Reuse `ServiceActionExecutor`, the signed command protocol, approval grants and Node journal. Add the two explicit service command capabilities to the shared action contract and scope dispatch to the exact capabilities configured on the Node. Do not grant service actions through workspace or reconcile capabilities. Keep uncertain restart outcomes non-replayable. Treat a completed read-only inspect with nonzero process status as observed unhealthy state, consistent with the existing postcheck collector.

**Files:** `fleet_core/node_action_contract.py`, `tools/platform/node_runtime.py`, `tools/platform/node_executor.py`, `tools/platform/services/actions.py`, `tools/platform/services/base.py`, `tools/plugins/node.py`, `hub/application/service_action_service.py`, `hub/application/run_worker_service.py`, `hub/platform/tool_broker.py`, `hub/platform/runtime/assistant_prompt.py`, `hub/domain/tool_activity.py`, and their runtime, action, collector, assistant-routing and feedback tests; existing Node, journey and release documentation.

- [x] Add a failing Hub request/approval -> signed Node Runtime -> process fixture -> journal -> Hub receipt test for systemd, Supervisor and Docker inspect/restart, including duplicate suppression and no execution before restart approval.
- [x] Add failing capability gate and nonzero-inspect state tests.
- [x] Define the two exact service command capabilities; assemble and dispatch the existing executor only where configured, returning bounded state metadata.
- [x] Verify malformed envelopes, absent/mismatched capabilities, explicit grant handling, duplicate suppression and uncertain restart behavior, plus existing reconcile contracts.
- [x] Expose existing approval reads and bounded service-command status to the assistant, so a request receipt can be followed to its actual state without resubmission. Test owner/target scope and normal remote-assistant Hub routing.
- [x] Reproduce missing service activity titles/status previews (11 failed cases), show request/approval/execution separately through existing progress projection, and remove obsolete service display aliases.
- [x] Reproduce Docker stopped-container false health (20 failed cases); use actual container status for ordinary inspect, reconcile inspect and existing collectors. Unknown output must remain unknown.
- [x] Run focused tests, lint and final diff review before publishing through required CI. Fresh local evidence: 255 passed across 14 affected test modules, ACTION-01 1/1 passing with its declared selectors, Ruff selected correctness/import checks and diff/UTF-8-no-BOM checks passed. This does not substitute for full required Linux/Windows CI or live service execution.
- [ ] Publish the verified candidate, rebase onto current public main before merge, and require exact-head CI before deployment.
- [ ] On the released source, use only a dedicated disposable test service for live service diagnostics; retain actual receipts and mark capabilities requiring unconfigured external infrastructure as unverified.

**Separate capability gap:** `build_monitoring` does not install a production `log_reader`. The existing normal `service.read_logs` declaration is not proof of working remote log retrieval. A durable normal log-command/receipt path or accurate capability filtering needs its own evidence and implementation; the inspect/restart fix does not claim this gap is closed.
