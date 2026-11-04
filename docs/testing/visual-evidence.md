# UI visual evidence

This page records the visual evidence produced for the deployed revision
`136915220efb8cde80c25e4660b16412781993be` by
[CI run 41792242807](https://github.com/dengyie/agent-fleet/actions/runs/41792242807).
The source artifact is `agent-fleet-test-evidence`; it contains the 19 PNG
files below, `journeys.json`, and `results.xml`.

![Contact sheet of all 19 CI browser screenshots](../assets/visual-evidence/ci-41792242807-ui-contact-sheet.png)

The contact sheet is a durable review aid. The CI artifact remains the source
of truth for the full-resolution bytes and is retained according to the
workflow policy.

## Coverage inventory

| Evidence | Viewport | Surface verified |
|---|---:|---|
| `assistant-desktop.png` | 1440 x 1000 | Assistant initial desktop layout |
| `assistant-complete.png` | 1440 x 1000 | Completed response and composer layout |
| `assistant-artifact.png` | 1440 x 1000 | Artifact presentation |
| `assistant-dark.png` | 1440 x 1000 | Dark theme contrast and layout |
| `assistant-focus.png` | 1440 x 1000 | Keyboard/focus-visible state |
| `assistant-mobile.png` | 390 x 844 | Assistant mobile layout |
| `assistant-mobile-panel.png` | 390 x 844 | Mobile navigation/panel layout |
| `fleet-desktop.png` | 1440 x 1075 | Fleet dashboard cards and event rail |
| `node-desktop.png` | 1440 x 1811 | Machine detail and long-page flow |
| `task-desktop.png` | 1440 x 1077 | Task state, result, and action layout |
| `monitoring-desktop.png` | 1440 x 1350 | Service health and bounded actions |
| `session-desktop.png` | 1440 x 11443 | Full session detail and transcript flow |
| `search-1440.png` | 1440 x 900 | Session search desktop layout |
| `search-390.png` | 390 x 1051 | Session search mobile layout |
| `request-metadata-desktop.png` | 1440 x 1000 | Request details on desktop |
| `request-metadata-mobile.png` | 390 x 844 | Request details on mobile |
| `login-desktop.png` | 1440 x 1000 | Login desktop layout |
| `login-mobile.png` | 390 x 844 | Login mobile layout |
| `full-flow-recovered.png` | 1280 x 720 | Recovered conversation after failure/reload |

## What these images prove

- Every PNG is decodable, has non-zero dimensions, and contains non-uniform
  pixels. The 19-image contact sheet was reviewed as a single set for blank
  output, major overlap, clipped controls, and inconsistent responsive layout.
- Desktop and 390 px mobile evidence exists for login, assistant, search, and
  request metadata. Long-page evidence exists for node, task, monitoring, and
  session views.
- These images contain fixture data. They prove browser rendering of the tested
  revision; they do not prove production account data, external provider
  behavior, mail delivery, or a real remote node.

## Deliberate boundaries

This release did not capture a logged-in production UI, run external model,
mail, or real-node acceptance, or open a production Muse account. The public
production smoke verified the release manifest, health endpoint, public routes,
and anonymous authentication boundary. Those facts must not be relabeled as
screenshots of live authenticated behavior.

When UI behavior changes, the required browser journeys must regenerate the
relevant screenshots. Update this inventory and contact sheet only from a
clean CI run for the released revision.
