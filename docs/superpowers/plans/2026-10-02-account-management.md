# Account Management Implementation Plan

> **For agentic workers:** Execute inline with checkpoint verification. Do not deploy until registration policy and mail delivery are configured.

**Goal:** Add verified email registration, password login/recovery, revocable sessions, personal account settings and administrator account management.

**Architecture:** A separate SQLite account store owns users, challenges, sessions and rate limits. HTTP-only cookies authenticate accounts; all mutations require a configured same-origin browser origin. Existing operator mode remains available only when account mode is disabled. Account IDs, never submitted email addresses, become platform owner IDs. Ordinary users cannot access legacy fleet-wide operator APIs.

**Tech Stack:** Flask, SQLite, Werkzeug scrypt, one-mail HTTP API with optional TLS SMTP, existing vanilla JavaScript frontend.

## Global Constraints

- Develop on `codex/account-management`; do not modify the primary worktree.
- Never log passwords, codes, cookies or SMTP secrets. Store only password and token hashes.
- Registration policy is configurable as `invite` or `open`; default `invite` until explicitly selected.
- Email delivery uses an injected mail sender for tests; the selected production integration is one-mail. No mock delivery in production.
- No automatic takeover of legacy email-owned workspaces; migration requires an explicit administrative mapping.

## Deliverables and verification

- [x] Durable accounts: email normalization, password hashing, one-use expiring challenges with bounded attempts, atomic invitation consumption, rate limits and session revocation. Files: `hub/accounts/store.py`, `tests/test_accounts.py`.
- [x] HTTP integration: registration/login/reset/settings/admin APIs; secure cookies, same-origin mutation guard, strict account-mode identity and legacy role isolation. Files: `hub/accounts/http.py`, `hub/accounts/mail.py`, `hub/auth.py`, `hub/bootstrap.py`, `tests/test_account_http.py`.
- [x] UI: email/password login, registration/code request, recovery, account settings, session revocation and administrator invitations/user controls. Files: `frontend/assets/shell/account-login.js`, `frontend/assets/views/account.js`, shared API/navigation/composition.
- [x] Verification: focused pytest and browser acceptance for registration through logout, invalid/expired codes, password resets, authorization and account isolation. Re-run existing token-mode authentication coverage.
- [x] Document runtime settings, bootstrap-admin procedure and deployment/migration limitations. Actual production configuration depends on the user's mail provider and dedicated one-mail sender address/JWT configuration.

## Checkpoint commands

```sh
python -m pytest tests/test_accounts.py tests/test_account_http.py -q
python -m pytest tests/test_auth_matrix.py tests/test_platform_auth.py tests/test_frontend_separation.py -q
```

## Verified implementation

- User selected invitation-only registration and one-mail delivery. `OneMailSender` implements the existing address-scoped HTTP contract, including idempotency headers and bounded success validation.
- Full repository verification with browser acceptance: 2110 passed, 2 skipped, 154 subtests passed before the one-mail adapter addition.
- One-mail integration has dedicated mocked-transport tests; no real mail was sent.
- Production remains unchanged. Dedicated sender credentials, legacy owner mapping/resource provisioning and activation are not completed by this development change.

## Production review root-cause fixes

- [x] Reproduce the three review findings with four failing tests before changing implementation.
- [x] Bind mounted page state to user ID/role on every session check; dispose and reload on changes.
- [x] Resolve source IP through explicitly trusted proxy chains and separate authentication-operation quotas.
- [x] Require account revisions, update only intended fields, and protect revision checks and last-admin invariants in one transaction.
- [x] Verify spoofed headers, same-role account switches, concurrent admin writes, schema upgrades and browser conflict recovery.

Final verification after the root-cause fixes: **2139 passed, 2 skipped, 154 subtests passed**, with real Chromium acceptance enabled. The four original regression checks were observed failing before implementation and passing afterward. Full-suite testing also exposed and removed a shared-logger mutation in the new test fixture; diagnostic tests pass in the final run.

## Second review: recovery lifecycle and assistant permissions

Execute inline in the existing feature worktree. The three confirmed reproduction paths are stale reset codes after password change, ineligible registration requests exhausting recovery mail quotas, and ordinary assistant pages requesting administrator APIs.

- [x] Add recovery regression tests in `tests/test_account_recovery.py`; observe rejection assertions fail before editing `hub/accounts/store.py`.
- [x] Delete pending reset challenges in the password-change transaction alongside password update and session revocation. Verify old codes, concurrent reset completion and delayed delivery cannot restore an older grant; fresh challenges still work.
- [x] Reuse a transaction-local rate-counter operation inside `issue_code`: check eligibility, reserve minute/day limits and replace the challenge in one transaction. Ineligible requests keep the generic response without reserving mail quota. Keep external mail I/O outside the transaction.
- [x] Add a real Hub/browser fixture for ordinary and administrator assistant sessions, including seeded owner resources, conversation submission/cancellation and artifact preview. Observe ordinary requests to administrator endpoints before editing the UI.
- [x] Pass the authenticated account from `frontend/assets/app.js` to `mountAssistant`. Mount administrator-only panels and perform their initialization/recovery calls only for an administrator or existing legacy operator. Preserve backend guards and ordinary conversation/artifact behavior.
- [x] Update `docs/account-management.md`, run the new regressions, existing account/browser tests, and the full suite with Playwright enabled. Run `git diff --check` and verify the protected primary worktree is clean.

Primary command: `FLEET_PLAYWRIGHT_MODULE=/tmp/agent-fleet-ui-deps/node_modules/playwright /Users/mango/project/codex/agent-fleet/.venv/bin/python -m pytest -q tests/test_account_recovery.py tests/test_account_browser.py --disable-warnings --tb=short`.

Verification progress: five recovery regressions and the ordinary assistant browser case failed before their fixes. The initial focused run passed 55 checks. The first full run found one obsolete source-string assertion for the memory opt-in expression; it has been replaced by browser checks for selected-but-disabled, enabled-but-empty, and enabled-with-selection requests. Those browser and remaining memory contract checks passed (6 checks). Final full verification with Playwright enabled: **2147 passed, 2 skipped, 154 subtests passed** in 143.04 seconds. `git diff --check` passed and the primary `main` worktree remained clean. All three second-review findings are fixed locally; no production deployment, commit or push was performed.

## Third review: public code responses and recovery limits

- [x] Reproduce account/invitation enumeration through consecutive code requests in `tests/test_account_public_responses.py`. Compare known, unknown, disabled, invited and uninvited addresses under cooldown, daily exhaustion and sender failure. Verify request-level IP throttles and unexpected errors remain visible.
- [x] Keep delivery quota reservations in `AccountStore.issue_code`; normalize only its expected email-specific outcomes at `/api/accounts/code`. Use an acknowledgement that does not assert delivery. Retain sanitized mail-failure diagnostics and the global missing-sender error. Update browser text assertions and documentation.
- [x] Reproduce recovery-after-lockout in `tests/test_account_credential_limits.py`, including authenticated password changes, failed verification, IP/other-account isolation and transaction rollback.
- [x] Make successful credential replacement clear that account's obsolete credential-attempt limits in the same transaction as password update/session revocation. Reuse the transition for reset and authenticated change; preserve independent request/mail counters.
- [x] Run the new failing tests before each fix, then targeted account/browser regression and the complete suite with Playwright enabled. Check the diff and protected primary worktree.

Verification: the initial regression run reproduced six failures (four public-response leaks and reset/change login lockout), with five checks already passing. After correcting the unrelated-account fixture to fill the actual login quota and extending rollback coverage to both reset/change, the credential tests still failed on both uncleared account counters before implementation. The final public-response and credential regression files each pass six checks. Targeted account/browser verification: **65 passed** in 52.87 seconds. Full repository verification with Playwright enabled: **2159 passed, 2 skipped, 154 subtests passed** in 155.29 seconds. `git diff --check` passed and the protected primary `main` worktree stayed clean. Both third-review findings are fixed locally; no commit, push, deployment or real email was performed.

## Production activation follow-through

The user still sees operator-token login because the live release predates the account implementation. Read-only checks confirm the live account module is absent, the options endpoint returns 404 and account environment settings are absent. Existing platform resources belong to the legacy operator, so enabling a new identity without an explicit ownership migration would hide them.

- [x] Reproduce the public token-login page and confirm the missing account module/configuration at the origin.
- [x] Locate the canonical deployment procedure, mail configuration and current platform ownership inventory.
- [ ] Prepare the reviewed account implementation on a branch from the current public repository, preserving private-history protection and the primary worktree.
- [ ] Configure a dedicated one-mail sender and confirm the administrator email. Prepare protected credentials and explicit resource migration before activation.
- [ ] Validate release artifacts and a side-by-side account-mode instance. Back up runtime stores consistently and prepare rollback of code, configuration and ownership.
- [ ] Deploy the account release and enable invitation registration, correct trusted proxy handling and browser-cookie authentication.
- [ ] Verify the public email/password page, login/logout/redirects, mail delivery and visibility of the administrator's existing resources; update the canonical deployment record.
