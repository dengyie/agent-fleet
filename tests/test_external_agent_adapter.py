import pytest

from connectors.external_agents.base import (
    CancelReceipt, ExternalAgentCapabilities, ExternalAgentError, ExternalTaskRef,
)
from connectors.external_agents.muse import MuseExternalAgentAdapter


class Bridge:
    def __init__(self, submit=None, read=None):
        self.submit_result = submit or {"task_id": "dispatch-1", "state": "sent"}
        self.read_result = read or {"task_id": "dispatch-1", "state": "completed", "reply": "done"}
        self.calls = []

    def submit(self, task_id, prompt, *, scope):
        self.calls.append(("submit", task_id, prompt, scope))
        return self.submit_result

    def read(self, task_id, *, scope):
        self.calls.append(("read", task_id, scope))
        return self.read_result


def test_muse_declares_only_proven_capabilities_and_preserves_identity():
    bridge = Bridge()
    adapter = MuseExternalAgentAdapter(bridge, workspace_id="workspace")
    assert adapter.capabilities.submit_idempotent
    assert adapter.capabilities.lookup_by_token
    assert not adapter.capabilities.cancel_supported
    assert not adapter.capabilities.output_export_supported
    task = adapter.submit("dispatch-1", {"prompt": "do the work"})
    assert task.external_id == "dispatch-1"
    assert adapter.inspect(task).state == "succeeded"
    assert bridge.calls[0] == ("submit", "dispatch-1", "do the work", "workspace")


def test_uncertain_send_never_creates_a_new_task_or_allows_resubmit():
    bridge = Bridge(submit={"task_id": "dispatch-1", "state": "unknown"})
    adapter = MuseExternalAgentAdapter(bridge, workspace_id="workspace")
    with pytest.raises(ExternalAgentError) as caught:
        adapter.submit("dispatch-1", {"prompt": "once"})
    assert caught.value.code == "dispatch_unknown"
    assert caught.value.uncertain
    assert len([call for call in bridge.calls if call[0] == "submit"]) == 1


def test_cancel_requested_is_not_confirmed_and_export_is_explicitly_unsupported():
    adapter = MuseExternalAgentAdapter(Bridge(), workspace_id="workspace")
    task = ExternalTaskRef("muse", "dispatch-1", "dispatch-1")
    receipt = adapter.request_cancel(task)
    assert receipt.state == "unsupported"
    assert not receipt.requested and not receipt.confirmed
    with pytest.raises(ExternalAgentError, match="output_export_unsupported"):
        adapter.export_outputs(task, ())


def test_status_and_scope_fail_closed():
    adapter = MuseExternalAgentAdapter(Bridge(read={"state": "offline"}), workspace_id="workspace")
    task = ExternalTaskRef("muse", "dispatch-1", "dispatch-1")
    assert adapter.inspect(task).state == "unknown"
    with pytest.raises(ExternalAgentError, match="external_task_mismatch"):
        adapter.inspect(ExternalTaskRef("other", "dispatch-1", "task-1"))


def test_cancel_receipt_cannot_claim_confirmation_without_cancelled_state():
    with pytest.raises(ExternalAgentError):
        CancelReceipt("cancelling", requested=True, confirmed=True)


def test_capability_flags_reject_inconsistent_confirmation():
    with pytest.raises(ExternalAgentError):
        ExternalAgentCapabilities(False, False, False, False, True, False)


def test_muse_adapter_maps_requested_cancel_and_export_without_hidden_fallback():
    adapter = MuseExternalAgentAdapter(Bridge(), workspace_id="workspace")
    task = ExternalTaskRef("muse", "dispatch-1", "dispatch-1")
    cancel = adapter.request_cancel(task)
    assert (cancel.requested, cancel.confirmed, cancel.state) == (False, False, "unsupported")


@pytest.mark.parametrize("state,requested,confirmed", [("cancelled", True, False), ("cancelled", False, True), ("unsupported", True, False), ("cancelling", False, False)])
def test_cancel_receipt_cannot_contradict_its_evidence(state, requested, confirmed):
    with pytest.raises(ExternalAgentError, match="invalid_cancel_receipt"):
        CancelReceipt(state, requested, confirmed)


def test_muse_foreign_task_receipt_cannot_become_success_evidence():
    adapter = MuseExternalAgentAdapter(Bridge(read={"task_id": "foreign-task", "state": "completed", "reply": "not ours"}), workspace_id="workspace")
    status = adapter.inspect(ExternalTaskRef("muse", "dispatch-1", "dispatch-1"))
    assert status.state == "unknown" and status.error_code == "external_task_mismatch"


def test_muse_explicit_lookup_recovers_same_task_without_submit(tmp_path):
    from tools.browser_bridge.muse import MuseBridge
    from test_muse_browser_bridge import Transport

    transport = Transport()
    bridge = MuseBridge(tmp_path / "muse.db", transport=transport)
    adapter = MuseExternalAgentAdapter(bridge, workspace_id="workspace")
    assert adapter.lookup("dispatch-1", {"prompt": "once"}) is None
    original = adapter.submit("dispatch-1", {"prompt": "once"})
    assert adapter.lookup("dispatch-1", {"prompt": "once"}) == original
    assert len(transport.sends) == 1
    with pytest.raises(ExternalAgentError, match="task_conflict") as error:
        adapter.lookup("dispatch-1", {"prompt": "changed"})
    assert error.value.__cause__ is not None


def test_muse_workflow_identity_requires_explicit_account_and_tracks_journal_scope(tmp_path):
    from tools.browser_bridge.muse import MuseBridge
    from test_muse_browser_bridge import Transport

    bridge = MuseBridge(tmp_path / "muse.db", transport=Transport())
    assert MuseExternalAgentAdapter(bridge, workspace_id="workspace").configuration_digest is None
    bridge.initialize()
    adapter = MuseExternalAgentAdapter(bridge, workspace_id="workspace", account_ref="account-a", transport_ref="fixture-v1")
    first = adapter.configuration_digest
    assert isinstance(first, str) and len(first) == 64
    assert first == MuseExternalAgentAdapter(bridge, workspace_id="workspace", account_ref="account-a", transport_ref="fixture-v1").configuration_digest
    assert first != MuseExternalAgentAdapter(bridge, workspace_id="workspace", account_ref="account-b", transport_ref="fixture-v1").configuration_digest
    bridge.path = tmp_path / "different.db"
    assert adapter.configuration_digest != first
