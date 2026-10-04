"""Node-side authenticated poll/execute loop with journal idempotency."""
from __future__ import annotations

from collections.abc import Mapping
import time
from typing import Any

from hub.domain.platform_command import args_hash, verify_command


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
                 require_signature=False, worker_id=None):
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

    def _headers(self):
        if not self.node_id or not self.credential:
            raise ValueError("node transport credentials required")
        return {
            "Content-Type": "application/json",
            "X-Platform-Node-Credential": self.credential,
        }

    def _post(self, path, body):
        if self.transport is None:
            from tools.transport import Transport
            self.transport = Transport()
        url = self.hub_url + path
        if not url.startswith(("http://", "https://")):
            raise ValueError("hub_url must use http or https")
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
            signature_ok = not self.require_signature
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

    def handle(self, command: dict):
        command_id = command.get("command_id")
        if not isinstance(command_id, str) or not command_id:
            return {"status": "rejected", "reason": "invalid_command"}
        if not self._signature_ok(command):
            return {"command_id": command_id, "status": "rejected",
                    "reason": "invalid_signature"}
        prior = self.journal.get(command_id)
        if prior and prior["state"] in {"succeeded", "failed", "unknown"}:
            return {"command_id": command_id, "status": prior["state"], "result": prior["result"]}
        self.journal.begin(command_id, now=self.clock())
        try:
            result = self.executor(command)
            if isinstance(result, Mapping):
                result = self._materialize_browser_session_result(command, result)
                result = self._materialize_browser_result(command, result)
            execution_state = result.get("state") if isinstance(result, Mapping) else None
            status = execution_state if execution_state in {"succeeded", "failed", "unknown"} else "succeeded"
            self.journal.finish(command_id, status, result, now=self.clock())
            return {"command_id": command_id, "status": status, "result": result or {}}
        except Exception:
            self.journal.finish(command_id, "unknown", {"reason": "executor_interrupted"}, now=self.clock())
            return {"command_id": command_id, "status": "unknown", "reason": "executor_interrupted"}

    def poll_once(self, *, limit=20, lease_s=60.0):
        """Poll once, execute each command, and upload bounded receipts."""
        try:
            status, payload = self._post(
                "/api/platform/v1/nodes/poll",
                {"worker_id": self.worker_id, "limit": limit, "lease_s": lease_s},
            )
        except Exception:
            return {"ok": False, "commands": 0, "error": "poll_transport_error"}
        if status != 200 or not isinstance(payload, dict) or not payload.get("ok"):
            return {"ok": False, "commands": 0, "error": "poll_failed"}
        commands = payload.get("commands")
        if not isinstance(commands, list):
            return {"ok": False, "commands": 0, "error": "malformed_commands"}
        handled = 0
        receipts = 0
        for command in commands:
            if not isinstance(command, dict):
                continue
            result = self.handle(command)
            handled += 1
            wire_status = result.get("status", "unknown")
            # The Hub command receipt contract records execution outcomes; a
            # local validation rejection is represented as bounded failure so
            # the rejection is durable rather than becoming an invalid HTTP
            # receipt.
            if wire_status == "rejected":
                wire_status = "failed"
            receipt = {
                "command_id": command.get("command_id"),
                "status": wire_status,
                "worker_id": self.worker_id,
            }
            if isinstance(result.get("result"), dict):
                receipt["result"] = result["result"]
                if isinstance(result.get("error_code"), str):
                    receipt["result"] = dict(receipt["result"])
                    receipt["result"]["error_code"] = result["error_code"][:80]
            elif result.get("reason"):
                receipt["result"] = {"reason": str(result["reason"])[:80]}
            try:
                receipt_status, _ = self._post(
                    "/api/platform/v1/nodes/receipts", receipt)
                if receipt_status == 200:
                    receipts += 1
            except Exception:
                pass
        return {"ok": True, "commands": handled, "receipts": receipts}

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
