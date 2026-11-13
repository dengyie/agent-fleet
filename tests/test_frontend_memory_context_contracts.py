"""Contracts for the explicit MemoryItem picker in the assistant workspace."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLATFORM_API = ROOT / "frontend" / "assets" / "api" / "platform.js"
ASSISTANT_VIEW = ROOT / "frontend" / "assets" / "views" / "assistant.js"


class TestMemoryContextApiContractTests:
    def test_platform_api_exposes_bounded_memory_list_and_search(self):
        source = PLATFORM_API.read_text()
        assert "export async function getPlatformMemories" in source
        assert "export async function searchPlatformMemories" in source
        assert "apiPath('platform', 'v1', 'memory')" in source
        assert "apiPath('platform', 'v1', 'memory', 'search')" in source
        assert "encodeURIComponent(value)" in source
        assert "Math.max(1, Math.min(100" in source
        assert "platformRequest(" in source

    def test_memory_api_wrappers_do_not_bypass_http_boundary(self):
        source = PLATFORM_API.read_text()
        for name in ("getPlatformMemories", "searchPlatformMemories"):
            block = re.search(
                rf"export async function {name}.*?\n}}", source, re.S
            )
            assert block is not None
            assert "platformRequest(" in block.group(0)
            assert "fetch(" not in block.group(0)


class TestAssistantMemoryContextContractTests:
    def test_assistant_renders_picker_states_and_selected_references(self):
        source = (ASSISTANT_VIEW.read_text() + (ROOT / 'frontend/assets/views/assistant/panels.js').read_text()
                  + (ROOT / 'frontend/assets/views/assistant/service.js').read_text())
        for value in (
            "getPlatformMemories",
            "searchPlatformMemories",
            "getMemories",
            "searchMemories",
            "refreshMemoryItems",
            "assistant-memory-context",
            "记忆上下文",
            "搜索记忆",
            "记忆上下文加载中",
            "记忆上下文不可用",
            "暂无匹配记忆",
            "已选记忆",
            "memory_context",
            "memory_ids",
            "revisions",
            "max_items",
            "max_bytes",
        ):
            assert value in source

    def test_assistant_memory_context_is_reference_only_and_dom_safe(self):
        view_source = (ASSISTANT_VIEW.read_text() + (ROOT / 'frontend/assets/views/assistant/panels.js').read_text())
        service_source = (ROOT / 'frontend/assets/views/assistant/service.js').read_text()
        assert "innerHTML" not in view_source
        assert "fetch(" not in view_source
        assert "api/platform" not in view_source
        assert "textContent" in view_source
        assert "createElement" in view_source
        assert "payload.memory_context = {" in service_source
        assert "memory_ids: memoryContext.memoryIds.slice()" in service_source
        assert "content:" not in service_source
        assert "var references = (Array.isArray(data.memories) ? data.memories : []).map(memoryReference).filter(Boolean);" in view_source
