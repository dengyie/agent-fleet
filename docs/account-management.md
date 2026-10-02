# Local account management

Account mode adds email verification, email/password login, password reset, profile editing, revocable sessions and administrator account controls. It is independent of the legacy edge-issued operator token. Ordinary accounts use immutable `acct_…` owner IDs; email changes or matching an existing operator email cannot grant ownership of existing data.

## Runtime interface

`FleetConfig.from_root(..., accounts={...})` accepts `enabled`, `origin`, `registration`, `trusted_proxies` (IP/CIDR list) and an injected `sender(email, purpose, code)` callable. The sender injection is for embedding and tests. The application also reads:

| Variable | Purpose |
| --- | --- |
| `AGENT_FLEET_ACCOUNTS_ENABLED=1` | Enable account mode; unset preserves legacy mode |
| `AGENT_FLEET_ACCOUNT_ORIGIN` | Exact public HTTPS origin, without trailing slash; local HTTP only for localhost/127.0.0.1 |
| `AGENT_FLEET_ACCOUNT_TRUSTED_PROXIES` | Comma-separated trusted proxy IPs/CIDRs; empty means direct clients and forwarded headers are ignored |
| `AGENT_FLEET_REGISTRATION` | `invite` (default) or `open` |
| `AGENT_FLEET_ONEMAIL_ORIGIN` | one-mail Worker HTTPS origin; selects one-mail before SMTP |
| `AGENT_FLEET_ONEMAIL_TOKEN` | Dedicated sender address JWT, not a unified inbox key or admin password |
| `AGENT_FLEET_ONEMAIL_SITE_PASSWORD` | Optional one-mail private-site password |
| `AGENT_FLEET_SMTP_HOST` | SMTP hostname |
| `AGENT_FLEET_SMTP_PORT` | Port, default 465 |
| `AGENT_FLEET_SMTP_STARTTLS=1` | Use required STARTTLS, typically with port 587; otherwise implicit TLS |
| `AGENT_FLEET_SMTP_USERNAME` | SMTP login username |
| `AGENT_FLEET_SMTP_PASSWORD` | Runtime-only SMTP secret |
| `AGENT_FLEET_SMTP_FROM` | Verified sender address |

The one-mail adapter uses its documented `POST /api/send_mail`, address-JWT `Authorization` header and optional `x-custom-auth`. Each send includes `x-idempotency-key`; only HTTP 200 with `status: ok` confirms delivery. Redirects and automatic retries are disabled. The dedicated address must already have sending permission/balance and its domain must have a working mail transport in one-mail. This integration does not require its global administrator password.

Account mode can instead explicitly select a configured TLS SMTP transport. The activation preflight found that the available one-mail API did not confirm delivery; the existing SMTP transport used by the mail project is being validated for activation. Configure only the selected transport's environment variables. There is no automatic retry through a second sender after an uncertain delivery. A successful address login or SMTP authentication alone is not delivery verification: the activation check must receive a code and complete registration with it.

No mail sender means verification requests return an explicit service-unavailable error for every email address. With a configured sender, the public response acknowledges the request without asserting delivery. Public registration and real mail delivery are not activated by adding these files.

The durable store is `<root>/var/accounts/accounts.db`. It contains scrypt password/code hashes, SHA-256 hashes of random session tokens, invitations and persistent rate counters. Protect this directory as credential material. First-administrator creation uses `tools/account-admin.py --root <root> --email <email>` and hidden interactive password entry. `--username` can assign an optional, case-insensitive login name (3–32 letters, digits, underscores or hyphens); both username and email authenticate the same immutable account and share credential limits. Registration and recovery still require an email address. It refuses to create a second bootstrap administrator. Administrators can promote verified users through account settings. Creating an invitation grants registration eligibility for seven days; it does not send an invitation email. The invited person opens the registration page and requests a verification code.

## Health checks

Guardians and release startup probes use credential-free `GET /healthz`, which returns only `{"status":"ok"}` with `Cache-Control: no-store`. `/api/status` remains an authenticated administrator business API in account mode; using it as an anonymous liveness probe causes repeated restarts on 401. Configuration readiness still requires an authenticated platform check.

## Authentication and authorization

- Production sessions use host-bound Secure, HttpOnly, SameSite=Lax cookies. Password reset/change and disabling/role-changing a user revoke sessions at the server. Sessions expire after seven days.
- Changing a password atomically revokes pending password-reset codes as well as login sessions. An earlier code cannot overwrite the new password, including when its email is delivered late or its reset request is already in flight. A newly issued code can still recover the account.
- Successful password reset/change also clears that account's login and password-change attempt counters in the credential transaction, allowing the new password to work after an account-level lockout. Failed verification or a transaction rollback does not clear them. IP limits, mail delivery quotas and other accounts' counters remain independent.
- Browser mutations require JSON and an exact configured Origin. Forwarded headers never override browser Origin; client IP is resolved only through the explicitly trusted proxy policy described below.
- Verification codes expire after ten minutes, allow five attempts, and are consumed atomically. A replacement code invalidates the previous one. Mail requests have a per-email cooldown/day limit; login attempts have a per-email limit. Public account endpoints also apply an operation-specific client-IP limit. Login attempts cannot consume code-delivery or password-reset quotas. Behind a reverse proxy, explicitly configure only the trusted proxy IPs/CIDRs: the resolver walks X-Forwarded-For from right to left and stops at the first untrusted hop. Spoofed prefixes and headers from untrusted direct peers do not select client identity. A trusted peer with a missing, malformed or fully trusted chain is rejected instead of sharing a proxy quota. The proxy must append the actual peer address or overwrite the header from a trusted source; do not whitelist whole client networks.
- Account mode ignores edge email identity and DEV_OPERATOR fallback. Machine credentials keep their existing separate guards. The edge must allow the login shell and account APIs to reach the Hub; a proxy that still demands the old operator token on every route is incompatible with account mode.
- Ordinary users can access account settings and owner-scoped conversations/artifacts/catalogs. Legacy fleet-wide tasks, raw sessions, adoption, monitoring administration and node configuration remain administrator-only. UI visibility does not replace server-side checks.
- The assistant receives the verified account identity from the shell. Ordinary accounts mount conversation/history/artifact controls; administrator-only memory, execution-window, legacy-task and command-inspection panels and their API requests are excluded. Administrator and legacy operator workflows retain those controls.
- Each API request checks session validity; streaming responses check it before forwarding each chunk. Visible idle pages recheck once per minute, and focus/history restoration also revalidates. Every validation compares user ID and role against the mounted page. An identity change disposes the old view, stops streams, clears rendered private content and reloads the document.

Mail eligibility, minute/day quota reservations and challenge replacement share one write transaction. Requests that cannot send mail (such as registration for an existing account or an uninvited email) keep the generic response but do not consume the email's delivery quotas. Request-level IP throttles still apply. A rejected reservation rolls back both mail counters, and network delivery happens after the transaction commits.

The public code endpoint returns the same status and body for ineligible emails, accepted deliveries, email cooldown/day exhaustion and provider failures. It does not turn email-specific outcomes into an account/invitation lookup. Failed delivery deletes only the matching challenge and records `account_mail_delivery_failed` with exception types and stack locations, without email addresses, codes, credentials or raw provider errors. Missing configuration, request-IP exhaustion and unexpected store failures remain explicit errors. Delivery is synchronous, so this status/body protection does not guarantee identical response timing.

## Concurrent account administration

User responses include an integer `revision` for access-policy changes. `POST /api/accounts/users/<id>` requires `revision` plus the fields intentionally changed (`role` and/or `active`). The UI submits only one changed field per action. The store validates the actor, compares revision, checks the last-administrator invariant, changes state and revokes sessions within one SQLite write transaction. Missing revisions return 428; stale revisions return 409 without changing state or revoking sessions. The UI reloads the list on conflicts. Existing account stores gain the revision column without changing account IDs, password hashes or sessions.

## Existing data and resource provisioning

Enabling accounts does **not** migrate existing email-owned platform data, provision models/workspaces for new users, or copy credentials. New users see an explicit model/workspace configuration empty state until their account owner ID has resources. An explicit administrator-reviewed ownership mapping is needed before moving existing workspaces/conversations to a new account. No automatic email-based account linking is performed.

Existing users and a bootstrap administrator must be prepared before enabling the mode. Switching the mode changes the identity source and access boundary; this change has not been released to production. This implementation does not include email-address changes, MFA, social login or billing.

## Verification

`tests/test_accounts.py` covers verification expiry, attempt exhaustion, invitation consumption, hashing, password reset, session revocation, role protection and throttling. `tests/test_account_http.py` covers cookie attributes, origin checks, legacy-mode compatibility, ordinary/admin boundaries and cross-account conversation isolation.

`tests/test_account_browser.py` runs the real frontend against a disposable Hub with a local mail sink and a controlled clock. Set `FLEET_PLAYWRIGHT_MODULE` to a Playwright installation to exercise registration, login, password change/reset and account settings in Chromium. No test sends real email or contacts production.

Production review regressions in `tests/test_account_review_regressions.py` cover trusted proxy chains, spoofed forwarding headers, independent quotas, stale updates, concurrent updates/demotions and schema upgrades. The account-switch browser fixture covers administrator-to-user and user-to-user transitions through focus, history and timer validation, plus stale administrator UI updates.

`tests/test_account_recovery.py` covers password rotation with pending/in-flight reset grants, delayed mail, ineligible registration attempts, concurrent deliveries and quota rollover. The account assistant browser fixture exercises ordinary and administrator sessions against a real disposable Hub: task completion, reload recovery, artifact preview, cancellation, and the absence of administrator requests/controls for ordinary users.

`tests/test_account_public_responses.py` compares registration/reset responses across known, unknown, disabled and invited addresses, including cooldown, daily quota and sender failures; it also verifies safe diagnostics and independent IP/configuration errors. `tests/test_account_credential_limits.py` covers reset/change after login lockout, isolation of unrelated limits, failed/expired verification and complete credential-transaction rollback.
