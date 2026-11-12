"""Deterministic capability selection; reports never grant authority."""
from __future__ import annotations

import itertools
import json
from dataclasses import replace

import pytest

from hub.domain.capability import (
    AuthorizedNode, CapabilityDefinition, CapabilityError, NodePolicy,
    SelectionRequirements, parse_offer,
)
from hub.domain.workflow_contract_types import CapabilityRequirement, CatalogRef
from hub.application.capability_router import select_target


OWNER = "owner@example.test"
WORKSPACE = "workspace-a"
BOOT = "hub-boot-a"
CAP = CatalogRef("coding.review", 1)


def definition(**changes):
    values = dict(capability_id=CAP.id, revision=1, adapter_id="cli", input_schema_ref="in-v1",
        output_schema_ref="out-v1", input_schema=b'{"type":"object"}', output_schema=b'{"type":"object"}',
        risk="workspace_read", retry_class="reconcile_before_retry", allowed_owner_roles=("operator",),
        max_active_seconds=1800, result_limit_bytes=65536)
    return CapabilityDefinition(**(values | changes))


def requirements(**changes):
    values = dict(owner_id=OWNER, workspace_id=WORKSPACE, template=CatalogRef("fixture", 1), owner_role="operator",
        adapter_id="cli", capabilities=(CapabilityRequirement(CAP.id, (1,)),), input_digest="a" * 64,
        operating_systems=("linux",), architectures=("x86_64",), software=(("python", "3.10.21"),),
        input_aliases=("source",), residency_aliases=("private",), memory_bytes=100, disk_bytes=200)
    return SelectionRequirements(**(values | changes))


def offer_payload(**changes):
    values = dict(schema_version=1, offer_revision=1, capabilities=[{"id": CAP.id, "revision": 1}],
        local_allowed_capabilities=[{"id": CAP.id, "revision": 1}], os="linux", architecture="x86_64",
        software={"python": "3.10.21"}, slots=2, memory_bytes=1000, disk_bytes=2000,
        observed_at="2026-10-23T00:00:00Z", health="healthy")
    return values | changes


def node(node_id="node-a", *, priority=0, **changes):
    policy = NodePolicy(OWNER, node_id, (WORKSPACE,), (CatalogRef("fixture", 1),), (CAP,),
        priority=priority, slot_limit=2, input_aliases=("source",), residency_aliases=("private",),
        allowed_risks=("workspace_read",))
    offer = parse_offer(json.dumps(offer_payload()).encode(), received_at=100, boot_id=BOOT)
    values = dict(policy=policy, offer=offer, enabled=True, credential_enabled=True, workspace_enabled=True,
        held_slots=0, held_memory_bytes=0, held_disk_bytes=0)
    return AuthorizedNode(**(values | changes))


def select(nodes, *, req=None, defs=None, now=100, boot_id=BOOT):
    return select_target(req or requirements(), tuple(nodes), tuple(defs or (definition(),)), now=now, boot_id=boot_id)


@pytest.mark.parametrize(("nodes", "selected"), [
    ((node("node-z"), node("node-b", held_slots=1), node("node-c", priority=-1), node("node-a", priority=-1)), "node-a"),
    ((node("node-a"), node("node-z", priority=-1, held_slots=1)), "node-z"),
    ((node("node-a", held_slots=1), node("node-z")), "node-z"),
    ((node("node-z"), node("node-a")), "node-a"),
], ids=["combined", "priority-before-occupancy", "occupancy-before-node-id", "node-id-tie"])
def test_selection_is_stable_for_every_input_order_and_does_not_sort_candidates(nodes, selected):
    for order in itertools.permutations(nodes):
        result = select(order)
        assert result.binding.node_id == selected
        assert result.binding.input_digest == "a" * 64
        assert result.binding.capabilities[0].definition_digest == definition().digest
        assert not result.exclusions


@pytest.mark.parametrize(("change", "reason"), [
    ({"enabled": False}, "node_disabled"), ({"credential_enabled": False}, "node_disabled"),
    ({"workspace_enabled": False}, "workspace_forbidden"),
    ({"offer": None}, "offer_missing"), ({"held_slots": 2}, "capacity_exhausted"),
    ({"held_memory_bytes": 901}, "memory_insufficient"), ({"held_disk_bytes": 1801}, "disk_insufficient"),
])
def test_first_exclusion_reason_and_waiting_node(change, reason):
    result = select((node(**change),))
    assert result.binding is None and result.state == "waiting_node"
    assert result.exclusions[0].reason_code == reason


@pytest.mark.parametrize(("field", "value", "reason"), [
    ("owner_id", "other", "owner_forbidden"), ("workspaces", ("other",), "workspace_forbidden"),
    ("templates", (CatalogRef("other", 1),), "template_forbidden"),
    ("input_aliases", (), "input_unreachable"), ("residency_aliases", (), "residency_forbidden"),
    ("capabilities", (), "policy_forbidden"), ("allowed_risks", (), "policy_forbidden"),
])
def test_admin_policy_and_aliases_cannot_be_supplied_by_node(field, value, reason):
    candidate = node()
    candidate = replace(candidate, policy=replace(candidate.policy, **{field: value}))
    assert select((candidate,)).exclusions[0].reason_code == reason


@pytest.mark.parametrize(("change", "reason"), [
    ({"os": "windows"}, "platform_mismatch"), ({"architecture": "arm64"}, "platform_mismatch"),
    ({"software": {"python": "3.13.7"}}, "software_mismatch"),
    ({"capabilities": []}, "capability_unavailable"),
    ({"capabilities": [{"id": CAP.id, "revision": 2}]}, "revision_mismatch"),
    ({"local_allowed_capabilities": []}, "policy_forbidden"),
    ({"health": "degraded"}, "node_unhealthy"), ({"slots": 0}, "capacity_exhausted"),
])
def test_report_implementation_health_and_local_policy_filters(change, reason):
    reported = parse_offer(json.dumps(offer_payload(**change)).encode(), received_at=100, boot_id=BOOT)
    assert select((node(offer=reported),)).exclusions[0].reason_code == reason


def test_freshness_uses_received_clock_and_requires_a_report_after_boot():
    candidate = node()
    assert select((candidate,), now=220).binding is not None
    assert select((candidate,), now=220.001).exclusions[0].reason_code == "offer_stale"
    assert select((candidate,), now=99).exclusions[0].reason_code == "offer_stale"
    assert select((candidate,), boot_id="new-boot").exclusions[0].reason_code == "offer_stale"
    future = parse_offer(json.dumps(offer_payload(observed_at="9999-12-31T23:59:59Z")).encode(), received_at=100, boot_id=BOOT)
    assert select((node(offer=future),), now=221).binding is None


@pytest.mark.parametrize("change", [
    {"trusted": True}, {"readOnly": True}, {"owner_id": OWNER}, {"node_id": "other"},
    {"offer_revision": True}, {"offer_revision": 2**53}, {"slots": 2.0}, {"slots": -1},
    {"memory_bytes": 1.1}, {"disk_bytes": 2**53}, {"health": "unknown"},
    {"observed_at": "2026-10-23T00:00:00"}, {"observed_at": "2026-10-23T08:00:00+08:00"},
    {"software": {"python": "bad\nversion"}}, {"capabilities": [{"id": CAP.id, "revision": 1}] * 2},
])
def test_invalid_untrusted_report_is_rejected(change):
    with pytest.raises(CapabilityError):
        parse_offer(json.dumps(offer_payload(**change)).encode(), received_at=100, boot_id=BOOT)


def test_catalogue_pins_canonical_schema_and_rejects_external_references():
    original = definition()
    assert CapabilityDefinition.from_snapshot(original.snapshot) == original
    assert original.digest == definition(input_schema=b'{ "type" : "object" }').digest
    assert definition(revision=2).digest != original.digest
    with pytest.raises(CapabilityError, match="external_schema_reference"):
        definition(input_schema=b'{"$ref":"https://example.test/schema"}')
    with pytest.raises(CapabilityError):
        definition(input_schema=json.dumps({"a": "x" * 32768}).encode())


def test_exclusion_list_is_bounded_and_does_not_expose_other_owners():
    denied = tuple(node(f"node-{i:03}", enabled=False) for i in range(100))
    result = select(denied)
    assert len(result.exclusions) == 32 and result.excluded_count == 100
    foreign = replace(node("secret"), policy=replace(node().policy, owner_id="other", node_id="secret"))
    result = select((foreign,))
    assert result.excluded_count == 1 and result.exclusions[0].node_id is None
    with pytest.raises(CapabilityError, match="candidate_limit"):
        select(denied * 3)


@pytest.mark.parametrize("change", [
    {"os": {}}, {"health": []}, {"architecture": {}},
    {"capabilities": [{"id": [], "revision": 1}]},
    {"capabilities": [{"id": CAP.id, "revision": {}}]},
    {"local_allowed_capabilities": [None]}, {"software": {"python": []}},
])
def test_hostile_json_types_produce_typed_errors(change):
    with pytest.raises(CapabilityError):
        parse_offer(json.dumps(offer_payload(**change)).encode(), received_at=100, boot_id=BOOT)


def test_requested_role_limits_and_all_compatible_revisions_use_policy_intersection():
    with pytest.raises(CapabilityError):
        replace(node().policy, input_aliases=([],))
    assert select((node(),), req=requirements(owner_role="viewer")).exclusions[0].reason_code == "policy_forbidden"
    assert select((node(),), defs=(definition(max_active_seconds=59),)).exclusions[0].reason_code == "policy_forbidden"
    assert select((node(),), defs=(definition(result_limit_bytes=100),)).exclusions[0].reason_code == "policy_forbidden"
    offered = offer_payload(capabilities=[{"id": CAP.id, "revision": 1}, {"id": CAP.id, "revision": 2}])
    observed = parse_offer(json.dumps(offered).encode(), received_at=100, boot_id=BOOT)
    result = select((node(offer=observed),), req=requirements(capabilities=(CapabilityRequirement(CAP.id, (1, 2)),)),
                    defs=(definition(), definition(revision=2)))
    assert result.binding.capabilities[0].revision == 1


def test_corrupt_definition_roles_and_nested_revision_types_have_stable_errors():
    value = json.loads(definition().snapshot)
    value["allowed_owner_roles"] = None
    with pytest.raises(CapabilityError):
        CapabilityDefinition.from_snapshot(json.dumps(value).encode())
    with pytest.raises(CapabilityError):
        requirements(capabilities=(CapabilityRequirement(CAP.id, ([],)),))


def test_selection_uses_registration_time_schema_without_redecoding_per_candidate(monkeypatch):
    import hub.domain.capability as module
    item = definition()
    nodes = tuple(node(f"node-{i:03}") for i in range(64))
    original = module.decode
    def reject_schema_decode(raw, **kwargs):
        assert raw != b'{"type":"object"}', "schema must be frozen at registration"
        return original(raw, **kwargs)
    monkeypatch.setattr(module, "decode", reject_schema_decode)
    result = select(nodes, defs=(item,))
    assert result.binding.node_id == "node-000"


@pytest.mark.parametrize("change", [
    {"capabilities": [{"id": f"cap-{index}", "revision": 1} for index in range(129)]},
    {"software": {f"package-{index}": "1" for index in range(65)}},
])
def test_report_collection_limits_are_enforced_before_selection(change):
    with pytest.raises(CapabilityError):
        parse_offer(json.dumps(offer_payload(**change)).encode(), received_at=100, boot_id=BOOT)


def test_corrupt_definition_retry_policy_is_a_typed_validation_error():
    value = json.loads(definition().snapshot)
    value["retry_class"] = {}
    with pytest.raises(CapabilityError):
        CapabilityDefinition.from_snapshot(json.dumps(value).encode())
