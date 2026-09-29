"""Static contracts for the explicit assistant-to-legacy-task bridge."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "frontend" / "assets" / "api" / "platform.js"
VIEW = ROOT / "frontend" / "assets" / "views" / "assistant.js"


def test_platform_api_uses_bounded_platform_request_for_legacy_task():
    source = API.read_text()
    assert "createPlatformLegacyTask" in source
    assert "getPlatformLegacyTask" in source
    assert "legacy-task" in source
    assert "platformRequest(" in source
    assert "fetch(" not in source
    assert "getConversations" in source
    assert "Math.min(100" in source


def test_assistant_requires_explicit_legacy_control_and_uses_dom_safe_projection():
    source = (VIEW.read_text() + (ROOT / 'frontend/assets/views/assistant/panels.js').read_text())
    assert "打开旧任务关联" in source
    assert "createPlatformLegacyTask" in source
    assert "getPlatformLegacyTask" in source
    assert "legacyMachine.value.trim()" in source
    assert "legacyProject.value.trim()" in source
    assert "legacyInstruction.value.trim()" in source
    assert "renderLegacyProjection" in source
    assert "innerHTML" not in source
    assert "fetch(" not in source
    assert "diff.patch_bytes" in source
    assert "projection.files" in source
    assert "assistant-history" in source
    assert "refreshConversationHistory" in source
    assert "pagePath('conversation', conversation.conversation_id)" in source
    assert "innerHTML" not in source
