# Platform availability and session capture

The assistant initializes from the defaults catalog so a failing optional node endpoint cannot disable model/workspace selection. Existing conversations retain their workspace; the sidebar lists owner-scoped Hub conversations and refreshes after submissions. Monitoring independently loads node observations as well as the service catalog.

Production setup must explicitly enable platform, service monitoring, worker/scheduler and provider networking; populate the authenticated owner's models and directory workspaces and provide the referenced provider credential. A published frontend alone does not enable these APIs. Empty service catalogs represent services that have not been registered, not successful service health checks.

Provider requests identify Agent Fleet with a User-Agent. Tool definitions include parameter schemas, so real models can supply the executor's actual argument names.

Runner and native JSONL capture now preserve source records, replay pending uploads, page full conversations, and support managed native resume. See session-conversations.md for supported formats and limits.

Validation includes a real Chromium workflow with a failed optional nodes API, restored conversation/workspace, sidebar history, and long transcript paging, plus provider request contract and runner/session regression tests. Deployment still requires real provider and file-output acceptance.
