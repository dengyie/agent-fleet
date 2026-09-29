"""Node-side authenticated poll/execute loop with journal idempotency."""
from __future__ import annotations

from collections.abc import Mapping
import time

from hub.domain.platform_command import args_hash, verify_command


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
