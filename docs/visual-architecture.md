# Agent Fleet visual architecture

This document is the visual companion to
[Architecture v3](architecture-v3.md),
[Control Plane v4](architecture-v4-control-plane.md), and the
[self-hosted platform contract](platform/agent-fleet-self-hosted-platform.md).
The diagrams show the current production boundaries. Historical pull-based SSH
collection is intentionally absent.

## System topology

```mermaid
flowchart LR
    Operator["Operator browser"] --> Access["Cloudflare Access / account session"]

    subgraph Hub["Hub control center"]
        API["Flask HTTP API"]
        App["Application services"]
        Repos["SQLite repositories"]
        UI["Static console + SSE"]
        Artifacts["Bounded artifacts"]
        API --> App --> Repos
        App --> Artifacts
        UI -- "same-origin API" --> API
    end

    subgraph Nodes["Managed compute nodes"]
        Probe["Probe<br/>read-only observation"]
        Runner["Runner / Node client<br/>allowlisted execution"]
        Control["Control client<br/>signed supervisor commands"]
        Workspace["Owned workspace / process group"]
        Runner --> Workspace
        Control --> Workspace
    end

    Probe -- "POST ingest" --> API
    Runner -- "poll / heartbeat / result" --> API
    Control -- "poll / receipt" --> API
    Access --> UI
    App --> Provider["Configured model provider"]

    Hub -.-> Boundary["No inbound node connection"]
```

Node-side clients initiate every connection. The Hub never opens SSH, agent,
or browser ports on a managed node.

## Data and control planes

```mermaid
flowchart LR
    subgraph Observation["Observation plane"]
        direction TB
        P1["Process and system probe"] --> I1["Authenticated ingest"]
        I1 --> S1["Sanitize and atomically persist"]
        S1 --> V1["Fleet / machine views"]
    end

    subgraph Execution["Task and Run plane"]
        direction TB
        O2["Operator creates bounded task or Run"] --> Q2["Durable queue + lease"]
        Q2 --> N2["Node polls and executes fixed capability"]
        N2 --> R2["Journal + receipt + bounded artifacts"]
        R2 --> F2["Run finalization and conversation projection"]
    end

    subgraph Supervision["Session supervision plane"]
        direction TB
        D3["Explicit process/session identity"] --> A3["Adoption"]
        A3 --> C3["Signed pause / resume / terminate / detach"]
        C3 --> E3["Durable receipt and session projection"]
    end

    V1 -. "status only" .-> Q2
    F2 -. "optional task/session binding" .-> A3
    E3 -. "never grants arbitrary shell" .-> Q2
```

## Task and attempt state machine

```mermaid
flowchart TB
    subgraph Automatic["Automatic and runner transitions"]
        direction LR
        Start(["Created"]) --> Queued["queued"]
        Queued -- "runner claims eligible task" --> Leased["leased"]
        Leased -- "first accepted heartbeat" --> Running["running"]
        Leased -. "lease expires; same task requeued" .-> Queued
        Running -- "fenced result: exit 0" --> Succeeded["succeeded"]
        Running -- "fenced result: non-zero" --> Failed["failed"]
        Queued -- "task TTL expires" --> Expired["expired"]
    end

    subgraph OperatorRules["Operator transitions: source state to destination state"]
        direction LR
        PauseSource["queued | leased | running"] -- "pause" --> Paused["paused"]
        Paused -- "continue; fresh attempt" --> Continued["queued"]
        CancelSource["queued | leased | running | paused"] -- "cancel" --> Cancelled["cancelled"]
        RetrySource["succeeded | failed | cancelled | expired"] -- "retry; fresh attempt" --> Retried["queued"]
    end
```

Every claim, heartbeat, result, retry, and continuation is fenced by task,
attempt, lease, and worker identity. Task TTL expiry applies only while queued;
lease expiry requeues the current leased or running attempt. An unknown write
outcome is retained and is not replayed as a new write.

## Native transcript identity resolution

```mermaid
flowchart LR
    Process["Discovered agent process"] --> Family{"Known agent family?"}
    Family -- "no" --> Unassociated["Observable process<br/>no native transcript association"]
    Family -- "yes" --> Resolver{"Family has an explicit identity resolver?"}
    Resolver -- "no" --> Unassociated
    Resolver -- "yes" --> Selector{"Exactly one valid session selector?"}
    Selector -- "no / missing / conflicting" --> Unassociated
    Selector -- "yes" --> Path["Resolve candidate below the configured session root"]
    Path --> Unique{"Exactly one regular JSONL file?"}
    Unique -- "no" --> Unassociated
    Unique -- "yes" --> Bound["Bind transcript to this process identity"]

    Note["Never select the newest file by mtime"] -.-> Selector
```

Codex accepts an explicit resume identity, Claude Code accepts an exact
session ID, and Pi accepts an explicit session path or ID constrained to its
configured root. Families without a stable selector remain unassociated.

## Scheduler execution lifecycle

```mermaid
sequenceDiagram
    participant Scheduler
    participant Repository
    participant Worker
    participant Diagnostics

    Scheduler->>Repository: claim owner and worker slot
    Repository-->>Scheduler: fenced lease
    Scheduler->>Worker: execute Run
    par while execution is active
        Scheduler->>Repository: heartbeat owner and slot
    and worker path
        Worker-->>Scheduler: result or original exception
    end
    Scheduler->>Repository: release worker slot
    alt release succeeds
        Repository-->>Scheduler: released
    else release fails
        Repository--xScheduler: cleanup error
        Scheduler->>Diagnostics: platform_slot_release_failed
        Note over Scheduler: Preserve the worker result or original exception
    end
```

Slot release is durable housekeeping. Its failure is recorded but cannot
replace a successful worker result or mask the worker's original exception;
the lease remains reclaimable by expiry and reconciliation.

## Release and production verification

```mermaid
flowchart LR
    Main["Push public main"] --> Linux["Linux full suite<br/>required journeys + release evidence"]
    Main --> Windows["Windows launch and workspace gate"]
    Linux --> Package["Backend and frontend packages"]
    Windows --> Package
    Package --> Receiver["Restricted deployment receiver"]
    Receiver --> Backup["Consistent backup + integrity checks"]
    Backup --> Activate["Activate backend and static manifest"]
    Activate --> Health{"Health, account readiness, and version checks pass?"}
    Health -- "yes" --> Live["Deployment receipt: deployed / succeeded"]
    Health -- "no" --> Rollback["Restore code and static release<br/>retain new database writes"]
    Live --> Smoke["Independent public smoke<br/>manifest + health + anonymous auth"]
```

The deployment job only accepts artifacts from the same clean revision and CI
run. A green pull request does not deploy, and fixture screenshots do not
substitute for external model, mail, or real-node acceptance.

## UI evidence

The current UI evidence inventory and durable contact sheet are in
[UI visual evidence](testing/visual-evidence.md). Individual full-resolution
screenshots remain attached to the CI run because they are test evidence, not
product assets.
