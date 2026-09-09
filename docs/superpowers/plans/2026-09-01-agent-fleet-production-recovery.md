# Agent Fleet Production Recovery and Topology Implementation Plan

> **Status (2026-09-08): historical / implemented.** Open checkboxes below are the original plan and were not kept in sync after merge. Do not re-implement from unchecked boxes. Remaining work is recorded in `docs/HANDOFF.md` §六 as `done` or `deferred-with-condition`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore `hub.example.com` from the 502 state, establish one evidence-backed production route and one reversible release/guardian lifecycle, and record the result in the canonical operational documentation without exposing credentials.

**Architecture:** Treat the Docker container path as the production path until live evidence proves otherwise: Cloudflare edge → host Nginx → container address `127.0.0.1:8790` → guardian-managed web process. The host `127.0.0.1:8790` process is an emergency observation point, not a production cutover target. Recovery is read-only-first, then the smallest reversible container-side action; no SSH, shell, or file-write capability is added to the Hub.

**Tech Stack:** Docker/host Nginx, Cloudflare proxy, bash guardian, Python 3/Flask, release symlink, curl, `pytest`.

**Spec:** `docs/superpowers/specs/2026-08-29-agent-instance-discovery-adoption-design.md` for the non-invasive control boundary; operational truth is additionally constrained by Obsidian `00.MOC/AI-DOC-ROUTER.md` and `Note/Infra/agent-fleet 2026-08-31 线上部署 incident.md`.

## Global Constraints

- Read `00.MOC/AI-DOC-ROUTER.md` before any production or Obsidian operation; use the canonical agent-fleet entries and name conflicts explicitly.
- Do not print, copy, paste, grep, or log token values, SSH passwords, cookies, runner credentials, ingest credentials, private keys, or full credential files.
- Do not use chat plaintext as a `sudo` password; if an interactive privilege step is required, stop and provide a user-run `! <command>` instruction.
- Preserve a rollback target before changing `live`, guardian scripts, Nginx, or container state.
- Recovery must restore the legacy `/api/status` path before enabling new session, supervisor, or adoption behavior.
- The Hub remains push-only and must not gain SSH, reverse tunnel, arbitrary remote shell, arbitrary file write, or browser-to-agent direct connectivity.
- All production probes must use status codes, bounded headers, process metadata, symlink targets, and file-existence checks only; never dump response bodies containing sensitive data.
- Every mutation has a bounded blast radius, an explicit stop condition, and a rollback command that does not require credentials in chat.

---

## File Structure

Repository files used for validation and release preparation:

- `deploy/package-release.sh`: backend release archive; must include every runtime Python package, including top-level `agent_profiles.py`.
- `deploy/hk-container-adopt-release.sh`: existing release adoption guard; use only after confirming its directory/symlink assumptions match the live container.
- `deploy/hk-container-inspect.sh`: bounded inspection helper; extend only if a missing safe fact cannot be collected without exposing secrets.
- `deploy/hk-container-install.sh`: current direct-start installer; document its lack of guardian supervision before using it as a fallback.
- `hub/web.py`: compatibility entry point and runtime gate resolution on the adoption branch.
- `tests/test_feature_gate_runtime.py`: gate precedence and default-off contract.
- `docs/HANDOFF.md`, `README.md`, `docs/architecture-v3.md`: repository-facing deployment and topology statements.

Obsidian files to update only after live recovery is verified:

- `00.MOC/AI-DOC-ROUTER.md`: consolidate the two conflicting agent-fleet rows.
- `Note/Infra/agent-fleet 部署与运维.md`: canonical operational SOP and rollback.
- `Note/Infra/agent-fleet 架构与自动化运维手册.md`: canonical topology and release model.
- `Note/Infra/agent-fleet 2026-08-31 线上部署 incident.md`: close recovery evidence and preserve the incident timeline.
- `Note/Infra/GitHub CI 健康总览.md`: correct the stale “all green implies production healthy” statement.
- `00.MOC/项目索引.md`: add the missing project link if the canonical routing cleanup confirms it belongs there.

---

## Phase 0: Read-only production truth

### Task 1: Capture a redacted route and process evidence bundle

**Owner:** main; hakus performs an independent read-only comparison and reports contradictions.

**Files:**
- Read: `00.MOC/AI-DOC-ROUTER.md`
- Read: `Note/Infra/agent-fleet 部署与运维.md`
- Read: `Note/Infra/agent-fleet 架构与自动化运维手册.md`
- Read: `Note/Infra/agent-fleet 2026-08-31 线上部署 incident.md`
- Inspect: host Nginx, container network/process metadata, release symlinks
- Create locally: a redacted evidence table in the operator notes, never raw command output

**Interfaces:**
- Consumes: the canonical route table and the incident’s expected probes.
- Produces: a bounded table containing HTTP status, upstream address, listener ownership, guardian/web existence, `cwd` release id, `live` target, and frontend title classification. It must not contain response bodies, environment dumps, process arguments with secrets, or credential file contents.

- [ ] **Step 1: Run public and loopback status checks without response bodies**

```bash
curl -sS -o /dev/null -w 'public=%{http_code}\n' https://hub.example.com/api/status
curl -sS -o /dev/null -w 'container=%{http_code}\n' http://127.0.0.1:8790/api/status
curl -sS -o /dev/null -w 'host=%{http_code}\n' http://127.0.0.1:8790/api/status
```

Expected: record all three codes; do not assume the production route from one probe.

- [ ] **Step 2: Locate the actual Nginx upstream with a bounded filter**

```bash
nginx -T 2>/dev/null | grep -A8 -B2 'server_name hub.example.com'
ss -ltnp | grep ':8790' || true
```

Expected: identify whether the active upstream is `127.0.0.1:8790` or host loopback. Redact unrelated virtual hosts and all credential-like values from the evidence.

- [ ] **Step 3: Inspect guardian/web identity without dumping command lines**

```bash
pgrep -af 'guardian|web.py|vendor-run|hermes' | sed -E 's/(token|password|secret|credential)[^ ]*/REDACTED/Ig'
for p in $(pgrep -f 'python3.*hub/web.py' || true); do
  printf 'pid=%s cwd=' "$p"
  readlink "/proc/$p/cwd" || true
  printf 'exe='
  readlink "/proc/$p/exe" || true
  printf '\n'
done
```

Expected: record only process class, PID existence, executable, and release cwd. Never include full argv, environment, or `/proc/*/environ`.

- [ ] **Step 4: Inspect release/live metadata and the known debug marker**

```bash
ls -ld ${HOME}/agent-fleet ${HOME}/.hermes/agent-fleet-releases 2>/dev/null || true
find ${HOME}/.hermes/agent-fleet-releases -maxdepth 2 -type f -name 'hub/web.py' -print 2>/dev/null | sort
```

Expected: record release IDs and symlink type only; separately record whether `.hermes/.f-diag-run` and `guardian.sh.bak` exist, without printing their contents.

- [ ] **Step 5: Classify the frontend without downloading the whole page**

```bash
curl -sS https://hub.example.com/ | tr '\n' ' ' | grep -o '<title>[^<]*</title>' | cut -c1-120 || true
```

Expected: classify `frontend/` versus `frontend-dist/` using the bounded title only.

- [ ] **Step 6: Write the decision record**

Record one of the following explicit outcomes: `container-production`, `host-production`, or `unresolved-stop`. If Nginx, public failure correlation, and listener evidence disagree, stop without mutation and escalate the contradiction to the user.

- [ ] **Step 7: Commit only local evidence/documentation changes if any**

```bash
git diff --check
```

Expected: no code commit in this task; production evidence is recorded only in the canonical incident note after recovery authorization and without secret values.

**Rollback:** none; all operations are read-only.

---

## Phase 1: Restore service with the smallest reversible action

### Task 2: Diagnose the container import failure in-place

**Owner:** main; hakus may review the proposed diagnosis but must not mutate the container.

**Files:**
- Inspect only the staged release under the container’s existing release root.
- Modify only if the diagnosis proves the package boundary is the cause: `tools/__init__.py` or release packaging inputs.
- Test: a bounded import probe and local `py_compile`.

**Interfaces:**
- Consumes: Task 1’s confirmed container `cwd`, Python executable, and release id.
- Produces: either a proven import correction or a preserved release with a precise failure category. The probe must print only `sys.executable`, sanitized `sys.path` roots, `tools.__path__`, and import success/failure class; never environment values.

- [ ] **Step 1: Run the import probe with bounded output**

```bash
python3 - <<'PY'
import importlib.util
import sys
from pathlib import Path
print('python=' + Path(sys.executable).name)
print('path_roots=' + '|'.join(sorted({Path(p).name for p in sys.path if p})))
try:
    import tools
    print('tools=' + '|'.join(Path(p).name for p in getattr(tools, '__path__', ())))
    import tools.session
    print('tools.session=ok')
except Exception as exc:
    print('import_class=' + type(exc).__name__)
    raise SystemExit(2)
PY
```

Expected: determine whether the failure is namespace-package resolution, cwd/path setup, incomplete archive contents, or a different dependency. Do not print the exception message if it could include a path or secret.

- [ ] **Step 2: Compare archive membership to the import contract**

```bash
tar -tzf /tmp/agent-fleet-release.tgz | grep -E '(^|/)tools(/|$)|agent_profiles\.py$' | cut -c1-200
```

Expected: `tools/session/__init__.py`, required modules, and `agent_profiles.py` are present. If the archive is incomplete, update `deploy/package-release.sh` and add a regression test for required top-level/package paths before repackaging.

- [ ] **Step 3: Add the smallest package-boundary fix only when reproduced**

If and only if Python reports a namespace/package resolution problem while the files are present, add an empty `tools/__init__.py` to the release source and add a test that imports `tools.session` from a copied release root. Do not change unrelated import paths or add runtime path mutation.

- [ ] **Step 4: Verify local and release imports**

```bash
python3 -m py_compile tools/session/*.py tools/supervisor/*.py hub/web.py
python3 -c 'import tools.session; print("tools.session=ok")'
```

Expected: both pass; no credential output.

- [ ] **Step 5: Commit the packaging/import correction**

```bash
git add deploy/package-release.sh tools/__init__.py tests
 git commit -m "fix: make release package imports explicit"
```

Expected: commit includes only the reproduced import fix and its regression test.

**Rollback:** revert the commit and select the previously known-good release; never repair imports by disabling authentication or copying credential files.

### Task 3: Remove one-shot diagnostics and restore guardian source

**Owner:** main after Task 1 confirms container production; any destructive cleanup requires an explicit production change window.

**Files:**
- Container `.hermes/.f-diag-run`: delete only after recording existence.
- Container `.hermes/scripts/agent-fleet-guardian.sh`: restore from the verified `.bak` or known-good source.
- Preserve a timestamped backup outside the live path before replacement.

**Interfaces:**
- Consumes: Task 1 evidence and the original guardian checksum/backup.
- Produces: an unmodified guardian with one deterministic web start path, no diagnostic marker, and no secret-bearing output.

- [ ] **Step 1: Verify the backup and target are distinct without printing contents**

```bash
sha256sum /path/to/guardian.sh /path/to/guardian.sh.bak
```

Expected: if the backup is missing or the hashes do not establish which file is known-good, stop. Do not overwrite an ambiguous file.

- [ ] **Step 2: Save a rollback copy**

```bash
cp -p /path/to/guardian.sh /path/to/guardian.sh.pre-recovery
```

Expected: the pre-recovery copy exists and has the same mode/owner; record only path, mode, and hash.

- [ ] **Step 3: Restore the known-good guardian and remove only the marker**

```bash
cp -p /path/to/guardian.sh.bak /path/to/guardian.sh
rm -f /path/to/.f-diag-run
```

Expected: guardian checksum matches the known-good backup; no other `.hermes` files change.

- [ ] **Step 4: Validate shell syntax without starting anything**

```bash
bash -n /path/to/guardian.sh
```

Expected: exit 0.

**Rollback:** restore `guardian.sh.pre-recovery`; recreate `.f-diag-run` only if an operator explicitly requests renewed diagnostics.

### Task 4: Restart the real supervisor entry and prove public recovery

**Owner:** main; user/operator supplies the required privileged execution channel if `docker restart` is blocked.

**Files:**
- Runtime only: `hermes-gateway` container and guardian-managed process.
- Test: bounded public smoke commands.

**Interfaces:**
- Consumes: restored importable release, known-good guardian, confirmed production route.
- Produces: public `/api/status` success, stable guardian/web process, and a release cwd matching the intended release. New gates remain off unless separately authorized in the rollout plan.

- [ ] **Step 1: Check the intended rollback target before restart**

```bash
readlink ${HOME}/agent-fleet 2>/dev/null || true
ls -ld ${HOME}/.hermes/agent-fleet-releases/<known-good-id> 2>/dev/null || true
```

Expected: known-good release is present. If not, restore service from the last verified release rather than the staged release.

- [ ] **Step 2: Restart through the actual production owner**

Use the existing authorized container/supervisor mechanism, not an ad-hoc background `python hub/web.py`. If the current account cannot execute it, provide the exact user-run command with redacted paths and wait for the result; do not attempt password-based escalation.

- [ ] **Step 3: Poll all three bounded endpoints**

```bash
for i in $(seq 1 30); do
  public=$(curl -sS -o /dev/null -w '%{http_code}' https://hub.example.com/api/status || true)
  container=$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8790/api/status || true)
  [ "$public" = 200 ] && [ "$container" = 200 ] && break
  sleep 2
done
printf 'public=%s container=%s\n' "$public" "$container"
```

Expected: both production-path codes are 200. A 502, 000, or unstable sequence is a failed recovery; do not advance gates.

- [ ] **Step 4: Verify route presence without exposing payloads**

```bash
for path in /api/sessions /api/adoptions /api/supervisor/poll; do
  curl -sS -o /dev/null -w "$path=%{http_code}\n" "https://hub.example.com$path" || true
done
```

Expected: new routes may be `404` while their gates are off; `/api/status` must remain 200. Record the exact bounded codes.

- [ ] **Step 5: Verify process cwd and guardian stability**

```bash
pgrep -af 'guardian|web.py' | sed -E 's/(token|password|secret|credential)[^ ]*/REDACTED/Ig'
```

Then read only `/proc/<pid>/cwd` for the selected web PID. Repeat after 60 seconds. Expected: guardian and web remain present and cwd does not drift.

- [ ] **Step 6: Run local regression tests before declaring recovery complete**

```bash
python3 -m pytest -q
bash deploy/test-static-frontend.sh
bash deploy/test-release-routing.sh
```

Expected: tests pass; no production endpoint is used by the local scripts unless explicitly passed by the operator.

**Rollback:** switch `live` to the known-good release, restart through the same supervisor owner, and repeat the bounded endpoint checks. Do not use the host web bypass unless Nginx evidence proves it is the active upstream.

---

## Phase 2: Canonical topology and operational documentation

### Task 5: Normalize repository deployment documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/HANDOFF.md`
- Modify: `docs/architecture-v3.md`
- Modify: `deploy/nginx-agent-site.conf`
- Modify: `deploy/nginx-expose.md`
- Modify: `deploy/cloudflare-access.md`
- Modify: `deploy/frontend-release-layout.md`
- Modify: `deploy/hk-container-install.sh`, `deploy/hk-container-adopt-release.sh`, `deploy/hk-container-inspect.sh` only where comments/guardrails are inaccurate
- Test: `tests/test_release_layout.py` and any deployment script syntax checks

**Interfaces:**
- Consumes: Task 1–4 evidence and the actual release/guardian mechanism.
- Produces: one clearly labeled production topology, one release/live contract, one rollback SOP, and explicit labels for example versus production configuration.

- [ ] **Step 1: Replace contradictory topology prose**

State explicitly whether evidence established `CF → host Nginx → 127.0.0.1:8790`. Mark host `127.0.0.1:8790` as a bypass/diagnostic process only if evidence supports that role. Do not describe both host and container as current production.

- [ ] **Step 2: Separate frontend source names**

Use distinct names for the shipped static `frontend/` release and the React/Vite source/build directories `frontend-v2/` and `frontend-dist/`. The chosen production artifact must be named in the release instructions; the other must be labeled legacy, source-only, or non-production based on evidence.

- [ ] **Step 3: Document the guardian owner and restart contract**

Document the one supported start/restart owner, the expected web cwd, the health check, and the rollback symlink operation. Explicitly mark `hk-container-install.sh` direct start as bootstrap-only if it does not supervise the process.

- [ ] **Step 4: Add regression checks for contradictory phrases**

Add a text-level test or shell check that fails if canonical deployment docs simultaneously call host and container “current production,” or if an example Nginx file is presented as production. Keep the test bounded to exact phrases, not broad prose matching.

- [ ] **Step 5: Run validation**

```bash
bash -n deploy/*.sh
python3 -m pytest -q tests/test_release_layout.py
python3 -m compileall -q hub tools connectors tests
```

Expected: all checks pass.

- [ ] **Step 6: Commit repository documentation only**

```bash
git add README.md docs deploy tests
 git commit -m "docs: make production topology and rollback explicit"
```

**Rollback:** revert the documentation commit; no runtime behavior changes.

### Task 6: Close the incident and repair Obsidian routing

**Files:**
- Modify: `Note/Infra/agent-fleet 2026-08-31 线上部署 incident.md`
- Modify: `Note/Infra/agent-fleet 部署与运维.md`
- Modify: `Note/Infra/agent-fleet 架构与自动化运维手册.md`
- Modify: `00.MOC/AI-DOC-ROUTER.md`
- Modify: `00.MOC/项目索引.md` if the project link is missing
- Modify: `Note/Infra/GitHub CI 健康总览.md`

**Interfaces:**
- Consumes: verified production evidence and repository documentation from Task 5.
- Produces: canonical notes with one route, one topology, an incident closure status, and no secret values.

- [ ] **Step 1: Update the incident status and evidence table**

Change `status: incident-open` only after public recovery and guardian stability pass. Record timestamps, bounded status codes, release IDs, and rollback target. Correct the stale PR reference to actual PR `#1`; if “Task 9” is intended, say so explicitly.

- [ ] **Step 2: Consolidate the two router rows**

Keep one agent-fleet canonical row with trigger words covering production, guardian, 8790, release, runner, session, supervisor, adoption, and rollback. Link the other operational manual as the primary downlink rather than retaining two contradictory authority rows.

- [ ] **Step 3: Move superseded topology into historical context**

Do not delete history blindly. Mark the old host/container claim as superseded with its date and link to the incident evidence; the canonical “current truth” section must remain unambiguous.

- [ ] **Step 4: Run the Obsidian health check**

```bash
cd "$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents/obsidian-note"
python3 .local/bin/scan-stale-docs
```

Expected: no broken links, parser errors, or stale canonical claims introduced by this change.

- [ ] **Step 5: Verify no credential material entered the notes**

```bash
git diff --no-index /dev/null /tmp/redacted-note-check 2>/dev/null || true
```

Use a manual bounded review of changed lines; do not run a broad secret scanner that prints matched secrets. Only credential paths and rotation actions may be named.

**Rollback:** restore the prior note versions from version control/Obsidian history; keep the incident record if it contains the only recovery evidence.

---

## Completion Gate

The plan is complete only when all of the following are true:

- Public `/api/status` is 200 and the confirmed production upstream is stable across two checks at least 60 seconds apart.
- Guardian and web are both owned by the documented supervisor path; the web cwd points to the intended release.
- `.f-diag-run` is absent and guardian source matches the verified known-good copy.
- New feature gates remain off until the separate gated rollout plan passes its own approval gates.
- Repository tests, compile checks, static frontend smoke, and route-contract checks pass.
- Repository docs and Obsidian canonical docs agree on production topology, release/live semantics, frontend artifact, and rollback.
- The incident references actual PR `#1`, contains no secret values, and has an explicit closure or unresolved-stop status.
