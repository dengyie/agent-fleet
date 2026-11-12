# Workflow startup registration

**Goal:** Enable the ordinary Hub process to load the already proven coding delivery template and executors from an explicit operator-owned file.

**Architecture:** `hub.web` supplies a path through `FleetConfig`; `hub/composition` builds the existing catalog, routes and adapters. Existing workspaces, conversations, Runner authorization and Node identities remain authoritative. Local tests use the existing sandbox launcher; remote tests use the existing signed input, capability and fixed-test pipeline. No database or public request contract changes.

- [x] Capture missing registration via the normal `make_app` HTTP template endpoint.
- [x] Load a versioned, bounded UTF-8 JSON manifest, validate existing bindings and gate dependencies, and fail startup on ambiguous registration.
- [x] Exercise normal startup and the full controlled coding delivery using the original coordinator, Runner and worker; cover remote registration and disabled dispatch.
- [x] Document configuration, add release selectors and run focused regressions.

Back to [development ledger](../../platform/development-completion.md) and [Workflow entry](../../platform/workflow-platform/README.md).

## Verified results — 2026-11-11

Original startup missing-registration failure: `/tmp/agent-fleet-workflow-startup-red.xml`. Controlled full chain initially exposed fixture adapter identity and isolated pytest path assumptions; those original failures remain in `/tmp/agent-fleet-workflow-startup-{first,second}.xml`. Both were corrected explicitly in the fixtures. Original seven-module regression: 85 passed. Subsequent precedence regression reproduced two failures: explicit/environment empty paths incorrectly fell back to the persisted file. `_file_setting` now respects presence and explicit emptiness; existing boolean settings are unchanged. Fresh seven-module regression `/tmp/agent-fleet-registration-precedence-green.xml`: **88 passed, zero failures/errors/skips, 24.96 s**. Five selectors added to WF-01.

Configuration and actual boundaries are documented in [startup registration](../../platform/workflow-platform/startup-registration.md). This proves normal Hub registration and controlled delivery, not production rollout or permanent Node setup. The [Node registration follow-up](2026-11-11-node-workflow-registration.md) retains that remaining work.
