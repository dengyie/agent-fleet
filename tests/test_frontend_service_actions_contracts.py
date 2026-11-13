from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"


def test_platform_action_wrappers_are_bounded_and_shell_wired():
    api = (FRONTEND / "assets/api/platform.js").read_text(encoding="utf-8")
    shell = (FRONTEND / "index.html").read_text(encoding="utf-8") + (FRONTEND / "assets/app.js").read_text()
    assert "export async function requestPlatformServiceAction" in api
    assert "export async function getPlatformApproval" in api
    assert "export async function decidePlatformApproval" in api
    assert "apiPath('platform', 'v1', 'services', segment(serviceId), 'actions')" in api
    assert "action !== 'inspect' && action !== 'restart'" in api
    assert "Idempotency-Key" in api
    assert "apiPath('platform', 'v1', 'approvals', segment(grantId))" in api
    assert "decision !== 'approve' && decision !== 'reject'" in api
    assert "...platform" in shell
    assert "...platform" in shell
    assert "...platform" in shell


def test_monitoring_renders_gated_actions_and_bounded_approval_flow():
    source = (FRONTEND / "assets/views/monitoring.js").read_text(encoding="utf-8")
    assert "allowed_actions" in source
    assert "requestPlatformServiceAction" in source
    assert "getPlatformApproval" in source
    assert "decidePlatformApproval" in source
    assert "inspect" in source and "restart" in source
    assert "MAX_APPROVAL_POLLS = 5" in source
    assert "setTimeout" in source
    assert "innerHTML" not in source
    assert "argv" not in source
    assert "raw_output" not in source
    assert "approval_required" in source
    assert "approval_decision" in source


def test_existing_monitoring_contract_mentions_action_degradation():
    source = (FRONTEND / "assets/views/monitoring.js").read_text(encoding="utf-8")
    assert "动作接口不可用" in source
    assert "服务监控仍可用" in source
