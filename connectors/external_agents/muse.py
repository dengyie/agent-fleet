"""Muse external Agent adapter over the existing controlled bridge."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fleet_core.workflow.external_outputs import ExternalOutputSet
from tools.browser_bridge.muse import MuseBridge, MuseBridgeError

from .base import (
    CancelReceipt,
    ExternalAgentCapabilities,
    ExternalAgentError,
    ExternalAgentStatus,
    ExternalTaskRef,
    _id,
)


def open_muse_bridge(journal: Path) -> MuseBridge:
    """Construct the original controlled bridge without journal or browser I/O."""
    return MuseBridge(journal)


class MuseExternalAgentAdapter:
    agent_id = "muse"
    capabilities = ExternalAgentCapabilities(
        submit_idempotent=True,
        lookup_by_token=True,
        status_evidence=True,
        cancel_supported=False,
        cancel_confirmable=False,
        output_export_supported=False,
    )

    def __init__(self, bridge: MuseBridge, *, workspace_id: str,
                 account_ref: str | None = None, transport_ref: str = "muse-cdp-v1") -> None:
        if bridge is None:
            raise ExternalAgentError("bridge_unavailable")
        self.bridge = bridge
        self.workspace_id = _id(workspace_id, "invalid_workspace")
        self.account_ref = _id(account_ref) if account_ref is not None else None
        self.transport_ref = _id(transport_ref)

    @property
    def configuration_digest(self) -> str | None:
        if not isinstance(self.bridge, MuseBridge) or self.account_ref is None:
            return None
        journal = self.bridge.journal_identity()
        if journal is None:
            return None
        payload = {"adapter": "muse-controlled-v1", "account_ref": self.account_ref,
                   "journal_identity": journal,
                   "workspace_id": self.workspace_id, "journal": str(self.bridge.path.absolute()),
                   "transport_ref": self.transport_ref,
                   "transport_type": type(self.bridge.transport).__module__ + "." + type(self.bridge.transport).__qualname__}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()

    @staticmethod
    def _prompt(contract: Mapping[str, Any]) -> str:
        if not isinstance(contract, Mapping):
            raise ExternalAgentError("invalid_external_contract")
        prompt = contract.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 8192:
            raise ExternalAgentError("invalid_external_contract")
        return prompt

    def lookup(self, dispatch_token: str, resolved_contract: Mapping[str, Any]) -> ExternalTaskRef | None:
        token = _id(dispatch_token)
        prompt = self._prompt(resolved_contract)
        try:
            result = self.bridge.lookup(token, prompt, scope=self.workspace_id)
        except MuseBridgeError as exc:
            raise ExternalAgentError(exc.code, uncertain=True, cause=exc) from exc
        if result is None:
            return None
        if not isinstance(result, Mapping) or result.get("task_id") != token:
            raise ExternalAgentError("external_task_mismatch", uncertain=True)
        return ExternalTaskRef(self.agent_id, token, external_id=token)

    def submit(self, dispatch_token: str, resolved_contract: Mapping[str, Any]) -> ExternalTaskRef:
        token = _id(dispatch_token)
        prompt = self._prompt(resolved_contract)
        try:
            result = self.bridge.submit(token, prompt, scope=self.workspace_id)
        except MuseBridgeError as exc:
            if exc.code in {"dispatch_unknown", "bridge_unavailable", "observation_unavailable"}:
                raise ExternalAgentError("dispatch_unknown", uncertain=True, cause=exc) from exc
            raise ExternalAgentError(exc.code, cause=exc) from exc
        if not isinstance(result, Mapping):
            raise ExternalAgentError("invalid_external_receipt")
        state = result.get("state")
        if state == "unknown":
            raise ExternalAgentError("dispatch_unknown", uncertain=True)
        external_id = result.get("task_id")
        if not isinstance(external_id, str):
            raise ExternalAgentError("dispatch_identity_missing", uncertain=True)
        if external_id != token:
            raise ExternalAgentError("external_task_mismatch", uncertain=True)
        return ExternalTaskRef(self.agent_id, token, external_id=external_id)

    def inspect(self, task: ExternalTaskRef) -> ExternalAgentStatus:
        self._check_task(task)
        try:
            result = self.bridge.read(task.external_id or task.dispatch_token, scope=self.workspace_id)
        except MuseBridgeError as exc:
            if exc.code in {"observation_unavailable", "task_not_found", "bridge_unavailable"}:
                return ExternalAgentStatus("unknown", "unknown", task.external_id, error_code=exc.code)
            raise ExternalAgentError(exc.code, cause=exc) from exc
        if not isinstance(result, Mapping):
            return ExternalAgentStatus("unknown", "unknown", task.external_id, error_code="invalid_external_receipt")
        if result.get("task_id") != (task.external_id or task.dispatch_token):
            return ExternalAgentStatus("unknown", "unknown", task.external_id, error_code="external_task_mismatch")
        mapped = {"sent": "waiting_external", "running": "running", "waiting_input": "waiting_input",
                  "completed": "succeeded", "failed": "failed", "dispatching": "dispatching"}.get(
                      result.get("state"), "unknown")
        evidence = "agent_reported" if mapped in {"succeeded", "failed"} else (
            "accepted" if mapped in {"waiting_external", "running", "waiting_input"} else "unknown")
        return ExternalAgentStatus(mapped, evidence, task.external_id,
                                   str(result.get("reply") or "")[:8192], result.get("error_code"))

    def request_cancel(self, task: ExternalTaskRef) -> CancelReceipt:
        self._check_task(task)
        return CancelReceipt("unsupported", requested=False, confirmed=False,
                             external_id=task.external_id, error_code="cancel_unsupported")

    def export_outputs(self, task: ExternalTaskRef,
                       output_specs: tuple[Mapping[str, Any], ...]) -> ExternalOutputSet:
        self._check_task(task)
        if not isinstance(output_specs, tuple) or len(output_specs) > 32:
            raise ExternalAgentError("invalid_output_specs")
        raise ExternalAgentError("output_export_unsupported")

    def _check_task(self, task: ExternalTaskRef) -> None:
        if (not isinstance(task, ExternalTaskRef) or task.agent_id != self.agent_id
                or task.external_id != task.dispatch_token or task.session_id is not None):
            raise ExternalAgentError("external_task_mismatch")


__all__ = ["MuseExternalAgentAdapter"]
