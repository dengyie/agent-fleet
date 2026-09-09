"""tools/supervisor/control_client.py — 本地拉取式签名控制客户端（Task 9 / §13）。

The Agent only ever *pulls*:
  POST /api/supervisor/poll      (authenticated by supervisor credential)
  POST /api/supervisor/receipts  (bounded, five fields)

Every pulled command passes a single local gate before execution:

* session existence pre-check (opaque probe: none -> bounded ``unknown_session``);
* ``hub.domain.control.validate_command`` — Ed25519 signature over canonical
  bytes, action in the fixed control-action set (incl. ``adopt``/``detach``
  and the default-off Phase 4/5 tokens), machine/session/attempt scope, TTL
  expiry, clock skew, nonce uniqueness, and bounded payload for
  ``append_user_turn`` / ``apply_local_profile``;
* a race-condition state gate mirroring the Hub's conflict precedence
  (``terminate``/``quarantine`` block a stale ``resume``);
* execution through the *local* ``Supervisor`` fixed methods ONLY — never a
  raw/spawn of the command string.

A bounded receipt (``command_id, machine_id, status, reason, received_at``) is
uploaded in every case — success, already-finished, and rejection — so the Hub
never has to guess parse from a bare poll.  No signature, nonce, credential,
path, or arbitrary action value is ever included in a receipt or a log line.

The signed ``adopt`` envelope (Task 8) is the bridge carrier: it may carry a
bounded top-level ``candidate`` (pid / started_at / exe_path / agent_family /
native_file_path) that the Hub snapshotted at adopt-time.  The client NEVER
trusts it blindly — the values feed ``Supervisor.attach_to_existing`` whose
live identity re-read compares against the running process before anything is
bound.  A successful attach (``"adopted"``) starts exactly ONE managed native
transcript through the injected ``bridge_factory`` (a ``SessionBridge``-shaped
object); a failed attach NEVER starts a bridge; ``detach`` flushes + closes
the bridge before dropping the supervised entry, and never signals.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

USER_AGENT = "agent-fleet-control-client/1.0"

from hub.domain import control as ctrl

#: The only actions the client may dispatch — mirror of both the Hub's fixed
#: set and ``tools.supervisor.supervisor.CONTROL_ACTIONS``.
_LOCAL_ACTIONS = frozenset(ctrl.CONTROL_ACTIONS)
_PAYLOAD_ACTIONS = frozenset(ctrl.PAYLOAD_ACTIONS)

#: Race/state gates for a *stale* command the Hub already pre-empted.  If the
#: local session has already been quarantined or terminated, a later resume is
#: never executed (the graceful-decision stays with the Hub's receipt).
_SCOPE_BLOCKED = {
    "resume_session": frozenset({"quarantined", "terminated"}),
    "quarantine_session": frozenset({"quarantined", "terminated"}),
    "terminate_session": frozenset({"terminated"}),
    "cancel_attempt": frozenset({"terminated", "quarantined"}),
    "pause_session": frozenset(),
    "append_user_turn": frozenset({"quarantined", "terminated"}),
    "apply_local_profile": frozenset({"terminated"}),
}

#: Supervisor method callable keyed exactly by control action (fixed surface).
_ACTION_HANDLERS: dict[str, Callable[..., Any]] = {}


def _register_handlers() -> None:
    from tools.supervisor.supervisor import Supervisor

    _ACTION_HANDLERS.update({
        "pause_session": Supervisor.pause_session,
        "resume_session": Supervisor.resume_session,
        "terminate_session": Supervisor.terminate_session,
        "quarantine_session": Supervisor.quarantine_session,
        "cancel_attempt": Supervisor.cancel_attempt,
        "append_user_turn": Supervisor.append_user_turn,
        "apply_local_profile": Supervisor.apply_local_profile,
        # ``detach`` drops the supervised entry WITHOUT touching the process;
        # the bridge close/flush is handled by the client (``_execute``), not
        # by the Supervisor method.  ``adopt`` is NOT in this table — it is a
        # signed-envelope route executed by ``_execute_adopt`` (attach -> one
        # managed native bridge).
        "detach": Supervisor.detach,
    })


_register_handlers()

_EXECUTION_OK = frozenset({
    "pause", "paused", "resume", "resumed", "running",
    "quarantined", "quarantine",
    "terminated", "terminated_forced", "escape_unverified",
    "appended", "applied",
})
#: Guard refusals for an adopted session: bounded codes that MUST never reach
#: a signal; surfaced as ``rejected`` receipts (like the other refused paths).
#: No code in ``_EXECUTION_OK`` overlaps these four — the mapping is additive.
_GUARD_REJECTED = frozenset({
    "adoption_revoked", "pid_reused", "exe_changed", "no_permission",
})
_TERMINAL_STATES = frozenset({"terminated"})

#: No raw/except text ever reaches a receipt reason; map unexpected outcomes to
#: this bounded code.
_UNEXPECTED = "control_failed"


def _rfc3339(ts: float | None = None) -> str:
    ts = time.time() if ts is None else float(ts)
    return (datetime.fromtimestamp(ts, timezone.utc).isoformat()
            .replace("+00:00", "Z"))


#: Bounded length caps for the signed-envelope adoption candidate.  The Hub
#: already bounds these fields at enqueue-time; this is the probe-side gate
#: applied before ANY attach/bridge work (a maliciously oversized envelope
#: candidate is ``invalid_candidate``, nothing is touched).
_CANDIDATE_SHORT_MAX = 128
_CANDIDATE_PATH_MAX = 4096


def _bounded_hash(value: str) -> str:
    """A deterministic opaque digest (hex) for a bounded token/id."""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _validate_candidate(candidate: Any) -> tuple:
    """Validate a signed ``adopt`` candidate; returns a bounded field tuple.

    Returns ``(ok, pid, started_at, exe_path, native_file_path, family)`` where
    ``ok`` is False (all other values ``None``) on ANY violation:

    - ``candidate`` is not a mapping (incl. ``None``);
    - ``pid`` is not a positive non-bool int;
    - ``started_at`` / ``exe_path`` / ``agent_family`` are not non-empty
      bounded strings;
    - ``native_file_path`` is not ``None`` / a bounded string.

    The agent_family is NOT validated here — it is passed through to
    ``Supervisor.attach_to_existing`` whose fixed family gate rejects unknown
    families (``unsupported_family``) before the live probe even runs.
    """
    if not isinstance(candidate, Mapping):
        return (False, None, None, None, None, None)
    pid = candidate.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return (False, None, None, None, None, None)
    for key, cap in (("started_at", _CANDIDATE_SHORT_MAX),
                     ("agent_family", _CANDIDATE_SHORT_MAX),
                     ("exe_path", _CANDIDATE_PATH_MAX)):
        value = candidate.get(key)
        if (not isinstance(value, str) or not value or len(value) > cap):
            return (False, None, None, None, None, None)
    native = candidate.get("native_file_path")
    if native is not None and (
            not isinstance(native, str) or len(native) > _CANDIDATE_PATH_MAX):
        return (False, None, None, None, None, None)
    return (True, int(pid), str(candidate["started_at"]),
            str(candidate["exe_path"]),
            native if isinstance(native, str) else None,
            str(candidate["agent_family"]))


class NonceStore:
    """Durable, fail-closed used-nonce marker store.

    A nonce is "used" iff a marker file whose name is the SHA-256 digest of the
    nonce exists in ``path``.  Availability failures in either direction fail
    *closed* (``used -> True``, ``mark -> False``) so a replay can never slip
    through because the store was unavailable.
    """

    def __init__(self, path: str | Path | None = None):
        if path is None:
            path = Path.home() / ".cache" / "agent-fleet" / "supervisor-used"
        self._dir = Path(path).expanduser()
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._dir = None  # fail-closed: used() returns True

    def _marker(self, nonce: str) -> Path:
        digest = hashlib.sha256(str(nonce).encode("utf-8")).hexdigest()
        return self._dir / f"{digest}.used"

    def used(self, nonce: str) -> bool:
        """True if ``nonce`` was already marked (or the store is unusable)."""
        if not isinstance(nonce, str) or not nonce:
            return True
        if self._dir is None:
            return True
        try:
            return self._marker(nonce).exists()
        except OSError:
            return True  # cannot verify → treat as used

    def mark(self, nonce: str) -> bool:
        """Write a durable marker; False on ANY failure (caller must refuse)."""
        if not isinstance(nonce, str) or not nonce:
            return False
        if self._dir is None:
            return False
        try:
            marker = self._marker(nonce)
            marker.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(marker), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return True  # already marked = used
        except OSError:
            return False
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(b"")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError:
            return False
        return True


class ControlClient:
    """Pulls + validates + dispatches + receipts every supervisor command."""

    def __init__(
        self,
        *,
        hub_url: str,
        machine_id: str,
        credential: str,
        hub_public_key: bytes,
        public_supervisor: Any,
        transport: Callable[[str, Any, Mapping[str, str]], tuple[int, Any]] | None = None,
        nonce_store: NonceStore | None = None,
        max_clock_skew_s: float = 300.0,
        terminate_grace_s: float = 0.0,
        bridge_factory: Callable[[dict], Any] | None = None,
        spool: Any | None = None,
        spool_root: str | os.PathLike | None = None,
        spool_key: bytes | None = None,
        uploader: Any | None = None,
        post_json: Callable[[dict], Any] | None = None,
    ):
        if not isinstance(machine_id, str) or not machine_id:
            raise ValueError("machine_id required")
        self._hub_url = str(hub_url or "")
        self._machine_id = machine_id
        self._credential = credential
        self._public_key = hub_public_key
        self._supervisor = public_supervisor
        self._transport = transport or self._http_transport
        self._nonces = nonce_store or NonceStore()
        self._skew = float(max_clock_skew_s or 300.0)
        self._terminate_grace_s = float(terminate_grace_s)
        # ---- adopt -> bridge wiring (Task 8) --------------------------------
        # ``bridge_factory`` builds a ``SessionBridge``-shaped object from a
        # bounded config; None means the probe has NO bridge capability, so an
        # adopt fails closed BEFORE any attach (``bridge_unavailable``).
        self._bridge_factory = bridge_factory
        #: session_id -> live bridge (one per adopted session)
        self._bridges: dict[str, Any] = {}
        # optional spool/uploader wiring surfaced into the bridge config for
        # the real ``SessionBridge``; absent values are simply not carried.
        self._spool = spool
        self._spool_root = Path(spool_root) if spool_root is not None else None
        self._spool_key = spool_key
        self._uploader = uploader
        self._post_json = post_json

    # ------------------------------------------------------------------ #
    # probe gate
    # ------------------------------------------------------------------ #

    def poll_once(self) -> dict[str, Any]:
        """One pull cycle; returns ``{"ok":True,"commands":n}`` / ``{"ok":False,...}``.

        Never raises on a transport/parse failure — a degraded result surface is
        what the caller logs/backs off on.
        """
        try:
            status, body = self._transport(
                "/api/supervisor/poll", None, self._headers())
        except Exception:
            return {"ok": False, "commands": 0, "error": "poll_transport_error"}
        if status != 200 or not isinstance(body, dict) or not body.get("ok"):
            detail = (body or {}).get("error", "poll_failed") \
                if isinstance(body, dict) else "poll_failed"
            return {"ok": False, "commands": 0, "error": str(detail)[:80]}

        commands = body.get("commands")
        if not isinstance(commands, list):
            return {"ok": False, "commands": 0, "error": "malformed_commands"}
        handled = 0
        for command in commands:
            if isinstance(command, dict):
                self._handle_command(command)
                handled += 1
        return {"ok": True, "commands": handled}

    # ------------------------------------------------------------------
    # per-command gate + execution (+ bounded receipt)
    # ------------------------------------------------------------------

    def _handle_command(self, command: Mapping[str, Any]) -> None:
        cid = str(command.get("command_id") or "")[:160]
        target = command.get("target")
        session_id = target.get("session_id") if isinstance(target, dict) else None
        attempt_id = target.get("attempt_id") if isinstance(target, dict) else None
        action = command.get("action")

        # 1. session existence pre-check (opaque; never trusts argv/env).
        # For ``adopt`` ALONE the pre-check is bypassed: the command targets an
        # ``adopt_*`` session that does not exist yet in the local supervisor
        # manifest — that is exactly the point of a pending adoption.  detach
        # and every other action still require a live session and a missing one
        # yields the bounded ``unknown_session`` receipt.
        state = None
        if action == "adopt":
            if not (isinstance(session_id, str) and session_id):
                self._receipt(cid, "rejected", "scope_mismatch")
                return
        else:
            if isinstance(session_id, str) and session_id:
                try:
                    manifest = self._supervisor.get(session_id)
                    state = manifest.state
                except KeyError:
                    # A revokee-side control on a no-longer-bound adoption.
                    # The operator may have revoked this seat long ago and the
                    # probe already ``detach``ed it; answer that bounded code
                    # instead of the generic unknown (a truly-unknown session
                    # still yields ``unknown_session``).
                    code = "unknown_session"
                    try:
                        identity = self._supervisor.validate_attached_identity(
                            session_id)
                    except Exception:
                        identity = ""   # truly unknown — not an adoption
                    if identity == "adoption_revoked":
                        code = "adoption_revoked"
                    self._receipt(cid, "rejected", code)
                    return
            else:
                self._receipt(cid, "rejected", "scope_mismatch")
                return

            # 1b. attempt-scope gate (spec §13 session/attempt binding).  The
            # command's target.attempt_id MUST match the local manifest's
            # bound attempt: a crafted attempt on a session bound to another
            # attempt (or to None) is a bounded ``attempt_mismatch`` rejection,
            # and the session is NEVER touched.  A command with no attempt
            # matches only a manifest bound to None.
            manifest_attempt = getattr(manifest, "attempt_id", None)
            if manifest_attempt is None:
                if attempt_id is not None:
                    self._receipt(cid, "rejected", "attempt_mismatch")
                    return
            else:
                if attempt_id != manifest_attempt:
                    self._receipt(cid, "rejected", "attempt_mismatch")
                    return

        # 2. signature / scope / TTL / nonce gate (single authority).
        ok, code = ctrl.validate_command(
            command,
            public_key=self._public_key,
            expected_machine=self._machine_id,
            now=_now(),
            max_clock_skew_s=self._skew,
            nonce_used=self._nonces.used,
        )
        if not ok:
            self._receipt(cid, "rejected", code)
            return

        # 3. persist the used nonce before dispatch (fail-closed on store loss).
        nonce = command.get("nonce")
        if not self._nonces.mark(nonce):
            self._receipt(cid, "rejected", "nonce_replay")
            return

        # 4. local conflict/state gate (a stale resume never resurrects a
        # quarantined/terminated session).
        if action in _SCOPE_BLOCKED and state in _SCOPE_BLOCKED[action]:
            self._receipt(cid, "rejected", "scope_mismatch")
            return

        # 5. execute through the fixed Supervisor methods.  ``adopt`` is the
        # only signature-carrier route: it materializes the bounded candidate
        # into an attach + (on success) ONE managed native bridge.  Phase 4/5
        # actions carry a bounded ``payload`` already verified by
        # ``validate_command``; they never write the live process stdin.
        if action == "adopt":
            status, reason = self._execute_adopt(command, cid, session_id)
        elif action in _PAYLOAD_ACTIONS:
            status, reason = self._execute_payload(action, cid, session_id,
                                                   command.get("payload"))
        else:
            status, reason = self._execute(action, cid, session_id)
        self._receipt(cid, status, reason)

    def _execute_adopt(self, command, cid: str, session_id: str):
        """Execute a signed ``adopt``: feed the candidate through the attach +
        native-bridge flow.  Returns ``(status, reason)``; reason is ALWAYS a
        fixed bounded token.

        Ordering matters (each step fails closed BEFORE the next):

        1. candidate validation — a malformed/missing ``candidate`` is rejected
           with ``invalid_candidate``; nothing is touched (no bridge, no
           supervisor mutation);
        2. bridge-capability gate — ``None`` factory means the probe has no
           bridge: ``bridge_unavailable`` BEFORE any attach (nothing to roll
           back);
        3. ``Supervisor.attach_to_existing`` — the re-read gate compares the
           wire identity against the LIVE process; any non-``adopted`` result
           is a bounded rejection (``pid_reused``/``exe_changed``/…) and the
           factory is never invoked;
        4. ON REAL ATTACH SUCCESS ONLY — construct + start + ingest the ONE
           managed native bridge; any construction exception rolls back the
           private entry with ``_safe_detach`` and yields ``capture_gap``.
        """
        candidate = command.get("candidate")
        ok, pid, started_at, exe_path, native_file_path, family = \
            _validate_candidate(candidate)
        if not ok:
            # envelope signed by the Hub but candidate missing/malformed —
            # reject BEFORE attach; nothing to clean up.
            return ("rejected", "invalid_candidate")
        if self._bridge_factory is None:
            # probe has no bridge capability: refuse before attach, so no
            # private entry is EVER created (nothing to roll back).
            return ("rejected", "bridge_unavailable")
        rc = self._supervisor.attach_to_existing(
            pid, started_at, exe_path, native_file_path, family,
            session_id=session_id)
        if rc != "adopted":
            return ("rejected", rc)   # pid_reused / exe_changed / …
        # bridge construction — ONLY on a real attach success.
        try:
            cfg = self._build_bridge_config(session_id, family,
                                            native_file_path)
            bridge = self._bridge_factory(cfg)
            bridge.start()
            if native_file_path:
                bridge.ingest_native(
                    native_file_path, checkpoint_path=cfg["checkpoint_path"])
            self._bridges[session_id] = bridge
        except Exception:
            # rollback the just-attached private entry; never a signal, never
            # a raw exception/reason on the wire.
            self._safe_detach(session_id)
            return ("rejected", "capture_gap")
        return ("accepted", "adopted")

    def _build_bridge_config(self, session_id: str, family: str,
                             native_file_path: str | None) -> dict:
        """Bounded adopt bridge config for the injected factory.

        Mirrors the brief "Adopt config": the client's own authenticated
        machine_id, an opaque stream id, the SUPERVISOR's opaque
        ``process_group_id`` (NEVER a raw pid/path), ``managed=True`` (attach
        succeeded), the adopt ``best_effort`` clamp, and — when configured — the
        existing spool/uploader wiring plus a checkpoint path underneath the
        spool root.
        """
        status = self._supervisor.status(session_id)
        if not isinstance(status, dict) or not status:
            raise RuntimeError("status unreadable")
        group_id = status.get("process_group_id") or ""
        if not isinstance(group_id, str):
            group_id = ""
        cfg: dict[str, Any] = {
            "session_id": session_id,
            "machine_id": self._machine_id,
            "stream_id": f"stream_{_bounded_hash(session_id)[:16]}",
            "agent_family": family,
            "process_group_id": group_id,
            "attempt_id": None,
            "managed": True,
            "best_effort": True,
            "native_file_path": native_file_path,
            "checkpoint_path": self._checkpoint_path(session_id),
        }
        if self._uploader is not None:
            cfg["uploader"] = self._uploader
        elif self._post_json is not None:
            cfg["post_json"] = self._post_json
        if self._spool is not None:
            cfg["spool"] = self._spool
        elif self._spool_root is not None:
            cfg["spool_root"] = str(self._spool_root)
            if self._spool_key is not None:
                cfg["spool_key"] = bytes(self._spool_key)
        return cfg

    def _checkpoint_path(self, session_id: str) -> str | None:
        """A bounded checkpoint path UNDER the configured spool root (if any)."""
        if self._spool_root is None:
            return None
        return str(self._spool_root / "checkpoints"
                   / f"native-{session_id[:64]}.json")

    def _safe_detach(self, session_id: str) -> None:
        """Rollback an adopt attach: drop the just-created private entry.

        Never signals; idempotent; no exception escapes.  Even if the
        supervisor is mid-flight (attach succeeded but the bridge could not be
        built), the entry is dropped so a failed adopt leaves NOTHING behind.
        """
        try:
            self._supervisor.detach(session_id)
        except Exception:
            pass

    @property
    def supervisor(self):
        """The bound public supervisor (read alias for tests / wiring)."""
        return self._supervisor

    def iter_bridges(self):
        """Yield the live adopted bridges (pump ticks iterate these)."""
        return list(self._bridges.values())

    def _execute_payload(self, action: str, cid: str, session_id: str,
                         payload: Any):
        """Execute a Phase 4/5 action from a bounded signed payload.

        Extra keys were already refused by ``validate_command``.  This path
        never writes the live process stdin and never copies credentials.
        """
        if action not in _PAYLOAD_ACTIONS:
            return "rejected", "unsupported_action"
        try:
            clean = ctrl.validated_control_payload(action, payload)
        except ValueError:
            return "rejected", "invalid_payload"
        try:
            if action == "append_user_turn":
                result = self._supervisor.append_user_turn(
                    session_id, clean["text"])
            else:
                result = self._supervisor.apply_local_profile(
                    session_id, clean["profile_id"])
        except KeyError:
            return ("rejected", "unknown_session")
        except Exception:
            return ("failed", _UNEXPECTED)
        result = str(result or "")
        if result in _GUARD_REJECTED:
            return ("rejected", result)
        if result in ("unsupported_action", "resume_unverified",
                      "unknown_profile", "family_mismatch",
                      "invalid_payload"):
            return ("rejected", result)
        if result in _EXECUTION_OK:
            return ("succeeded", result)
        if result in ("already_finished", "no_live_process"):
            return ("already_finished", result)
        return ("failed", result or _UNEXPECTED)

    def _execute(self, action: str, cid: str, session_id: str):
        if action not in _LOCAL_ACTIONS:
            return "rejected", "unsupported_action"
        if action == "detach":
            # Close the managed tail BEFORE dropping governance: push the
            # remaining bounded events out, then stop reading + uploading
            # permanently (``flush`` then ``close``; both contained so a
            # failing tail can never abort the detach or signal the process).
            first = self._bridges.pop(session_id, None)
            if first is not None:
                try:
                    first.flush()
                except Exception:
                    pass
                try:
                    first.close()
                except Exception:
                    pass
            try:
                self._supervisor.detach(session_id)
            except KeyError:  # UnknownSessionError - lost mid-flight
                return ("rejected", "unknown_session")
            except Exception:
                return ("failed", _UNEXPECTED)
            return ("succeeded", "detached")

        handler = _ACTION_HANDLERS.get(action)
        if handler is None:
            return "rejected", "unsupported_action"
        try:
            if action == "terminate_session":
                # Bounded, offline-safe grace for an operator terminate.
                result = self._supervisor.terminate_session(
                    session_id, grace_s=self._terminate_grace_s)
            elif action == "cancel_attempt":
                result = self._supervisor.cancel_attempt(session_id)
            else:
                result = handler(self._supervisor, session_id)
        except KeyError:  # UnknownSessionError - lost mid-flight
            return ("rejected", "unknown_session")
        except Exception:
            return ("failed", _UNEXPECTED)

        result = str(result or "")
        if result in _GUARD_REJECTED:
            # an adopted session's identity/adoption guard refused BEFORE any
            # signal — a bounded ``rejected`` receipt, never a signal attempt.
            return ("rejected", result)
        if result in _EXECUTION_OK:
            return ("succeeded", result)
        if result in ("already_finished", "no_live_process"):
            return ("already_finished", result)
        if result in _TERMINAL_STATES:
            return ("succeeded", result)
        return ("failed", result or _UNEXPECTED)

    def _reject(self, cid: str, reason: str) -> None:
        self._receipt(cid, "rejected", reason)

    def _receipt(self, cid: str, status: str, reason: str) -> None:
        # Bounded in every dimension: keys exactly, reason capped.
        body = {
            "command_id": str(cid)[:160],
            "machine_id": self._machine_id,
            "status": str(status)[:32],
            "reason": str(reason)[:120],
            "received_at": _rfc3339(),
        }
        try:
            self._transport("/api/supervisor/receipts", body, self._headers())
        except Exception:
            pass  # best-effort; the receipt never contains anything sensitive.

    def _headers(self) -> dict[str, str]:
        return {
            "X-Supervisor-Credential": f"{self._machine_id}:{self._credential}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }

    # ------------------------------------------------------------------
    # default HTTP transport (replaced in tests)
    # ------------------------------------------------------------------

    def _http_transport(self, path: str, body: Any, headers: dict[str, str]):
        url = (self._hub_url.rstrip("/") + "/" + path.lstrip("/"))
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                payload = json.loads(raw) if raw else {}
                return int(resp.status), payload
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8")
                payload = json.loads(raw) if raw else {}
            except Exception:
                payload = {}
            return int(exc.code), payload if isinstance(payload, dict) else {}


def _now() -> float:
    return time.time()


__all__ = ["ControlClient", "NonceStore"]