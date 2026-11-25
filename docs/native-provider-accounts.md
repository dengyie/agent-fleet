# Native AGY Google accounts

Authoritative backend contract for Google personal accounts, extending
[native tasks](native-task-api.md) and [AGY ACP](native-provider-runtime.md).
The browser calls Hub HTTP; the node owns the CLI and long-lived credentials.
This change does not implement a frontend account picker.

## Authority and capability

Public routes require the existing operator identity or active administrator
account session. Anonymous requests receive 401, ordinary account users 403.
Node/runner/ingest credentials cannot authorize browser routes. Account-mode
writes require the configured Origin and JSON. Responses use `no-store`.
An account belongs to one actor and machine; other owners receive 404.
Machine resolution rechecks the owner's current administrator grant before
revealing a temporary OAuth response; revocation also prevents readiness and
new task admission.

New operations require `agents.agy.provider_accounts={"schema_version":1}` in
the node observation. Old nodes receive 409 `provider_accounts_unavailable`;
explicit account selection never falls back to the default login. Upgrade the
signed probe, runner and control client together. Hub deployment does not
upgrade nodes. Capability advertisement is not Google authorization.

## Directory and creation

`GET /api/machines/<machine>/provider-accounts` returns
`{"ok":true,"accounts":[...]}` for the caller, at most 64 accounts per
owner/machine. The fixed bound requires no pagination. No deletion or credential
import is provided. Names are display labels, not verified email addresses:
1–80 trimmed characters without controls.

`POST /api/machines/<machine>/provider-accounts`:

```json
{"name":"Google work","client_token":"create-work-001"}
```

New submission returns 201; an example response is:

```json
{
  "ok":true,"created":true,
  "account":{
    "account_id":"pa_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "machine":"my-mac","name":"Google work","provider":"agy",
    "revision":1,"state":"starting","enabled":true,
    "created_at":"2026-11-25T10:00:00Z","updated_at":"2026-11-25T10:00:00Z"
  },
  "operation":{
    "command_id":"cmd_11111111111111111111111111111111",
    "account_id":"pa_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "revision":1,"operation":"create","state":"starting","status":"queued",
    "created_at":"2026-11-25T10:00:00Z","expires_at":"2026-11-25T10:05:00Z"
  }
}
```

`GET /api/provider-accounts/<id>` returns `{"ok":true,"account":{...}}`.
IDs are `pa_` plus 32 lowercase hex characters. Creation initializes a node
profile; its confirmed state becomes `signed_out`, without signing into Google.

## Operations and login

`POST /api/provider-accounts/<id>/operations` accepts:

```json
{"operation":"login","client_token":"login-work-001"}
```

```json
{"operation":"submit_login","client_token":"answer-work-001","login_command_id":"cmd_22222222222222222222222222222222","response":"PASTED_NATIVE_CODE_OR_CALLBACK_URL"}
```

```json
{"operation":"cancel_login","client_token":"cancel-work-001","login_command_id":"cmd_22222222222222222222222222222222"}
```

`enable`, `disable`, `logout` use only `operation` and `client_token` like login.
Extra fields are rejected. New operations return 202 with `ok`, `created`,
`account` and `operation` as above. Read
`GET /api/provider-accounts/<id>/operations/<command_id>` for operation state,
or replay the identical POST after a lost response.

Read the account until `waiting` exposes `authorization_url`,
`login_command_id` and `expires_at` (Unix seconds). Open the URL in the operator's
browser, authorize Google and submit the code/callback with the original flow
ID. The window is at most 300 seconds from Hub acceptance; network/process
startup consumes it. Waiting reports may shorten it. Login never opens a node
browser. ACP initialize/authenticate/session-new then verifies access without
sending a model prompt.

Only `https://accounts.google.com/o/oauth2/auth`, `response_type=code` and
`redirect_uri=https://antigravity.google/oauth-callback` are accepted. Callback
origin/path and state must match the flow. Duplicate query fields, credentials,
ports, fragments, multiline and oversized input are rejected. Raw codes must
be one bounded line. Do not change flow IDs to retry old codes.

The existing explicit session encryption key is required before login (503
`provider_login_encryption_unavailable` otherwise). Temporary response storage
uses AEAD with command ID associated data, never signed payloads/public DTOs or
logs. Terminal reports, command completion and expiry clear the secret on the
next repository access. Long-lived Google credentials remain on the node.

## States, receipts and retries

| State | Meaning |
| --- | --- |
| `starting` | Accepted operation awaiting node confirmation. |
| `waiting` | Validated URL available; response needed before deadline. |
| `verifying` | Response accepted for original flow; native verification pending. |
| `ready` | Current node operation confirmed login; admission enabled. |
| `signed_out` | Profile exists without confirmed usable login. |
| `disabled` | Admission fenced; credentials retained. |
| `failed` | Operation failed/expired; explicit new attempt required. |

`operation.status` is the existing signed-command receipt (`queued`,
`delivered`, `succeeded`, `failed`, etc.). A succeeded login receipt means the
asynchronous flow started; inspect account state for task readiness. New
operations increment revision; old reports cannot restore ready after a newer
disable/logout. Stale reports return `accepted:false` and stop retrying.

Mutation tokens are 1–128 ASCII characters, starting alphanumeric and then
allowing alphanumerics, `.`, `_`, `:`, `-`. They bind actor, machine, account,
operation and normalized request, including flow/response. Concurrent equal
requests create one persistent operation/command. Replay returns 200
`created:false` with original command/current state; it never enqueues or
renews TTL. Changed bindings return bounded 409 `idempotency_conflict`, without
original request/token/receipt. Current authorization is still required.

Active login blocks unrelated login/enable with 409 `provider_account_busy`;
cancel/disable/logout can fence it. Wrong/expired/unknown flow returns 409
`login_unavailable`. Invalid ID/body/token/response returns 400; body over
16 KiB returns 413; SQLite failure returns bounded 503
`provider_accounts_unavailable`. Unsupported native logout/cleanup is a failure,
not a claim that credentials were cleared. A new token is an explicit attempt.

## Node lifecycle and task identity

Profiles use `SHA256(account_id)/profile` under the owned supervisor store,
with POSIX private permissions or Windows private inheritable DACLs.
`GEMINI_HOME` points there. Following T3, set `SSH_CONNECTION`,
`AGY_ACP_FORCE_FILE_STORAGE=1` and disable CLI auto-update. Child environments
exclude ambient Google/Gemini and Hub credentials, browser/display variables
and Windows case aliases. Existing default login is not imported.

Multiple workers may use a ready account. Cross-process admission/mutation
locks guard generation fences; every worker holds a separate lease for the
vendor lifetime. Login/disable/logout fence admission before stopping selected
managed sessions and waiting for leases. Other accounts remain active.
Disable retains credentials; enable restores ready only with retained confirmed
login. Logout uses native negotiated ACP logout, preserving history/workspaces
and the executable. Enable after logout stays signed-out until new login.

The control client persists bounded operation/report records, never the code.
Restart interrupts pending login, fences its owned process and reports failure;
it never restarts login or replays secrets. Reports survive transient Hub
failures. Signed nonce/durable receipt rules prevent duplicate effects. Unknown
execution requires reconciliation or explicit retry.

An AGY task may select an account:

```json
{"machine":"my-mac","agent_type":"agy","project":"agent-fleet","instruction":"Check the failing tests.","client_token":"work-task-001","provider_account_id":"pa_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
```

Only AGY accepts the field. Owner/machine/readiness must match. Original task
replay recovers accepted work after disable without a new attempt. New tasks,
retry, continue and confirmation recheck readiness/owner. Public tasks and new
`session_metadata` events expose only the account ID; private leases carry the
owner digest. Exact continuation retains account/root/owner and native ID;
it cannot switch to another/default account. Omission preserves legacy behavior.

## Internal transport, migrations and verification

Signed `manage_provider_account` targets the account ID with no attempt. Payload
contains only account ID, revision, operation and owner digest.
Machine-only `POST /api/supervisor/provider-accounts/resolve` accepts
`{"command_id":"cmd_..."}` after delivery and within revision/deadline; login
configuration includes original `expires_at`. Machine-only
`POST /api/supervisor/provider-accounts/report` accepts bound command/account/
revision/state and optional bounded URL/expiry/error. Both require supervisor
credentials, not cookies/runner/ingest credentials.

The existing supervisor database gains `provider_accounts`,
`provider_account_operations` and `supervisor_meta.provider_accounts_schema=1`.
Account/token/command commit in one SQLite writer transaction. Task storage gains
nullable `provider_account_id` and
`task_repository_meta.provider_account_schema=1`; old tasks remain unbound.
Malformed/future schemas fail closed; partial migrations roll back. No database
or deployment process is added.

See [tests](testing/README.md) and the
[plan](superpowers/plans/2026-11-25-agy-multi-account.md). The E2E test uses real
HTTP/SQLite, signed queue, an independent login/ACP subprocess and two supervised
workers, covering response loss, isolation, task binding, disable/enable/logout,
preserved history and the other account remaining active. Other suites cover
concurrency, revocation, migration failure, restart, expiry and corruption.
Fixtures call no network, vendor CLI or model. Real AGY/Google/quotas and frontend
account UI remain separate acceptance. No automatic rotation or other auth
methods are included.

Upstream reference: T3 Code commit
[`b042e4ee757947ca1cd47bb217c94296f9c695cf`](https://github.com/pingdotgg/t3code/commit/b042e4ee757947ca1cd47bb217c94296f9c695cf),
especially the [Antigravity guide](https://github.com/pingdotgg/t3code/blob/b042e4ee757947ca1cd47bb217c94296f9c695cf/docs/user/providers-antigravity.md).
See the [source comparison](research/t3code-architecture-reference.md).
