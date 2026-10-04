# Frontend component provenance

Selected Vanilla components are adapted from the user-requested [dengyie/awesome-ui](https://github.com/dengyie/awesome-ui), revision `79a7f8186fd33cd7354f7d30534b889d50d83e98`:

- `UiIcon`: original icon dictionary and escaping; semantic aliases map legacy call sites to the shared icon set. Upstream attributes the geometry to Tabler Icons (MIT).
- `ChatPromptInput`: retained textarea sizing and IME handling; enhances application-owned form controls, does not clear drafts before server acceptance, and omits unsupported attachments.
- `StreamMarkdown`: retained component contract; bundles marked and DOMPurify locally, uses a restricted sanitized fragment, and disables embedded images/HTML/style and non-HTTP links.
- `ToolCallBadge`: retained disclosure and states; escapes all dynamic fields, localizes labels and exposes expanded state.
- `AutoScrollAnchor`: retained scroll/follow behavior and UiIcon; supports the nearest `data-chat-scroll` container, cleans up scroll/resize listeners and scheduled frames, and respects reduced motion. Falls back to document scrolling on other surfaces.

The selected awesome-ui revision contains no repository-wide license file; no broader license is inferred here. Reuse was explicitly requested by the repository owner. The original source URLs and revision are retained for audit and updates.

Markdown dependencies are committed browser ES modules, not runtime CDN calls or a new build framework:

- marked 16.4.2 — MIT; `assets/vendor/marked-LICENSE.md`.
- DOMPurify 3.3.0 — Apache-2.0 OR MPL-2.0; `assets/vendor/purify-LICENSE`.

The sanitizer is required because model output and tool responses are untrusted input. These modules have no authority over API access or backend state.

## Workspace design references (2026-10-05)

The shell and assistant layout were independently implemented after inspecting the authenticated browser DOM and deployed CSS/JavaScript of [PromptQL](https://prompt.ql.app/project/promptql-community/promptql-playground/bot/c2b89889-c88c-4aeb-a522-b5feef3eb3c3) and [Windhub](https://chat.windhub.cc/chat). PromptQL informed the independent artifact/operation panel; Windhub informed warm neutral surfaces, history navigation, centered conversation and composer controls. No application bundles, branding, conversation contents or account data from those sites are included.
