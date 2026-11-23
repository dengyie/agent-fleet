# Native Agent task HTTP contract

The browser and main assistant use the same `TaskService` to discover targets
and create native Agent tasks. The execution path remains Task → Runner →
native session; tasks do not create platform conversations or Runs. See the
[capability architecture](agent-capability-architecture.md) and
[implementation plan](superpowers/plans/2026-11-21-native-task-api-hardening.md).

## Registered launch directory

`GET /api/machines/<machine>/task-targets` requires the existing operator domain
or an administrator account session. Ordinary account users receive 403;
anonymous callers and foreign machine credentials cannot use this surface.
Successful responses use `Cache-Control: no-store`.

```json
{
  "machine": "my-mac",
  "projects": ["agent-fleet"],
  "observed_at": "2026-11-21T12:00:00Z",
  "agents": {
    "codex": {"installed": true},
    "claude_code": {"installed": null},
    "pi": {"installed": false},
    "zcode": {"installed": false},
    "agy": {"installed": null},
    "hermes": {"installed": null}
  }
}
```

Agent keys come from the executable Agent registry. Installation is a hint
from the latest observation, with `null` for missing/failed probe evidence.
`false` means explicit absence, including a missing family within a nonempty
Agent report. Neither installation nor a directory response grants session
control or guarantees the CLI is currently healthy. Projects are registered
identifiers; executable paths, local databases, prompts and credentials are
not returned. `observed_at` can be null.

A syntactically valid unknown machine returns an empty project list and
unknown installation states. Invalid machine names return the standard 400
`invalid_machine` error. Directory reads do not access the task SQLite store,
so observation/configuration discovery remains usable when task storage is
disabled. Task creation still returns 503 when that store is disabled.

## Creating and recovering a native task

`POST /api/tasks` continues to accept `machine`, `agent_type`, `project`,
`instruction`, optional `confirm`, and optional `client_token`. New tasks
return 201 with `created=true`; the same accepted submission returns 200 with
`created=false` and the current public task. A real session binding, if it
exists, is projected from that task's current attempt. No raw token, lease
nonce, native path or internal origin is included in the task DTO.

```json
{
  "machine": "my-mac",
  "agent_type": "codex",
  "project": "agent-fleet",
  "instruction": "Check the failing tests.",
  "client_token": "browser-generated-operation-id"
}
```

Freeze the whole request and token for one user submission. When its response
is lost, repeating this exact POST recovers the original task before checking
current machine availability, Agent installation or project registration.
Recovery does not queue another attempt, renew an expiry, repeat the creation
audit/event, undo cancellation, or change a confirmation gate's decision.
New submissions still pass all current launch policy checks.

The existing database gives tokens global uniqueness. A token binds the
authenticated actor, machine, Agent, project, exact instruction and original
confirmation requirement. Reusing it for another actor or request returns
409 `idempotency_conflict`, with no original task or request content. The
original confirmation requirement remains true after a gate is confirmed or
rejected. Internal handoff/Workflow origin bindings must also match; their
dedicated internal conflict contracts remain available to their own services.
An ordinary HTTP request cannot drop those bindings to recover an internal
task.

Tokens must be strings of 1–128 characters. Omitted, null and empty-string
tokens retain the existing non-idempotent behavior; booleans, numbers, arrays
and objects return 400 `invalid_client_token`. Separate intentional
submissions, even with identical instructions, need separate tokens.

Read-only recovery and serialized creation share one repository matcher.
Creation repeats matching under `BEGIN IMMEDIATE`, so two callers that both
miss the preliminary lookup cannot create duplicate tasks or accept a changed
request. Existing cancellation fences still block late first creation.
Repository failures return the bounded 503 `tasks_unavailable` response.

## Verification scope

`tests/test_native_task_api.py` uses real disposable Hub applications,
authentication, SQLite transactions, concurrent HTTP submissions and the
existing event publisher. It covers target projection, installation tri-state,
account/domain isolation, invalid inputs, response-loss recovery, changed
policy, content/actor/origin conflicts, original confirmation decisions,
cancellation, concurrent equal/conflicting submissions and storage failures.

```sh
python3 -m pytest tests/test_native_task_api.py tests/test_task_api.py \
  tests/test_task_store.py tests/test_assistant_agent_tasks.py \
  tests/test_handoff.py tests/test_legacy_task_bridge.py \
  tests/test_workflow_task_recovery.py -q
```

The new regression file also runs in the macOS and Windows CI jobs. This is
a Hub change and does not require a signed client update. These tests do not
claim vendor CLI or model acceptance. Ordinary session-message idempotency,
reverse transcript pagination and model HTTP routes retain their separate
contracts and implementation status.

## Google Antigravity CLI (`agy`)

`agy` is a registered executable family. The task/target APIs and idempotency
contract above apply without a new HTTP surface. Example launch:

```json
{"machine":"my-mac","project":"agent-fleet","agent_type":"agy","instruction":"Inspect the failing test and propose a fix.","client_token":"agy-start-001"}
```

The node uses `agy --output-format stream-json --print <instruction>`. Only a
Runner-owned invocation whose `init.conversation_id` is a valid UUID and whose
`init.cwd` matches its private worktree can retain continuation. Follow-ups use
`--conversation <exact-id>`, never the latest-conversation selector. Native IDs,
worktree paths, settings and credentials stay node-private. Explicit local
model/project/agent/effort/mode/settings options survive continuation. Browser
model-catalog takeover is not declared for agy.

Documented `step_update` agent-response deltas and tool parameters/results map
onto existing bounded session DTOs. A final aggregate response is used only if
no visible text delta was received. There is no inferred import of old agy or
Antigravity desktop conversations. Discovery identifies the exact `agy` binary;
installation, login and official vendor availability remain node prerequisites.
The tested protocol reference is https://antigravity.google/docs/cli/headless .
