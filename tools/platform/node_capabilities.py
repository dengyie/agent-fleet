"""Explicit local capability facts, preflight and bounded machine reporting."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import platform
import shutil
import time
from typing import Any, Protocol
from urllib.parse import urlsplit

from fleet_core.capability import (
    MAX_OFFER_CAPABILITIES, CapabilityDefinition, CapabilityError, NodeOffer,
    identifier, integer, timestamp, freeze,
)
from fleet_core.capability_execution import CAPABILITY_ARGUMENT, CapabilityExecutionContext
from fleet_core.workflow.contract_types import CatalogRef
from .node_executor import NODE_TOOLS, POSTCHECK_ACTIONS
from fleet_core.remote_workflow_test import REMOTE_TEST_ACTION

CAPABILITY_ACTIONS = frozenset({"tool." + name for name in NODE_TOOLS} | POSTCHECK_ACTIONS | {REMOTE_TEST_ACTION})


@dataclass(frozen=True, slots=True)
class LocalCapability:
    definition: CapabilityDefinition
    actions: tuple[str, ...]
    action_set: frozenset[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (type(self.definition) is not CapabilityDefinition or type(self.actions) is not tuple
                or not 1 <= len(self.actions) <= 128):
            raise CapabilityError("invalid_local_capability")
        for action in self.actions:
            identifier(action)
            if action not in CAPABILITY_ACTIONS:
                raise CapabilityError("invalid_capability_action")
        if len(set(self.actions)) != len(self.actions):
            raise CapabilityError("invalid_capability_action")
        object.__setattr__(self, "action_set", frozenset(self.actions))


@dataclass(frozen=True, slots=True)
class NodeCapabilityConfig:
    offer_revision: int
    registrations: tuple[LocalCapability, ...]
    local_allowed_capabilities: tuple[CatalogRef, ...]
    slots: int = 1

    def __post_init__(self) -> None:
        integer(self.offer_revision, minimum=1)
        integer(self.slots, maximum=1024)
        if type(self.registrations) is not tuple or not 1 <= len(self.registrations) <= MAX_OFFER_CAPABILITIES:
            raise CapabilityError("invalid_local_capability")
        if any(type(item) is not LocalCapability for item in self.registrations):
            raise CapabilityError("invalid_local_capability")
        references = tuple(CatalogRef(item.definition.capability_id, item.definition.revision) for item in self.registrations)
        # Reuse the exact report bounds, including duplicate and integer checks.
        NodeOffer(self.offer_revision, references, self.local_allowed_capabilities, "linux", "configuration", (),
            self.slots, 0, 0, "1970-01-01T00:00:00Z", "healthy", 0, "node-local")
        if not set(self.local_allowed_capabilities).issubset(references):
            raise CapabilityError("local_capability_unregistered")


@dataclass(frozen=True, slots=True)
class NodeCapabilityFacts:
    os: str
    architecture: str
    software: tuple[tuple[str, str], ...]
    memory_bytes: int
    disk_bytes: int
    health: str = "healthy"

    def __post_init__(self) -> None:
        NodeOffer(1, (), (), self.os, self.architecture, self.software, 0, self.memory_bytes,
            self.disk_bytes, "1970-01-01T00:00:00Z", self.health, 0, "node-local")


class CapabilityObserver(Protocol):
    def __call__(self) -> NodeCapabilityFacts: ...


def observe_local_capabilities(workspace_root: Path) -> NodeCapabilityFacts:
    """No programs or packages are discovered or executed by reporting.

    There is no portable, container-aware free-memory measurement in the
    standard library. Zero reserves no unproven RAM; an administrator can
    inject a scoped observer with evidence for additional software/resources.
    """
    try:
        return NodeCapabilityFacts(platform.system().lower(), platform.machine(),
            (("python", platform.python_version()),), 0, shutil.disk_usage(workspace_root).free)
    except (OSError, CapabilityError) as exc:
        raise CapabilityError("capability_observation_failed") from exc


class NodeCapabilityService:
    def __init__(self, config: NodeCapabilityConfig, *, node_id: str, observer: CapabilityObserver,
                 available_actions: Callable[[], frozenset[str]]) -> None:
        if type(config) is not NodeCapabilityConfig or not callable(observer) or not callable(available_actions):
            raise CapabilityError("invalid_capability_configuration")
        self.config, self.node_id = config, identifier(node_id)
        self.observer, self.available_actions = observer, available_actions
        self.registrations = {CatalogRef(item.definition.capability_id, item.definition.revision): item for item in config.registrations}

    def offer(self, *, now: float) -> NodeOffer:
        now = timestamp(now)
        try:
            facts, actions = self.observer(), self.available_actions()
            if type(facts) is not NodeCapabilityFacts or type(actions) is not frozenset:
                raise CapabilityError("invalid_capability_observation")
            implemented = tuple(ref for ref, item in self.registrations.items() if item.action_set.issubset(actions))
            implemented_set = set(implemented)
            allowed = tuple(ref for ref in self.config.local_allowed_capabilities if ref in implemented_set)
            return NodeOffer(self.config.offer_revision, implemented, allowed, facts.os, facts.architecture, facts.software,
                self.config.slots, facts.memory_bytes, facts.disk_bytes,
                datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z"), facts.health, now, "node-local")
        except CapabilityError:
            raise
        except Exception as exc:
            raise CapabilityError("capability_observation_failed") from exc

    def prepare(self, command: Mapping[str, Any], *, now: float) -> dict[str, Any]:
        context = CapabilityExecutionContext.from_command(command)
        binding = context.binding
        identifier(command.get("run_id"))
        identifier(command.get("action"))
        if command.get("target_node") != self.node_id or binding.node_id != self.node_id:
            raise CapabilityError("invalid_capability_command")
        observed = self.offer(now=now)
        if ((binding.offer_revision, binding.offer_digest) != (observed.offer_revision, observed.config_digest)
                or observed.health != "healthy" or not observed.slots
                or binding.memory_bytes > observed.memory_bytes or binding.disk_bytes > observed.disk_bytes):
            raise CapabilityError("capability_changed")
        permitted = set(observed.capabilities) & set(observed.local_allowed_capabilities)
        definitions = self._bound_definitions(context, permitted)
        selected = definitions[context.capability_id]
        if command.get("action") not in selected.action_set or command.get("retry_class") != selected.definition.retry_class:
            raise CapabilityError("capability_changed")
        arguments = dict(command["arguments"])
        arguments.pop(CAPABILITY_ARGUMENT)
        return {**command, "arguments": arguments}

    def _bound_definitions(self, context: CapabilityExecutionContext, permitted: set[CatalogRef]) -> dict[str, LocalCapability]:
        definitions: dict[str, LocalCapability] = {}
        for bound in context.binding.capabilities:
            reference = CatalogRef(bound.id, bound.revision)
            local = self.registrations.get(reference)
            if (reference not in permitted or local is None or local.definition.digest != bound.definition_digest
                    or local.definition.adapter_id != context.adapter_id):
                raise CapabilityError("capability_changed")
            definitions[bound.id] = local
        return definitions


class CapabilityReportTransport(Protocol):
    def request_bounded_json(self, method: str, url: str, body: bytes | None, headers: dict[str, str], *,
                             deadline: float, should_cancel: Callable[[], bool],
                             max_response_bytes: int = 65536) -> tuple[int, dict[str, Any]]: ...


class NodeCapabilityReportError(RuntimeError):
    def __init__(self, code: str, *, status: int | None = None) -> None:
        self.code, self.status = code, status
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class NodeCapabilityReceipt:
    node_id: str
    offer_revision: int
    received_at: str

    def __post_init__(self) -> None:
        identifier(self.node_id)
        integer(self.offer_revision, minimum=1)
        # The receipt uses the same strict UTC wire syntax as NodeOffer.
        NodeOffer(1, (), (), "linux", "receipt", (), 0, 0, 0, self.received_at, "healthy", 0, "node-local")


def _report_origin(value: str) -> str:
    if type(value) is not str or len(value) > 4096 or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise CapabilityError("invalid_capability_hub_origin")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise CapabilityError("invalid_capability_hub_origin") from exc
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or port == 0):
        raise CapabilityError("invalid_capability_hub_origin")
    return value.rstrip("/")


class NodeCapabilityReporter:
    def __init__(self, service: NodeCapabilityService, *, hub_url: str, credential: str,
                 transport: CapabilityReportTransport, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic, should_cancel: Callable[[], bool]) -> None:
        if (type(service) is not NodeCapabilityService or not all(callable(port) for port in (clock, monotonic, should_cancel))
                or not callable(getattr(transport, "request_bounded_json", None))):
            raise CapabilityError("invalid_capability_configuration")
        origin = _report_origin(hub_url)
        if (type(credential) is not str or len(credential) > 4096 or not credential.startswith(service.node_id + ":")
                or not credential.partition(":")[2] or ":" in credential.partition(":")[2]
                or any(ord(char) < 32 or ord(char) == 127 for char in credential)):
            raise CapabilityError("invalid_capability_credential")
        self.service, self.transport = service, transport
        self.url, self.credential = origin + "/api/platform/v1/nodes/capability-offers", credential
        self.clock, self.monotonic, self.should_cancel = clock, monotonic, should_cancel

    def report(self) -> NodeCapabilityReceipt:
        if self.should_cancel():
            raise NodeCapabilityReportError("node_stopped")
        deadline = self.monotonic() + 15
        try:
            offer = self.service.offer(now=self.clock())
        except CapabilityError as exc:
            raise NodeCapabilityReportError("capability_report_observation") from exc
        self._check_window(deadline)
        try:
            status, payload = self.transport.request_bounded_json("POST", self.url, freeze(offer.payload()),
                {"Content-Type": "application/json", "X-Platform-Node-Credential": self.credential},
                deadline=deadline, should_cancel=self.should_cancel, max_response_bytes=65536)
        except Exception as exc:
            raise NodeCapabilityReportError("capability_report_transport") from exc
        self._check_window(deadline)
        if type(status) is not int or status != 200:
            raise NodeCapabilityReportError("capability_report_rejected", status=status)
        try:
            return self._receipt(payload, offer.offer_revision)
        except CapabilityError as exc:
            raise NodeCapabilityReportError("invalid_capability_receipt") from exc

    def _check_window(self, deadline: float) -> None:
        if self.should_cancel():
            raise NodeCapabilityReportError("node_stopped")
        if self.monotonic() >= deadline:
            raise NodeCapabilityReportError("capability_report_deadline")

    def _receipt(self, payload: object, revision: int) -> NodeCapabilityReceipt:
        if type(payload) is not dict or set(payload) != {"ok", "offer"} or payload["ok"] is not True:
            raise CapabilityError("invalid_capability_receipt")
        value = payload["offer"]
        if type(value) is not dict or set(value) != {"node_id", "offer_revision", "received_at"}:
            raise CapabilityError("invalid_capability_receipt")
        receipt = NodeCapabilityReceipt(**value)
        if receipt.node_id != self.service.node_id or receipt.offer_revision != revision:
            raise CapabilityError("invalid_capability_receipt")
        return receipt
