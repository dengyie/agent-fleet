"""Contracts for the assistant execution-window control plane integration."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLATFORM_API = ROOT / "frontend" / "assets" / "api" / "platform.js"
ASSISTANT_VIEW = ROOT / "frontend" / "assets" / "views" / "assistant.js"


def _function_block(source, name):
    match = re.search(rf"export async function {name}\b.*?\n}}", source, re.S)
    assert match is not None, name
    return match.group(0)


def test_platform_api_exposes_window_lifecycle_wrappers():
    source = PLATFORM_API.read_text()
    for name in (
        "createExecutionWindow",
        "listExecutionWindows",
        "getExecutionWindow",
        "attachExecutionWindow",
        "reconnectExecutionWindow",
        "acquireExecutionWindowWriter",
        "renewExecutionWindowWriter",
        "releaseExecutionWindowWriter",
        "closeExecutionWindow",
        "getExecutionWindowEvents",
    ):
        block = _function_block(source, name)
        assert "platformRequest(" in block
        assert "fetch(" not in block
    assert "apiPath('platform', 'v1', 'execution-windows')" in source
    assert "encodeURIComponent" in source
    assert "Math.max(1, Math.min(50" in source


def test_assistant_uses_a_separate_window_cursor_and_recovery_flow():
    source = (ASSISTANT_VIEW.read_text() + (ROOT / 'frontend/assets/views/assistant/panels.js').read_text())
    for value in (
        "createExecutionWindow",
        "listExecutionWindows",
        "reconnectExecutionWindow",
        "attachExecutionWindow",
        "acquireExecutionWindowWriter",
        "renewExecutionWindowWriter",
        "getExecutionWindowEvents",
        "windowEventCursor",
        "windowEventCursor",
        "lease_conflict",
        "read-only",
        "window",
        "关闭执行窗口",
    ):
        assert value in source
    close_block = source.split("async function closeWindow()", 1)[-1].split("function renderMemoryState", 1)[0]
    assert "activeRunId = null" not in close_block


def test_assistant_window_is_dom_safe_and_does_not_cancel_on_close_or_unload():
    source = (ASSISTANT_VIEW.read_text() + (ROOT / 'frontend/assets/views/assistant/panels.js').read_text())
    assert "innerHTML" not in source
    assert "fetch(" not in source
    assert "textContent" in source
    assert "createElement" in source
    assert "cancelRun(" in source
    close_block = source.split("async function closeWindow()", 1)[-1].split("function renderMemoryState", 1)[0]
    assert "cancelRun(" not in close_block
    assert "beforeunload" not in source


def test_window_api_parameters_are_bounded_and_encoded():
    source = PLATFORM_API.read_text()
    list_block = _function_block(source, "listExecutionWindows")
    events_block = _function_block(source, "getExecutionWindowEvents")
    assert "Math.max(1, Math.min(50" in list_block
    assert "encodeURIComponent(String(bounded))" in list_block
    assert "encodeURIComponent(String(cursor))" in events_block
