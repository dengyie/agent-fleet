"""Contracts for the assistant workspace artifact surface."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLATFORM_API = ROOT / "frontend" / "assets" / "api" / "platform.js"
ASSISTANT_VIEW = ROOT / "frontend" / "assets" / "views" / "assistant.js"


class TestArtifactApiContractTests:
    def test_platform_api_exposes_bounded_manifest_and_safe_content_url(self):
        source = PLATFORM_API.read_text()
        assert "export async function getPlatformArtifacts" in source
        assert "export function getPlatformArtifactContentUrl" in source
        assert "Math.max(1, Math.min(1000" in source
        assert "encodeURIComponent(String(workspace))" in source
        assert "apiPath('platform', 'v1', 'workspaces', segment(workspaceId), 'artifacts')" in source
        assert "export async function getPlatformArtifactPreview" in source
        assert "apiPath('platform', 'v1', 'artifacts', segment(artifactId), 'preview')" in source

    def test_platform_api_keeps_artifact_request_on_platform_request_boundary(self):
        source = PLATFORM_API.read_text()
        block = re.search(r"export async function getPlatformArtifacts.*?\n}", source, re.S)
        assert block is not None
        assert "platformRequest(" in block.group(0)
        assert "fetch(" not in block.group(0)


class TestAssistantArtifactViewContractTests:
    def test_assistant_renders_workspace_artifacts_and_downloads(self):
        source = (ASSISTANT_VIEW.read_text() + (ROOT / 'frontend/assets/views/assistant/panels.js').read_text())
        assert "getPlatformArtifacts" in source
        assert "getPlatformArtifactContentUrl" in source
        assert "assistant-artifacts" in source
        assert "download" in source
        assert "工作区产物" in source
        assert "加载中" in source
        assert "暂无产物" in source
        assert "refreshRunEvents" in source
        assert "getRunEvents(runId, 0)" in source
        assert "if (type !== 'textarea') field.type = type;" in source
        assert "getPlatformArtifactPreview" in source
        assert "el('pre'" in source
        assert "truncated" in source

    def test_assistant_artifact_view_is_dom_safe_and_has_no_direct_http(self):
        source = (ASSISTANT_VIEW.read_text() + (ROOT / 'frontend/assets/views/assistant/panels.js').read_text())
        assert "innerHTML" not in source
        assert "fetch(" not in source
        assert "textContent" in source
        assert "createElement" in source
        assert "workspace_id" in source
