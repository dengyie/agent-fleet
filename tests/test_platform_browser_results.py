"""Browser outcomes must fit the real JSON boundary before journal/transport."""
import json

import pytest

from hub.domain.control import generate_ed25519_keypair
from hub.domain.platform_command import PlatformCommand
from tools.platform.browser_backend import BrowserBackendError
from tools.platform.journal import NodeJournal
from tools.platform.node_client import NodeClient
from tools.platform.node_executor import NodeToolExecutor
from support.browser import browser_backend, URL


def backend_result(value):
    class Driver:
        def open(self, url):
            return None

        def snapshot(self):
            return value

        def submit(self, selector):
            return value

        def screenshot(self):
            return value

    backend = browser_backend(Driver, submit_enabled=True)
    session = backend.execute("browser.open", {"url": URL}, run_id="run-a")["session_id"]
    return backend, session


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"),
                                   2**53, -(2**53), 10**1000, "\ud800", {"\ud800": "value"},
                                   {"value": b"raw bytes"}, b"raw bytes"],
                         ids=["nan", "inf", "negative-inf", "unsafe-positive-id", "unsafe-negative-id", "huge-int", "surrogate-value", "surrogate-key", "nested-bytes", "bytes"])
def test_browser_rejects_non_json_or_unsafe_numeric_result(tmp_path, value):
    backend, session = backend_result(value)
    private, public = generate_ed25519_keypair()
    journal = NodeJournal(tmp_path / "journal.db")
    journal.init()
    node = NodeClient(journal, executor=NodeToolExecutor(None, browser_enabled=True, browser_backend=backend),
                      node_id="node-a", public_key=public, require_signature=True, clock=lambda: 100)
    command = PlatformCommand.create(
        command_id="cmd-result", target_node="node-a", action="tool.browser.snapshot",
        resource_id="workspace-a", arguments={"session_id": session},
        retry_class="manual_only", expires_at=200, run_id="run-a",
    ).signed(private)
    receipt = node.handle(command.as_dict())
    assert receipt["status"] == "failed"
    assert receipt["result"]["error_code"] == "invalid_backend_receipt"
    assert journal.get("cmd-result")["state"] == "failed"
    json.dumps(receipt, allow_nan=False)


@pytest.mark.parametrize("value", ["\x00" * 3000, ["x" * 63] * 256,
                                   {"\x00" * 400 + str(i): "value" for i in range(8)}],
                         ids=["escaped-value", "list-punctuation", "escaped-keys"])
def test_browser_serialized_aggregate_budget_includes_escaping_and_punctuation(value):
    assert len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()) > 16 * 1024
    backend, session = backend_result(value)
    with pytest.raises(BrowserBackendError, match="result_too_large"):
        backend.execute("browser.snapshot", {"session_id": session}, run_id="run-a")


@pytest.mark.parametrize("extra", [0, 1])
def test_browser_json_result_exact_byte_boundary(extra):
    value = {"value": "x" * (16 * 1024 - 12 + extra)}
    assert len(json.dumps(value, separators=(",", ":")).encode()) == 16 * 1024 + extra
    backend, session = backend_result(value)
    if extra:
        with pytest.raises(BrowserBackendError, match="result_too_large"):
            backend.execute("browser.snapshot", {"session_id": session}, run_id="run-a")
        return
    assert backend.execute("browser.snapshot", {"session_id": session}, run_id="run-a")["result"] == value


def test_browser_safe_scalars_and_string_ids_preserve_value():
    value = {"text": "中文\n\"quoted\"", "id": str(2**63), "values": [None, True, False, 0, 2**53 - 1, -(2**53 - 1), 0.5]}
    backend, session = backend_result(value)
    assert backend.execute("browser.snapshot", {"session_id": session}, run_id="run-a")["result"] == value


@pytest.mark.parametrize("extra", [0, 1])
def test_browser_binary_budget_remains_exclusive_to_screenshot(extra):
    value = b"\x89PNG\r\n\x1a\n" + b"\x00" * (256 * 1024 - 8 + extra)
    backend, session = backend_result(value)
    if extra:
        with pytest.raises(BrowserBackendError, match="result_too_large"):
            backend.execute("browser.screenshot", {"session_id": session}, run_id="run-a")
        return
    assert backend.execute("browser.screenshot", {"session_id": session}, run_id="run-a")["result"] == value


@pytest.mark.parametrize("value", ["not-png", {"image": "not-png"}, b"not-png"])
def test_screenshot_requires_png_bytes(value):
    backend, session = backend_result(value)
    with pytest.raises(BrowserBackendError, match="invalid_backend_receipt"):
        backend.execute("browser.screenshot", {"session_id": session}, run_id="run-a")
