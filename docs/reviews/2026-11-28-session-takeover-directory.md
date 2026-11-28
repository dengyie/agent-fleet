# Original controllable sessions hidden by native history backfill

The incident report was that previously controllable native conversations had
lost takeover. Production comparison identified a reproducible directory
regression: native history backfill populated the newest directory pages with
read-only imports, while the browser's capability selector only filtered its
first 100 loaded rows. An empty matching list therefore concealed older
controllable identities.

The captured first Mac page contained 100 read-only Hermes imports and zero
message-capable rows. The same production API still reported message delivery
for all nine retained cross-platform native-continuation acceptance sessions,
and for the original desktop session with a prior successful delivery receipt.
Comparing the pre-release SQLite snapshot with current metadata found no
removed managed/process-group bindings or changed capability lists among the
144 previously managed sessions. Newly recovered family metadata is separate
from control authority. These observations do not establish that every actual
Agent send still succeeds.

## Root-cause repair

- HTTP `GET /api/sessions?capability=message|control|read` filters each bounded
  candidate page using the existing live authority projection. It keeps the
  immutable snapshot order and binds cursors to the capability selection.
- Empty matching pages retain a progressing cursor. The browser resets on
  capability changes and follows up to five bounded pages per action; any
  remainder is available through Load more.
- The administrator assistant can use the same capability/cursor fields in
  `session.list`, so its discovery can also find the original controlled row.
- Read-only imports retain their capture identity and authority. Native titles
  and equal text do not merge rows or grant control. No database migration,
  node-client update, model invocation or deployment implementation change is
  part of this repair.

## Evidence and regression entry points

HTTP regressions first failed for history backfill, empty matching pages,
filter-bound cursors and invalid queries. The real Chromium scenario first
failed to find the original session after filtering. The assistant discovery
scenario first rejected the capability argument. All are retained in the
product suite and SESSION-01 journey gate.

Run commands are in [the testing guide](../testing/README.md). The contract and
authorization-change behavior are in [the session API](../native-session-api.md#session-directory).
This change restores discovery of original controls; it does not assert that
every imported history supports continuation or that an API capability claim
proves a new native Agent action completed. Production acceptance must check
the original IDs, current permissions, paged discovery, and deployed browser
resources separately from model execution.
