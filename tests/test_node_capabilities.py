"""Node observations and preflight on real signed commands and local files."""
from __future__ import annotations

import copy
import json
import time
from dataclasses import replace
from pathlib import Path

import pytest

from hub.domain.capability import CapabilityError, TargetBinding, BoundCapability
from hub.domain.capability_execution import CAPABILITY_ARGUMENT, CapabilityExecutionContext
from fleet_core.control import generate_ed25519_keypair
from fleet_core.platform_command import PlatformCommand, verify_command
from hub.domain.workflow_contract_types import CatalogRef
from tools.platform.node_capabilities import (
    LocalCapability, NodeCapabilityConfig, NodeCapabilityFacts, NodeCapabilityService,
    NodeCapabilityReportError, NodeCapabilityReporter, observe_local_capabilities,
)
from tools.platform.node_runtime import NodeRuntime, NodeRuntimeConfig
from test_capability_router import definition
from test_transport_bounded import peer

PRIVATE_KEY, PUBLIC_KEY = generate_ed25519_keypair()
DEFINITION = definition(capability_id="workspace.files", adapter_id="workspace-adapter",
    risk="workspace_write", retry_class="idempotent_with_key")
CAP = CatalogRef(DEFINITION.capability_id, 1)
READ_DEFINITION = definition(capability_id="workspace.digest", adapter_id="workspace-adapter",
    risk="workspace_read", retry_class="read_only")
READ_CAP = CatalogRef(READ_DEFINITION.capability_id, 1)
ACTIONS = frozenset({"tool.workspace.write", "tool.workspace.read"})


def configuration(**changes):
    values = dict(offer_revision=1, registrations=(LocalCapability(DEFINITION, tuple(sorted(ACTIONS))),),
        local_allowed_capabilities=(CAP,), slots=2)
    return NodeCapabilityConfig(**(values | changes))


def facts(**changes):
    values = dict(os="darwin", architecture="arm64", software=(("python", "3.13.7"),),
        memory_bytes=1000, disk_bytes=2000, health="healthy")
    return NodeCapabilityFacts(**(values | changes))


def service(*, config=None, observer=None, actions=None):
    return NodeCapabilityService(config or configuration(), node_id="node-a", observer=observer or facts,
        available_actions=actions or (lambda: ACTIONS))


def binding(subject=None):
    observed = (subject or service()).offer(now=100)
    return TargetBinding("node-a", observed.offer_revision, observed.config_digest, "b" * 64,
        (BoundCapability(CAP.id, 1, DEFINITION.digest),), "c" * 64, "d" * 64, 100, 200)


def signed_command(*, command_id="capability-write", selected=None, action="tool.workspace.write", **changes):
    values = dict(command_id=command_id, target_node="node-a", resource_id="workspace", action=action,
        arguments={"path": "result.txt", "content": "first"}, run_id="run-a", expires_at=150,
        retry_class="idempotent_with_key")
    command = PlatformCommand.create(**(values | changes))
    context = CapabilityExecutionContext(CAP.id, DEFINITION.adapter_id, selected or binding())
    return context.bind(command).signed(PRIVATE_KEY).as_dict()


def test_context_is_covered_by_original_signature_and_stripped_only_after_validation():
    command = signed_command()
    assert verify_command(command, PUBLIC_KEY)
    changed = copy.deepcopy(command)
    changed["arguments"][CAPABILITY_ARGUMENT]["binding"]["offer_revision"] = 2
    assert not verify_command(changed, PUBLIC_KEY)
    prepared = service().prepare(command, now=100)
    assert prepared["arguments"] == {"path": "result.txt", "content": "first"}
    assert CAPABILITY_ARGUMENT in command["arguments"]
    assert prepared["command_id"] == command["command_id"]


@pytest.mark.parametrize("change", [
    {"schema_version": True}, {"adapter_id": "other"}, {"capability_id": "other"},
    {"trusted": True}, {"binding": []},
])
def test_hostile_context_is_rejected_before_action(change):
    command = signed_command()
    command["arguments"][CAPABILITY_ARGUMENT].update(change)
    with pytest.raises(CapabilityError):
        service().prepare(command, now=100)


def test_context_rejects_duplicate_json_and_cannot_override_existing_binding():
    context = CapabilityExecutionContext(CAP.id, DEFINITION.adapter_id, binding())
    raw = context.snapshot[:-1] + b',"schema_version":1}'
    with pytest.raises(CapabilityError):
        CapabilityExecutionContext.from_snapshot(raw)
    with pytest.raises(CapabilityError):
        context.bind(PlatformCommand.create(command_id="already-bound", target_node="node-a",
            action="tool.workspace.write", resource_id="workspace", run_id="run-a", expires_at=150,
            arguments={CAPABILITY_ARGUMENT: {}}))


def test_context_rejects_oversized_input_before_json_parsing():
    with pytest.raises(CapabilityError, match="json_too_large"):
        CapabilityExecutionContext.from_snapshot(b" " * 65537)


@pytest.mark.parametrize("field,value", [("run_id", True), ("run_id", 123), ("run_id", "bad\nrun"), ("action", [])])
def test_preflight_rejects_hostile_command_field_types_as_domain_errors(field, value):
    command = signed_command()
    command[field] = value
    with pytest.raises(CapabilityError):
        service().prepare(command, now=100)


@pytest.mark.parametrize("value", [None, [], "not-a-command"])
def test_context_parser_requires_a_command_mapping(value):
    with pytest.raises(CapabilityError):
        CapabilityExecutionContext.from_command(value)


@pytest.mark.parametrize("change", [
    {"software": (("python", "3.13.8"),)}, {"os": "linux"}, {"architecture": "x86_64"},
    {"health": "degraded"}, {"memory_bytes": 99}, {"disk_bytes": 199},
])
def test_live_software_platform_health_and_resource_changes_reject_original_binding(change):
    command = signed_command()
    current = service(observer=lambda: facts(**change))
    with pytest.raises(CapabilityError, match="capability_changed"):
        current.prepare(command, now=100)


def test_local_permission_revision_definition_and_available_action_are_rechecked():
    command = signed_command()
    changed_definition = replace(DEFINITION, result_limit_bytes=1024)
    configurations = (configuration(local_allowed_capabilities=()), configuration(offer_revision=2),
        configuration(registrations=(LocalCapability(changed_definition, tuple(sorted(ACTIONS))),)))
    for config in configurations:
        with pytest.raises(CapabilityError, match="capability_changed"):
            service(config=config).prepare(command, now=100)
    with pytest.raises(CapabilityError, match="capability_changed"):
        service(actions=lambda: frozenset()).prepare(command, now=100)


def test_resource_refresh_keeps_configuration_digest_and_does_not_invalidate_sufficient_capacity():
    subject = service(observer=lambda: facts(memory_bytes=500, disk_bytes=600))
    assert subject.offer(now=101).config_digest == service().offer(now=100).config_digest
    assert subject.prepare(signed_command(), now=101)["arguments"]["path"] == "result.txt"


def test_an_allowed_capability_cannot_run_an_unregistered_action_or_changed_retry_contract():
    with pytest.raises(CapabilityError, match="capability_changed"):
        service().prepare(signed_command(action="tool.workspace.exec"), now=100)
    with pytest.raises(CapabilityError, match="capability_changed"):
        service().prepare(signed_command(retry_class="read_only"), now=100)


def test_digest_helper_is_a_separate_local_read_only_capability():
    config = configuration(registrations=(LocalCapability(DEFINITION, tuple(sorted(ACTIONS))),
        LocalCapability(READ_DEFINITION, ("reconcile.workspace.digest",))), local_allowed_capabilities=(CAP, READ_CAP))
    subject = service(config=config, actions=lambda: ACTIONS | {"reconcile.workspace.digest"})
    selected = replace(binding(subject), capabilities=(BoundCapability(CAP.id, 1, DEFINITION.digest),
        BoundCapability(READ_CAP.id, 1, READ_DEFINITION.digest)))
    command = PlatformCommand.create(command_id="digest-check", target_node="node-a", resource_id="workspace",
        action="reconcile.workspace.digest", arguments={"path": "result.txt"}, retry_class="read_only",
        expires_at=150, run_id="run-a")
    bound = CapabilityExecutionContext(READ_CAP.id, READ_DEFINITION.adapter_id, selected).bind(command).signed(PRIVATE_KEY).as_dict()
    prepared = subject.prepare(bound, now=100)
    assert prepared["arguments"] == {"path": "result.txt"}
    assert READ_CAP.id != CAP.id


@pytest.mark.parametrize("change", ["missing_helper", "allowlist", "revision", "definition", "action", "retry", "unavailable"])
def test_digest_helper_revalidates_local_definition_action_policy_offer_and_exact_retry(change):
    registrations = (LocalCapability(DEFINITION, tuple(sorted(ACTIONS))),
        LocalCapability(READ_DEFINITION, ("reconcile.workspace.digest",)))
    config = configuration(registrations=registrations, local_allowed_capabilities=(CAP, READ_CAP))
    actions = ACTIONS | {"reconcile.workspace.digest"}
    original = service(config=config, actions=lambda: actions)
    selected = replace(binding(original), capabilities=(BoundCapability(CAP.id, 1, DEFINITION.digest),
        BoundCapability(READ_CAP.id, 1, READ_DEFINITION.digest)))
    command = PlatformCommand.create(command_id="digest-check", target_node="node-a", resource_id="workspace",
        action="reconcile.workspace.digest", arguments={"path": "result.txt"}, retry_class="read_only", expires_at=150, run_id="run-a")
    bound = CapabilityExecutionContext(READ_CAP.id, READ_DEFINITION.adapter_id, selected).bind(command).signed(PRIVATE_KEY).as_dict()
    if change == "missing_helper":
        config = replace(config, registrations=registrations[:1], local_allowed_capabilities=(CAP,))
    elif change == "allowlist":
        config = replace(config, local_allowed_capabilities=(CAP,))
    elif change == "revision":
        config = replace(config, offer_revision=2)
    elif change == "definition":
        helper = replace(READ_DEFINITION, result_limit_bytes=1024)
        config = replace(config, registrations=(registrations[0], LocalCapability(helper, ("reconcile.workspace.digest",))))
    elif change == "action":
        config = replace(config, registrations=(registrations[0], LocalCapability(READ_DEFINITION, ("tool.workspace.read",))))
    elif change == "retry":
        bound = replace(command, retry_class="reconcile_before_retry")
        bound = CapabilityExecutionContext(READ_CAP.id, READ_DEFINITION.adapter_id, selected).bind(bound).signed(PRIVATE_KEY).as_dict()
    else:
        actions = ACTIONS
    with pytest.raises(CapabilityError, match="capability_changed"):
        service(config=config, actions=lambda: actions).prepare(bound, now=100)


def test_only_real_available_implementations_and_policy_intersection_are_reported():
    subject = service(actions=lambda: frozenset({"tool.workspace.read"}))
    report = subject.offer(now=100)
    assert report.capabilities == ()
    assert report.local_allowed_capabilities == ()
    assert report.observed_at == "1970-01-01T00:01:40Z"
    assert not {"node_id", "owner_id", "trusted", "readOnly"} & report.payload().keys()


def test_default_observation_is_current_interpreter_and_disk_without_guessing_free_memory(tmp_path):
    import platform
    observed = observe_local_capabilities(tmp_path)
    assert dict(observed.software) == {"python": platform.python_version()}
    assert observed.os == platform.system().lower()
    assert observed.architecture == platform.machine()
    assert observed.memory_bytes == 0
    assert type(observed.disk_bytes) is int and observed.disk_bytes >= 0


class ReportTransport:
    def __init__(self):
        self.requests = []
        self.status = 200
        self.payload = {"ok": True, "offer": {"node_id": "node-a", "offer_revision": 1,
            "received_at": "1970-01-01T00:01:40Z"}}

    def request_bounded_json(self, method, url, body, headers, **kwargs):
        self.requests.append((method, url, json.loads(body), dict(headers), kwargs))
        return self.status, self.payload


def reporter(wire, **changes):
    kwargs = dict(hub_url="https://hub.invalid", credential="node-a:synthetic", transport=wire,
        clock=lambda: 100, monotonic=lambda: 10, should_cancel=lambda: False)
    return NodeCapabilityReporter(service(), **(kwargs | changes))


def test_reporter_uses_original_machine_credential_bounded_deadline_and_exact_receipt():
    transport = ReportTransport()
    receipt = reporter(transport).report()
    assert receipt.node_id == "node-a" and receipt.offer_revision == 1
    method, url, body, headers, kwargs = transport.requests[0]
    assert (method, url) == ("POST", "https://hub.invalid/api/platform/v1/nodes/capability-offers")
    assert headers["X-Platform-Node-Credential"] == "node-a:synthetic"
    assert kwargs["deadline"] == 25 and kwargs["max_response_bytes"] == 65536
    assert body["software"] == {"python": "3.13.7"} and "node_id" not in body


@pytest.mark.parametrize("offer", [
    {"node_id": "other", "offer_revision": 1, "received_at": "1970-01-01T00:01:40Z"},
    {"node_id": "node-a", "offer_revision": 2, "received_at": "1970-01-01T00:01:40Z"},
    {"node_id": "node-a", "offer_revision": True, "received_at": "1970-01-01T00:01:40Z"},
    {"node_id": "node-a", "offer_revision": 1, "received_at": "1970-01-01T00:01:40+00:00"},
    {"node_id": "node-a", "offer_revision": 1, "received_at": "invalidZ"}, {}, [],
])
def test_reporter_refuses_wrong_scope_revision_or_malformed_receipt(offer):
    transport = ReportTransport()
    transport.payload = {"ok": True, "offer": offer}
    with pytest.raises(NodeCapabilityReportError, match="invalid_capability_receipt"):
        reporter(transport).report()


def test_report_errors_keep_cause_without_retry_or_credential_in_message():
    transport = ReportTransport()
    transport.status, transport.payload = 409, {"error": "offer_revision_conflict"}
    with pytest.raises(NodeCapabilityReportError, match="capability_report_rejected") as failure:
        reporter(transport).report()
    assert failure.value.status == 409 and len(transport.requests) == 1
    assert "synthetic" not in str(failure.value)
    def broken(*args, **kwargs):
        raise OSError("synthetic network failure")
    transport.request_bounded_json = broken
    with pytest.raises(NodeCapabilityReportError, match="capability_report_transport") as failure:
        reporter(transport).report()
    assert isinstance(failure.value.__cause__, OSError)


def test_stopped_reporter_does_not_start_network_request():
    transport = ReportTransport()
    with pytest.raises(NodeCapabilityReportError, match="node_stopped"):
        reporter(transport, should_cancel=lambda: True).report()
    assert not transport.requests


def runtime(tmp_path: Path, *, config=None, observer=None, transport=None):
    return NodeRuntime(NodeRuntimeConfig(node_id="node-a", credential="node-a:synthetic",
        workspace_root=tmp_path / "workspace", workspace_id="workspace", journal_path=tmp_path / "node.db", hub_url="https://hub.invalid",
        public_key=PUBLIC_KEY, capabilities=frozenset({"workspace.read", "workspace.write"}),
        capability_config=config or configuration()), transport=transport or ReportTransport(), clock=lambda: 100,
        capability_observer=observer or facts)


def test_runtime_blocks_changed_software_before_file_write_and_replays_original_completion(tmp_path):
    state = [facts()]
    subject = runtime(tmp_path, observer=lambda: state[0])
    first = subject.client.handle(signed_command())
    assert first["status"] == "succeeded"
    output = tmp_path / "workspace" / "result.txt"
    assert output.read_text() == "first"
    state[0] = facts(software=(("python", "3.13.8"),))
    assert subject.client.handle(signed_command()) == first
    rejected = subject.client.handle(signed_command(command_id="new-command", arguments={"path": "blocked.txt", "content": "blocked"}))
    assert rejected["status"] == "failed" and rejected["result"]["error_code"] == "capability_changed"
    assert output.read_text() == "first"
    assert not (output.parent / "blocked.txt").exists()


def test_old_runtime_rejects_new_context_instead_of_ignoring_it(tmp_path):
    subject = NodeRuntime(NodeRuntimeConfig(node_id="node-a", credential="node-a:synthetic",
        workspace_root=tmp_path / "workspace", workspace_id="workspace", journal_path=tmp_path / "node.db", hub_url="https://hub.invalid",
        public_key=PUBLIC_KEY, capabilities=frozenset({"workspace.write"})), clock=lambda: 100)
    rejected = subject.client.handle(signed_command())
    assert rejected["status"] == "failed" and rejected["result"]["error_code"] == "capability_unavailable"
    assert not (tmp_path / "workspace" / "result.txt").exists()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_report_never_follows_credential_redirect_on_real_http(peer, status):
    from tools.transport import Transport
    peer.status, peer.body, peer.extra = status, b"", {"Location": "/credential-sink"}
    with pytest.raises(NodeCapabilityReportError, match="capability_report_rejected") as failure:
        reporter(Transport(mode="direct", attempts=3), hub_url=peer.url.rsplit("/", 1)[0], monotonic=time.monotonic).report()
    assert failure.value.status == status
    assert len(peer.requests) == 1
    assert peer.requests[0][1] == "/api/platform/v1/nodes/capability-offers"


@pytest.mark.parametrize("body", [b"x" * 65537, b"[]", b"not-json"], ids=["oversized", "array", "invalid-json"])
def test_report_rejects_unbounded_or_invalid_real_http_response(peer, body):
    from tools.transport import Transport
    peer.body = body
    with pytest.raises(NodeCapabilityReportError, match="capability_report_transport") as failure:
        reporter(Transport(mode="direct", attempts=3), hub_url=peer.url.rsplit("/", 1)[0], monotonic=time.monotonic).report()
    assert isinstance(failure.value.__cause__, ValueError)
    assert len(peer.requests) == 1


class PollTransport(ReportTransport):
    def post_json(self, url, body, headers):
        self.requests.append(("POST", url, dict(body), dict(headers), {}))
        return 200, {"ok": True, "commands": []}


def test_poll_reports_current_facts_but_report_failure_does_not_close_inflight_poll(tmp_path):
    transport = PollTransport()
    subject = runtime(tmp_path, transport=transport)
    assert subject.poll_once() == {"ok": True, "commands": 0, "receipts": 0}
    assert [item[1].rsplit("/", 1)[-1] for item in transport.requests] == ["capability-offers", "poll"]
    transport.status = 409
    result = subject.poll_once()
    assert result == {"ok": True, "commands": 0, "receipts": 0, "capability_report_error": "capability_report_rejected"}
    assert len(transport.requests) == 4
    subject.close_all()
    with pytest.raises(NodeCapabilityReportError, match="node_stopped"):
        subject.report_capabilities()
    assert len(transport.requests) == 4


def test_unconfigured_runtime_does_not_add_reports_or_change_manifest(tmp_path):
    transport = PollTransport()
    subject = NodeRuntime(NodeRuntimeConfig(node_id="node-a", credential="node-a:synthetic",
        workspace_root=tmp_path / "workspace", workspace_id="workspace", journal_path=tmp_path / "node.db", hub_url="https://hub.invalid",
        capabilities=frozenset({"workspace.read"})), transport=transport, clock=lambda: 100)
    assert subject.poll_once() == {"ok": True, "commands": 0, "receipts": 0}
    assert len(transport.requests) == 1 and transport.requests[0][1].endswith("/poll")
    assert subject.manifest() == {"node_id": "node-a", "worker_id": "node-a", "capabilities": {"workspace.read": True}}


def test_offer_does_not_advertise_exec_without_actual_sandbox_launcher(tmp_path):
    config = configuration(registrations=(LocalCapability(DEFINITION, ("tool.workspace.exec",)),))
    subject = NodeRuntime(NodeRuntimeConfig(node_id="node-a", credential="node-a:synthetic",
        workspace_root=tmp_path / "workspace", workspace_id="workspace", journal_path=tmp_path / "node.db", hub_url="https://hub.invalid",
        public_key=PUBLIC_KEY, capabilities=frozenset({"workspace.exec"}), capability_config=config),
        transport=ReportTransport(), clock=lambda: 100, capability_observer=facts)
    assert subject.capability_service.offer(now=100).capabilities == ()


def test_default_capability_observer_uses_explicit_workspace_binding(tmp_path):
    unused = tmp_path / "unused-default"
    actual = tmp_path / "actual-workspace"
    subject = NodeRuntime(NodeRuntimeConfig(node_id="node-a", credential="node-a:synthetic",
        workspace_root=unused, workspace_roots={"workspace": actual}, journal_path=tmp_path / "node.db",
        hub_url="https://hub.invalid", public_key=PUBLIC_KEY,
        capabilities=frozenset({"workspace.read", "workspace.write"}), capability_config=configuration()),
        transport=ReportTransport(), clock=lambda: 100)
    try:
        assert subject.backend is None
        assert subject.capability_service.offer(now=100).capabilities == (CAP,)
        assert actual.is_dir() and not unused.exists()
    finally:
        subject.close_all()


def test_exec_offer_resolves_explicit_workspace_map_without_single_root_fallback(tmp_path):
    config = configuration(registrations=(LocalCapability(DEFINITION, ("tool.workspace.exec",)),))
    subject = NodeRuntime(NodeRuntimeConfig(node_id="node-a", credential="node-a:synthetic",
        workspace_root=tmp_path / "unused-default", workspace_roots={"workspace": tmp_path / "actual-workspace"},
        journal_path=tmp_path / "node.db", hub_url="https://hub.invalid", public_key=PUBLIC_KEY,
        sandbox_launcher=("/usr/bin/env",), capabilities=frozenset({"workspace.exec"}), capability_config=config),
        transport=ReportTransport(), clock=lambda: 100, capability_observer=facts)
    try:
        assert subject.backend is None
        assert subject.capability_service.offer(now=100).capabilities == (CAP,)
    finally:
        subject.close_all()


@pytest.mark.parametrize("hub_url", ["https://name:secret@hub.invalid", "https://hub.invalid/?token=secret",
    "https://hub.invalid/#secret", "file:///tmp/hub", "https://hub.invalid/path"])
def test_report_rejects_non_origin_urls_before_exposing_credentials(hub_url):
    with pytest.raises(CapabilityError):
        reporter(ReportTransport(), hub_url=hub_url)


def test_capability_context_is_never_allowed_without_signature_even_on_legacy_client(tmp_path):
    from tools.platform.journal import NodeJournal
    from tools.platform.node_client import NodeClient
    journal = NodeJournal(tmp_path / "journal.db")
    journal.init()
    invoked = []
    client = NodeClient(journal, executor=lambda command: invoked.append(command), clock=lambda: 100, node_id="node-a")
    assert client.handle(signed_command())["reason"] == "invalid_signature"
    assert invoked == []


@pytest.mark.parametrize("value", [[], {}, None, "raw"])
def test_wrong_observation_type_keeps_execution_closed(value):
    with pytest.raises(CapabilityError, match="invalid_capability_observation"):
        service(observer=lambda: value).prepare(signed_command(), now=100)


def test_observer_error_keeps_original_cause_at_local_and_report_boundaries():
    def broken():
        raise OSError("probe failed")
    subject = service(observer=broken)
    with pytest.raises(CapabilityError, match="capability_observation_failed") as failure:
        subject.prepare(signed_command(), now=100)
    assert isinstance(failure.value.__cause__, OSError)
    client = NodeCapabilityReporter(subject, hub_url="https://hub.invalid", credential="node-a:synthetic",
        transport=ReportTransport(), should_cancel=lambda: False, clock=lambda: 100)
    with pytest.raises(NodeCapabilityReportError, match="capability_report_observation") as failure:
        client.report()
    assert isinstance(failure.value.__cause__.__cause__, OSError)


def test_report_deadline_includes_observation_and_final_receipt_validation():
    moments = iter((10, 25))
    with pytest.raises(NodeCapabilityReportError, match="capability_report_deadline"):
        reporter(ReportTransport(), monotonic=lambda: next(moments)).report()


@pytest.mark.parametrize("stopped", [False, True])
def test_observation_deadline_or_shutdown_prevents_starting_a_new_report(stopped):
    state = {"stopped": False, "elapsed": 0}
    def observed():
        state.update(stopped=stopped, elapsed=15)
        return facts()
    transport = ReportTransport()
    client = NodeCapabilityReporter(service(observer=observed), hub_url="https://hub.invalid",
        credential="node-a:synthetic", transport=transport, clock=lambda: 100,
        monotonic=lambda: state["elapsed"], should_cancel=lambda: state["stopped"])
    with pytest.raises(NodeCapabilityReportError):
        client.report()
    assert not transport.requests


@pytest.mark.parametrize("value", [None, 1, "https://hub.invalid:invalid", "https://hub.invalid\n"])
def test_invalid_origin_values_raise_typed_configuration_error(value):
    with pytest.raises(CapabilityError):
        reporter(ReportTransport(), hub_url=value)


def test_report_credential_must_have_exact_node_secret_shape():
    with pytest.raises(CapabilityError):
        reporter(ReportTransport(), credential="node-a:first:second")


@pytest.mark.parametrize("change", [{"clock": None}, {"monotonic": 1}, {"should_cancel": False}, {"transport": object()}])
def test_invalid_report_ports_are_rejected_at_assembly(change):
    with pytest.raises(CapabilityError, match="invalid_capability_configuration"):
        reporter(ReportTransport(), **change)


def test_unknown_command_never_reexecutes_after_capability_changes(tmp_path):
    state, writes = [facts()], []
    subject = runtime(tmp_path, observer=lambda: state[0])
    original = subject.backend.write
    def interrupted(path, content):
        writes.append(path)
        original(path, content)
        raise OSError("lost completion")
    subject.backend.write = interrupted
    command = signed_command()
    first = subject.client.handle(command)
    assert first["status"] == "unknown"
    state[0] = facts(software=(("python", "changed-version"),))
    assert subject.client.handle(command) == first
    assert writes == ["result.txt"]


@pytest.mark.parametrize("change", [
    {"offer_revision": True}, {"slots": 1.5}, {"registrations": ()},
    {"local_allowed_capabilities": (CatalogRef("unregistered", 1),)},
])
def test_invalid_local_configuration_is_rejected(change):
    with pytest.raises(CapabilityError):
        configuration(**change)
