"""Node-side authenticated poll/execute loop with journal idempotency."""
from __future__ import annotations

from collections.abc import Mapping
import json
import time
from typing import Any

from fleet_core.platform_command import args_hash, verify_command
from fleet_core.web_agent_contract import WEB_AGENT_TOOLS
from fleet_core.artifact_receive import RECEIVE_ACTION
from fleet_core.artifact_export import EXPORT_ACTION
from fleet_core.capability_execution import CAPABILITY_ARGUMENT
from fleet_core.remote_workflow_test import REMOTE_TEST_ACTION
from .command_control import CommandControl, ControlRejected


class BrowserSessionSyncError(RuntimeError):
    """Bounded session sync failure with an explicit cleanup outcome."""

    def __init__(self, *, sync_error: Exception, cleanup_error: Exception | None = None):
        self.code = "browser_session_sync_failed"
        self.sync_error = sync_error
        self.cleanup_error = cleanup_error
        self.cleanup_failed = cleanup_error is not None
        super().__init__(self.code)


class NodeClient:
    def __init__(self, journal, *, executor, clock=time.time, node_id=None,
                 credential=None, hub_url=None, transport=None, public_key=None,
                 require_signature=False, worker_id=None, should_cancel=None):
        self.journal = journal
        self.executor = executor
        self.clock = clock
        self.node_id = node_id
        self.credential = credential
        self.hub_url = str(hub_url or "").rstrip("/")
        self.transport = transport
        self.public_key = public_key
        self.require_signature = bool(require_signature)
        self.worker_id = worker_id or node_id
        self.should_cancel = should_cancel

    def _headers(self):
        if not self.node_id or not self.credential:
            raise ValueError("node transport credentials required")
        return {
            "Content-Type": "application/json",
            "User-Agent": "agent-fleet-node/1.0",
            "X-Platform-Node-Credential": self.credential,
        }

    def _post(self, path, body, *, bounded=False):
        if self.transport is None:
            from tools.transport import Transport
            self.transport = Transport()
        url = self.hub_url + path
        if not url.startswith(("http://", "https://")):
            raise ValueError("hub_url must use http or https")
        if bounded:
            exporter = self.executor.artifact_exporter
            return self.transport.request_bounded_json("POST", url,
                json.dumps(body, allow_nan=False, separators=(",", ":")).encode("utf-8"), self._headers(),
                deadline=time.monotonic() + 15, should_cancel=exporter.should_cancel)
        return self.transport.post_json(url, body, self._headers())

    def _post_bytes(self, path, body: bytes, headers: Mapping[str, str]):
        if self.transport is None:
            from tools.transport import Transport
            self.transport = Transport()
        url = self.hub_url + path
        if not url.startswith(("http://", "https://")):
            raise ValueError("hub_url must use http or https")
        merged = dict(self._headers())
        merged.update({str(key): str(value) for key, value in headers.items()})
        post_bytes = getattr(self.transport, "post_bytes", None)
        if not callable(post_bytes):
            raise RuntimeError("binary_transport_unavailable")
        return post_bytes(url, body, merged)

    @staticmethod
    def _browser_screenshot(command: Mapping[str, Any]) -> bool:
        return command.get("action") == "tool.browser.screenshot"

    def _materialize_browser_session_result(
        self, command: Mapping[str, Any], result: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        action = command.get("action")
        if action not in {"tool.browser.open", "tool.browser.close"}:
            return result
        outer = result.get("result") if isinstance(result.get("result"), Mapping) else {}
        session_id = outer.get("session_id") if isinstance(outer, Mapping) else None
        state = outer.get("state") if isinstance(outer, Mapping) else None
        if not isinstance(session_id, str) or state not in {"open", "closed"}:
            return result
        path = "/api/platform/v1/nodes/browser-sessions"
        if action == "tool.browser.close":
            path += "/" + session_id + "/close"
        try:
            status, payload = self._post(path, {
                "command_id": command.get("command_id"),
                "worker_id": self.worker_id,
                **({"session_id": session_id, "backend": outer.get("backend", "cdp_local")}
                   if action == "tool.browser.open" else {}),
            })
            if status != 200 or not isinstance(payload, Mapping) or not payload.get("ok"):
                raise RuntimeError("browser_session_sync_failed")
        except Exception as sync_error:
            cleanup_error = None
            if action == "tool.browser.open":
                backend = getattr(self.executor, "browser_backend", None)
                close_session = getattr(backend, "close_session", None)
                if callable(close_session):
                    try:
                        close_session(session_id)
                    except Exception as exc:
                        cleanup_error = exc
            failure = BrowserSessionSyncError(
                sync_error=sync_error, cleanup_error=cleanup_error,
            )
            raise failure from sync_error
        return result

    def _materialize_browser_result(self, command: Mapping[str, Any], result: Mapping[str, Any]):
        """Upload screenshot bytes before journal/receipt serialization."""
        if not self._browser_screenshot(command):
            return dict(result)
        outer = result.get("result") if isinstance(result.get("result"), Mapping) else {}
        raw = outer.get("result") if isinstance(outer, Mapping) else None
        if not isinstance(raw, bytes):
            return dict(result)
        if not raw.startswith(b"\x89PNG\r\n\x1a\n") or len(raw) > 256 * 1024:
            raise RuntimeError("browser_artifact_invalid")
        command_id = command.get("command_id")
        ticket_status, ticket_payload = self._post(
            "/api/platform/v1/nodes/browser-artifact-tickets",
            {"command_id": command_id, "worker_id": self.worker_id,
             "idempotency_key": str(command_id) + ":screenshot"},
        )
        if ticket_status != 200 or not isinstance(ticket_payload, Mapping):
            raise RuntimeError("browser_artifact_ticket_failed")
        ticket = ticket_payload.get("ticket")
        if not isinstance(ticket, Mapping):
            raise RuntimeError("browser_artifact_ticket_failed")
        ticket_id = ticket.get("ticket_id")
        upload_token = ticket.get("upload_token")
        artifact = ticket.get("artifact")
        if not isinstance(ticket_id, str):
            raise RuntimeError("browser_artifact_ticket_unavailable")
        if ticket.get("state") == "consumed":
            if not isinstance(artifact, Mapping):
                raise RuntimeError("browser_artifact_ticket_unavailable")
        elif not isinstance(upload_token, str):
            raise RuntimeError("browser_artifact_ticket_unavailable")
        if isinstance(artifact, Mapping):
            safe_outer = dict(result)
            safe_inner = dict(outer)
            safe_inner.pop("result", None)
            safe_inner["artifact"] = dict(artifact)
            safe_outer["result"] = safe_inner
            return safe_outer
        upload_key = str(command_id) + ":screenshot"
        status, payload = self._post_bytes(
            "/api/platform/v1/nodes/browser-artifact-tickets/" + ticket_id + "/content",
            raw,
            {"Content-Type": "image/png",
             "X-Platform-Artifact-Upload-Token": upload_token,
             "X-Platform-Command-ID": str(command_id),
             "X-Platform-Worker-ID": str(self.worker_id),
             "X-Platform-Artifact-Idempotency-Key": upload_key},
        )
        if status != 200 or not isinstance(payload, Mapping) or not payload.get("ok"):
            raise RuntimeError("browser_artifact_upload_failed")
        artifact = payload.get("artifact")
        if not isinstance(artifact, Mapping):
            raise RuntimeError("browser_artifact_upload_failed")
        safe_outer = dict(result)
        safe_inner = dict(outer)
        safe_inner.pop("result", None)
        safe_inner["artifact"] = dict(artifact)
        safe_outer["result"] = safe_inner
        return safe_outer

    def _signature_ok(self, command: Mapping):
        if self.public_key is None:
            action = command.get("action")
            protected = {"tool.browser.submit", RECEIVE_ACTION, EXPORT_ACTION, REMOTE_TEST_ACTION} | {"tool." + tool for tool in WEB_AGENT_TOOLS}
            arguments = command.get("arguments")
            capability_bound = isinstance(arguments, Mapping) and CAPABILITY_ARGUMENT in arguments
            signature_ok = not self.require_signature and action not in protected and not capability_bound
        else:
            signature_ok = verify_command(command, self.public_key)
        if not signature_ok:
            return False
        if self.node_id is not None and command.get("target_node") != self.node_id:
            return False
        try:
            expires_at = float(command.get("expires_at"))
            if expires_at <= self.clock():
                return False
            if args_hash(command.get("arguments") or {}) != command.get("args_hash"):
                return False
        except (TypeError, ValueError):
            return False
        return True

    def _renew_control(self, command_id, lease_s, stopped):
        status, payload = self.transport.request_bounded_json('POST',
            self.hub_url + '/api/platform/v1/nodes/commands/control',
            json.dumps({'command_id': command_id, 'worker_id': self.worker_id, 'lease_s': lease_s},
                       allow_nan=False, separators=(',', ':')).encode('utf-8'), self._headers(),
            deadline=time.monotonic() + 2, should_cancel=stopped, max_response_bytes=4096)
        if status in {400, 401, 403, 404, 409}:
            raise ControlRejected('command_control_rejected')
        if status != 200:
            raise OSError('command_control_unavailable')
        if not isinstance(payload, dict) or payload.get('ok') is not True:
            raise ControlRejected('invalid_command_control')
        return payload.get('control')

    def _execute(self, command, *, control_supported=False, lease_s=60.0):
        controlled = getattr(self.executor, 'execute_controlled', None)
        if (not callable(controlled)
                or command.get('action') not in getattr(self.executor, 'controlled_actions', ())):
            return self.executor(command)
        renew = (lambda stopped: self._renew_control(command['command_id'], lease_s, stopped)) if control_supported else None
        with CommandControl(command, renew=renew, should_stop=self.should_cancel,
                            clock=self.clock, lease_s=lease_s) as control:
            return controlled(command, should_cancel=control.should_cancel)

    def handle(self, command: dict, *, control_supported=False, lease_s=60.0):
        command_id = command.get("command_id")
        if not isinstance(command_id, str) or not command_id:
            return {"status": "rejected", "reason": "invalid_command"}
        if not self._signature_ok(command):
            return {"command_id": command_id, "status": "rejected",
                    "reason": "invalid_signature"}
        if command.get("action") in {RECEIVE_ACTION, EXPORT_ACTION}:
            # File transfers have their own durable intent and outbox. The
            # generic running/unknown journal must not prevent recovering a
            # publication that completed immediately before a process exit.
            outcome = self.executor(command)
            return {"command_id": command_id, "status": outcome["state"],
                "result": outcome["result"], **({"error_code": outcome["error_code"]}
                if outcome.get("error_code") else {}), **({"deferred": True} if outcome.get("deferred") else {})}
        attribution = {}
        arguments = command.get("arguments")
        if command.get("action") == "tool.browser.submit" and isinstance(arguments, Mapping):
            approval_id = arguments.get("approval_id")
            if isinstance(approval_id, str):
                attribution["approval_id"] = approval_id
        prior = self.journal.begin(command_id, now=self.clock(), metadata=attribution,
                                   worker_id=self.worker_id)
        if not prior["claimed"]:
            return {"command_id": command_id, "status": prior["state"], "result": prior["result"]}
        try:
            result = self._execute(command, control_supported=control_supported, lease_s=lease_s)
            if attribution and isinstance(result, Mapping):
                result = {**result, **attribution}
            if isinstance(result, Mapping):
                result = self._materialize_browser_session_result(command, result)
                result = self._materialize_browser_result(command, result)
            execution_state = result.get("state") if isinstance(result, Mapping) else None
            status = execution_state if execution_state in {"succeeded", "failed", "unknown"} else "succeeded"
            saved = self.journal.finish(command_id, status, result, now=self.clock())
            return {"command_id": command_id, "status": saved["state"], "result": saved["result"] or {}}
        except Exception:
            failure = {"reason": "executor_interrupted", **attribution}
            saved = self.journal.finish(command_id, "unknown", failure, now=self.clock())
            return {"command_id": command_id, "status": saved["state"], "result": saved["result"] or {}}

    def _upload_receipt(self, command_id, result, *, worker_id=None, artifact_handler=None, bounded=False):
        wire_status = result.get("status", "unknown")
        # Local validation rejections use the Hub's terminal failure state.
        if wire_status == "rejected":
            wire_status = "failed"
        receipt = {
            "command_id": command_id,
            "status": wire_status,
            "worker_id": worker_id or self.worker_id,
        }
        if isinstance(result.get("result"), dict):
            receipt["result"] = result["result"]
            if isinstance(result.get("error_code"), str):
                receipt["result"] = dict(receipt["result"])
                receipt["result"]["error_code"] = result["error_code"][:80]
        elif result.get("reason"):
            receipt["result"] = {"reason": str(result["reason"])[:80]}
            if isinstance(result.get("approval_id"), str):
                receipt["result"]["approval_id"] = result["approval_id"]
        try:
            options = {"bounded": True} if bounded else {}
            status, payload = self._post("/api/platform/v1/nodes/receipts", receipt, **options)
            if status != 200 or not isinstance(payload, Mapping) or not payload.get("ok"):
                return False
            if artifact_handler is not None and wire_status == "succeeded":
                confirmed = payload.get("receipt")
                if (not isinstance(confirmed, Mapping) or confirmed.get("command_id") != command_id
                        or confirmed.get("status") != "succeeded" or confirmed.get("result") != receipt.get("result")):
                    return False
                artifact_handler.acknowledge(command_id, receipt["result"])
            elif artifact_handler is None:
                self.journal.acknowledge_receipt(command_id)
            return True
        except Exception:
            # The terminal journal row stays pending, including when the Hub
            # accepted it but the acknowledgement was lost in transit.
            return False

    def poll_once(self, *, limit=20, lease_s=60.0):
        """Retry receipts, then claim only work that can start immediately.

        ``limit`` is the maximum batch throughput, not a prefetch count: this
        executor is sequential and every command needs its own fresh lease.
        """
        limit = max(1, min(int(limit), 100))
        receipts = 0
        if self.should_cancel is not None and self.should_cancel():
            return {'ok': True, 'commands': 0, 'receipts': 0}
        receiver = getattr(self.executor, "artifact_receiver", None)
        exporter = getattr(self.executor, "artifact_exporter", None)
        recovery_errors = ()
        export_errors = ()
        for handler, bounded in ((receiver, False), (exporter, True)):
            if handler is None:
                continue
            errors = handler.recover(limit=limit)
            if bounded:
                export_errors = errors
            else:
                recovery_errors = errors
            for command_id, result in handler.pending_receipts(limit=limit):
                receipts += self._upload_receipt(command_id, {"status": "succeeded", "result": result},
                    artifact_handler=handler, bounded=bounded)
        for saved in self.journal.pending_receipts(limit=limit):
            receipts += self._upload_receipt(
                saved["command_id"],
                {"status": saved["state"], "result": saved["result"] or {}},
                worker_id=saved["worker_id"],
            )
        handled = 0
        seen = set()
        for _ in range(limit):
            if self.should_cancel is not None and self.should_cancel():
                break
            try:
                status, payload = self._post(
                    "/api/platform/v1/nodes/poll",
                    {"worker_id": self.worker_id, "limit": 1, "lease_s": lease_s},
                )
            except Exception:
                return {"ok": False, "commands": handled, "error": "poll_transport_error"}
            if status != 200 or not isinstance(payload, dict) or not payload.get("ok"):
                return {"ok": False, "commands": handled, "error": "poll_failed"}
            commands = payload.get("commands")
            if not isinstance(commands, list) or len(commands) > 1:
                return {"ok": False, "commands": handled, "error": "malformed_commands"}
            if not commands:
                break
            command = commands[0]
            if not isinstance(command, dict):
                return {"ok": False, "commands": handled, "error": "malformed_commands"}
            command_id = command.get("command_id")
            if not isinstance(command_id, str) or not command_id:
                return {"ok": False, "commands": handled, "error": "malformed_commands"}
            if command_id in seen:
                break
            seen.add(command_id)
            control_supported = (payload.get('command_control_version') == 1
                and callable(getattr(self.transport, 'request_bounded_json', None)))
            result = self.handle(command, control_supported=control_supported, lease_s=lease_s)
            handled += 1
            if result.get("deferred"):
                continue
            saved = self.journal.get(command_id) if isinstance(command_id, str) else None
            action = command.get("action")
            handler = receiver if action == RECEIVE_ACTION else exporter if action == EXPORT_ACTION else None
            receipts += self._upload_receipt(
                command_id, result,
                worker_id=saved["worker_id"] if saved else self.worker_id,
                artifact_handler=handler, bounded=action == EXPORT_ACTION,
            )
        return {"ok": True, "commands": handled, "receipts": receipts,
            **({"input_recovery_errors": list(recovery_errors)} if recovery_errors else {}),
            **({"output_recovery_errors": list(export_errors)} if export_errors else {})}

    def heartbeat(self, *, status="online"):
        try:
            code, payload = self._post(
                "/api/platform/v1/nodes/heartbeat", {"status": status})
        except Exception:
            return {"ok": False, "error": "heartbeat_transport_error"}
        if code != 200 or not isinstance(payload, dict):
            return {"ok": False, "error": "heartbeat_failed"}
        return payload


__all__ = ["NodeClient"]
