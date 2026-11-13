# Agent Fleet visual architecture

This document is the visual companion to
[Architecture v3](architecture-v3.md),
[Control Plane v4](architecture-v4-control-plane.md), and the
[self-hosted platform contract](platform/agent-fleet-self-hosted-platform.md).
The diagrams show the current production boundaries. Historical pull-based SSH
collection is intentionally absent.

## System topology

![System topology](assets/architecture/system-topology.svg)

[Full-resolution PNG](assets/architecture/system-topology.png)

<details>
<summary>Mermaid source</summary>

```mermaid
flowchart TB
    Operator["Operator browser"] --> Access["Cloudflare Access / account session"]
    Access --> UI["Static console + SSE"]
    UI -- "same-origin API" --> API["Flask HTTP API"]
    subgraph Hub["Hub control center"]
        API --> App["Application services"]
        App --> Repos["SQLite repositories"]
        App --> Artifacts["Bounded artifacts"]
        App --> Provider["Configured model provider"]
    end
    subgraph Nodes["Managed node: clients initiate all connections"]
        Probe["Probe: read-only observation"]
        Runner["Runner / Node client: fixed capabilities"]
        Control["Control client: signed commands"]
        Runner --> Workspace["Owned workspace / process group"]
        Control --> Workspace
    end
    Probe -- "POST ingest" --> API
    Runner -- "poll / heartbeat / result" --> API
    Control -- "poll / receipt" --> API
```

</details>

Node-side clients initiate every connection. The Hub never opens SSH, agent,
or browser ports on a managed node.

## Data and control planes

![Data control planes](assets/architecture/data-control-planes.svg)

[Full-resolution PNG](assets/architecture/data-control-planes.png)

<details>
<summary>Mermaid source</summary>

```mermaid
flowchart TB
    subgraph Observation["Observation"]
        P1["Process and system probe"] --> I1["Authenticated ingest"]
        I1 --> S1["Sanitize + atomic persistence"]
        S1 --> V1["Fleet / machine views"]
    end
    subgraph Execution["Task and Run"]
        O2["Operator creates bounded work"] --> Q2["Durable queue + lease"]
        Q2 --> N2["Node polls fixed capability"]
        N2 --> R2["Journal + receipt + artifacts"]
        R2 --> F2["Finalize Run + conversation"]
    end
    subgraph Supervision["Session supervision"]
        D3["Explicit process/session identity"] --> A3["Adoption"]
        A3 --> C3["Signed pause / resume / terminate / detach"]
        C3 --> E3["Receipt + session projection"]
    end
```

</details>

The observation view supplies status, not execution authority. A completed Run
may carry an explicit task/session binding into adoption; supervision never
grants arbitrary shell access. These are separate contracts, not implicit
transitions between the three planes.

## Task and attempt state machine

![Task attempt states](assets/architecture/task-attempt-states.svg)

[Full-resolution PNG](assets/architecture/task-attempt-states.png)

<details>
<summary>Mermaid source</summary>

```mermaid
flowchart TB
    Start(["Created"]) --> Queued["queued"]
    Queued -- "eligible runner claim: new attempt + nonce" --> Leased["leased"]
    Queued -- "task TTL expires" --> Expired["expired"]
    Leased -- "first accepted heartbeat" --> Running["running"]
    Leased -. "lease expires" .-> Queued
    Running -. "lease expires" .-> Queued
    Leased -- "accepted result" --> Exit{"Exit code"}
    Running -- "accepted result" --> Exit
    Exit -- "0" --> Succeeded["succeeded"]
    Exit -- "non-zero" --> Failed["failed"]
```

</details>

Operator transitions are validated separately from runner events:

| Action | Allowed source states | Destination |
|---|---|---|
| pause | queued, leased, running | paused |
| continue | paused | queued, fresh attempt |
| cancel | queued, leased, running, paused | cancelled |
| retry | succeeded, failed, cancelled, expired | queued, fresh attempt |

Runner claims create an attempt and nonce-bound lease. Heartbeats and results
must name the current attempt and nonce; both are rejected after lease expiry
or when the task is no longer leased/running. Task TTL expiry applies only while
queued; lease expiry requeues the current leased or running attempt. Operator
pause, cancel, retry, and continue transitions are checked against their
allowed source states; retry and continue create a fresh attempt. Unknown
platform write outcomes are retained rather than replayed, a separate contract
from this legacy task queue.

## Native transcript identity resolution

![Transcript identity](assets/architecture/transcript-identity.svg)

[Full-resolution PNG](assets/architecture/transcript-identity.png)

<details>
<summary>Mermaid source</summary>

```mermaid
flowchart TB
    Process["Discovered agent process"] --> Family{"Known agent family?"}
    Family -- "yes" --> Resolver{"Explicit identity resolver?"}
    Resolver -- "yes" --> Selector{"Exactly one valid session selector?"}
    Selector -- "yes" --> Path["Resolve below configured session root"]
    Path --> Unique{"Exactly one regular JSONL file?"}
    Unique -- "yes" --> Bound["Bind transcript to process identity"]
    Family -- "no" --> Unassociated["Observable process; transcript unassociated"]
    Resolver -- "no" --> Unassociated
    Selector -- "missing / invalid / conflicting" --> Unassociated
    Unique -- "no" --> Unassociated
```

</details>

The resolver never selects the newest file by mtime.

Codex accepts an explicit resume identity, Claude Code accepts an exact
session ID, and Pi accepts an explicit session path or ID constrained to its
configured root. Families without a stable selector remain unassociated.

## Scheduler execution lifecycle

![Scheduler lifecycle](assets/architecture/scheduler-lifecycle.svg)

[Full-resolution PNG](assets/architecture/scheduler-lifecycle.png)

<details>
<summary>Mermaid source</summary>

```mermaid
sequenceDiagram
    participant Scheduler
    participant Repository
    participant Worker
    participant Diagnostics
    Scheduler->>Repository: claim owner lease, then Run
    Repository-->>Scheduler: owner lease + Run claim
    Scheduler->>Repository: claim worker slot
    alt no slot available
        Scheduler->>Repository: requeue claim, then release owner
    else slot acquired
        Scheduler->>Repository: release owner, then renew slot before execution
        Scheduler->>Worker: execute Run (if slot renewed)
        par execution active
            Scheduler->>Repository: heartbeat Run lease + slot
        and worker path
            Worker-->>Scheduler: result or original exception
        end
        Scheduler->>Repository: release worker slot
        alt release fails
            Repository--xScheduler: cleanup error
            Scheduler->>Diagnostics: platform_slot_release_failed
        else released
            Repository-->>Scheduler: released
        end
    end
```

</details>

The sequence starts with an eligible Run claim. An idle owner or a Run already
resolved by cancellation/recovery releases the owner lease without execution.
Slot contention requeues the claim without consuming an execution attempt.
Loss of the heartbeat changes a returned result to `lease_lost`; an original
worker exception still propagates.

Slot release is durable housekeeping. Its failure is recorded but cannot
replace a successful worker result or mask the worker's original exception;
the lease remains reclaimable by expiry and reconciliation.

## Release and production verification

![Release verification](assets/architecture/release-verification.svg)

[Full-resolution PNG](assets/architecture/release-verification.png)

<details>
<summary>Mermaid source</summary>

```mermaid
flowchart TB
    Main["Push public main"] --> Linux["Linux full suite + required journeys"]
    Linux --> Evidence["Verify release evidence"]
    Main --> Windows["Windows process + workspace tests"]
    Evidence --> Package["Backend + frontend packages"]
    Package --> Receiver["Restricted deployment receiver"]
    Windows --> Receiver
    Receiver --> Backup["Consistent backup + integrity checks"]
    Backup --> Activate["Activate backend + static manifest"]
    Activate --> Health{"Health, account readiness and version pass?"}
    Health -- "yes" --> Live["Receipt: deployed / succeeded"]
    Health -- "no" --> Rollback["Restore code + static files; retain new DB writes"]
    Live --> Smoke["Independent public smoke: version, health, auth"]
```

</details>

Linux tests are required to build the release packages. Deployment waits for
Linux tests, Windows launch-policy tests, and both packages. The deployment job
only accepts artifacts from the same main revision and CI run. A green pull
request does not deploy, and fixture screenshots do not substitute for external
model, mail, or real-node acceptance.

## UI evidence

The current UI evidence inventory, durable contact sheet, and full-resolution
screenshots are in [UI visual evidence](testing/visual-evidence.md). The
repository copies are unchanged CI fixture evidence, not product assets.

## Reproducing the diagrams

The six Mermaid blocks above are the editable source. The committed SVG and
PNG exports are generated from those exact blocks, with content-sized canvases
and selectable SVG text. `assets/architecture/render-manifest.json` records each
source hash, export hash, dimensions, and renderer versions. UI screenshots
remain separate test evidence.

With Mermaid 12.3.0 and Playwright 1.63.0 already installed outside the runtime:

```bash
FLEET_MERMAID_MODULE=/path/to/node_modules/mermaid \
FLEET_PLAYWRIGHT_MODULE=/path/to/node_modules/playwright \
FLEET_BROWSER_CHANNEL=chrome node tools/docs/render-architecture.cjs
```

The renderer uses an isolated headless browser for local documentation. It does
not connect to a user browser or a running Fleet instance.
