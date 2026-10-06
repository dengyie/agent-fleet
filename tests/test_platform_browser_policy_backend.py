import pytest

from tools.platform.browser_backend import (
    BrowserBackendError,
    BrowserCleanupError,
    BrowserExecutionError,
    LocalBrowserBackend,
)
from hub.domain.platform_command import PlatformCommand
from tools.platform.browser_policy import validate_url
from tools.platform.journal import NodeJournal
from tools.platform.node_client import BrowserSessionSyncError, NodeClient
from tools.platform.node_executor import BROWSER_TOOLS, NodeToolExecutor


GLOBAL = "93.184.216.34"


def _resolver(host, _port, *, type):
    return [(2, 1, 6, "", (GLOBAL, 443))]


def test_policy_matches_effective_default_https_port():
    assert validate_url(
        "https://one.example/page",
        network_enabled=True,
        allowed_origins=("https://one.example:443",),
        resolver=_resolver,
    ) == "https://one.example/page"


@pytest.mark.parametrize("value", [
    "https://one.example:0/path", "https://one.example:/path",
    "https://one.example../path", "https://one.example/path\n",
])
def test_policy_rejects_ambiguous_authorities(value):
    from tools.platform.browser_policy import BrowserPolicyError

    with pytest.raises(BrowserPolicyError, match="invalid_url"):
        validate_url(
            value, network_enabled=True, allowed_origins=("https://one.example",),
            resolver=_resolver,
        )


def test_policy_normalizes_scheme_case_and_rejects_duplicate_origins():
    assert validate_url(
        "HTTPS://ONE.EXAMPLE/path",
        network_enabled=True,
        allowed_origins=("https://one.example:443",),
        resolver=_resolver,
    ) == "HTTPS://ONE.EXAMPLE/path"

    from tools.platform.browser_policy import BrowserPolicyError

    with pytest.raises(BrowserPolicyError, match="invalid_url"):
        validate_url(
            "https://one.example/path", network_enabled=True,
            allowed_origins=("https://one.example", "HTTPS://ONE.EXAMPLE:443/"),
            resolver=_resolver,
        )


def test_backend_failure_preserves_driver_cause_without_exposing_it_in_text():
    cause = ValueError("driver-private-detail")

    class Driver:
        def open(self, _url):
            raise cause

    backend = LocalBrowserBackend(Driver, network_enabled=False)

    with pytest.raises(BrowserBackendError, match="backend_failed") as caught:
        backend.execute("browser.open", {"url": "http://localhost:3000"}, run_id="run-a")

    assert caught.value.__cause__ is cause
    assert str(caught.value) == "backend_failed"
    assert "driver-private-detail" not in str(caught.value)


def test_post_dispatch_driver_failure_is_unknown_and_keeps_cause():
    cause = RuntimeError("driver-interrupted-after-dispatch")

    class Driver:
        def open(self, _url):
            return None

        def navigate(self, _url):
            raise cause

    backend = LocalBrowserBackend(Driver, network_enabled=False)
    opened = backend.execute(
        "browser.open", {"url": "http://localhost:3000"}, run_id="run-a",
    )

    with pytest.raises(BrowserExecutionError, match="backend_interrupted") as caught:
        backend.execute(
            "browser.navigate",
            {"session_id": opened["session_id"], "url": "http://localhost:3000/next"},
            run_id="run-a",
        )

    assert caught.value.__cause__ is cause
    assert str(caught.value) == "backend_interrupted"


def test_cleanup_failures_are_bounded_and_keep_primary_cause():
    primary = RuntimeError("driver-private-open")
    cleanup = RuntimeError("driver-private-close")

    class Driver:
        def open(self, _url):
            raise primary

        def close(self):
            raise cleanup

    with pytest.raises(BrowserBackendError) as caught:
        LocalBrowserBackend(Driver, network_enabled=False).execute(
            "browser.open", {"url": "http://localhost:3000"}, run_id="run-a",
        )

    assert caught.value.code == "backend_failed"
    assert isinstance(caught.value.__cause__, BrowserCleanupError)
    assert caught.value.__cause__.failure_count == 2
    assert str(caught.value) == "backend_failed"


def test_close_all_reports_bounded_cleanup_failure_without_driver_text():
    class Driver:
        def open(self, _url):
            return None

        def close(self):
            raise RuntimeError("driver-private-close")

    backend = LocalBrowserBackend(Driver, network_enabled=False)
    backend.execute("browser.open", {"url": "http://localhost:3000"}, run_id="run-a")

    with pytest.raises(BrowserCleanupError) as caught:
        backend.close_all()

    assert caught.value.code == "backend_cleanup_failed"
    assert caught.value.failure_count == 1
    assert str(caught.value) == "backend_cleanup_failed"
    assert "driver-private-close" not in str(caught.value)


def test_node_client_journals_browser_dispatch_failure_as_unknown(tmp_path):
    cause = RuntimeError("driver-private-navigation-detail")

    class Driver:
        def open(self, _url):
            return None

        def navigate(self, _url):
            raise cause

    backend = LocalBrowserBackend(Driver, network_enabled=False)
    opened = backend.execute(
        "browser.open", {"url": "http://localhost:3000"}, run_id="run-a",
    )
    executor = NodeToolExecutor(
        None, allowed_tools=BROWSER_TOOLS, browser_backend=backend,
        browser_enabled=True,
    )
    command = PlatformCommand.create(
        command_id="run-a:step:2", target_node="node-a",
        action="tool.browser.navigate", resource_id="workspace-a",
        arguments={
            "session_id": opened["session_id"],
            "url": "http://localhost:3000/next",
        },
        retry_class="manual_only", expires_at=9_999_999_999, run_id="run-a",
    ).as_dict()

    class Transport:
        def __init__(self):
            self.receipts = []

        def post_json(self, url, body, _headers):
            if url.endswith("/nodes/poll"):
                return 200, {"ok": True, "commands": [command]}
            if url.endswith("/nodes/receipts"):
                self.receipts.append(dict(body))
                return 200, {"ok": True}
            raise AssertionError("unexpected Node route")

    journal = NodeJournal(tmp_path / "browser-journal.db")
    journal.init()
    transport = Transport()
    client = NodeClient(
        journal, executor=executor, node_id="node-a", credential="node-a:fixture",
        hub_url="https://hub.example.test", transport=transport,
    )

    assert client.poll_once() == {"ok": True, "commands": 1, "receipts": 1}
    assert journal.get(command["command_id"])["state"] == "unknown"
    assert transport.receipts[0]["status"] == "unknown"
    assert transport.receipts[0]["result"] == {"reason": "executor_interrupted"}
    assert "driver-private-navigation-detail" not in str(transport.receipts)


def test_node_client_redacts_dispatch_url_ip_body_and_tls_details(tmp_path):
    markers = (
        "https://one.example/private?token=url-marker",
        "203.0.113.77",
        "response-body-marker",
        "tls-detail-marker",
    )
    cause = RuntimeError(" ".join(markers))

    class Driver:
        def open(self, _url):
            return None

        def navigate(self, _url):
            raise cause

    backend = LocalBrowserBackend(Driver, network_enabled=False)
    opened = backend.execute(
        "browser.open", {"url": "http://localhost:3000"}, run_id="run-a",
    )
    executor = NodeToolExecutor(
        None, allowed_tools=BROWSER_TOOLS, browser_backend=backend,
        browser_enabled=True,
    )
    command = PlatformCommand.create(
        command_id="run-a:step:leak", target_node="node-a",
        action="tool.browser.navigate", resource_id="workspace-a",
        arguments={
            "session_id": opened["session_id"],
            "url": "http://localhost:3000/next",
        },
        retry_class="manual_only", expires_at=9_999_999_999, run_id="run-a",
    ).as_dict()

    class Transport:
        def __init__(self):
            self.receipts = []

        def post_json(self, url, body, _headers):
            if url.endswith("/nodes/poll"):
                return 200, {"ok": True, "commands": [command]}
            if url.endswith("/nodes/receipts"):
                self.receipts.append(dict(body))
                return 200, {"ok": True}
            raise AssertionError("unexpected Node route")

    journal = NodeJournal(tmp_path / "leak-journal.db")
    journal.init()
    transport = Transport()
    client = NodeClient(
        journal, executor=executor, node_id="node-a", credential="node-a:fixture",
        hub_url="https://hub.example.test", transport=transport,
    )

    assert client.poll_once() == {"ok": True, "commands": 1, "receipts": 1}
    journal_row = journal.get(command["command_id"])
    wire_receipt = transport.receipts[0]
    assert journal_row["state"] == "unknown"
    assert wire_receipt == {
        "command_id": command["command_id"],
        "status": "unknown",
        "worker_id": "node-a",
        "result": {"reason": "executor_interrupted"},
    }
    for marker in markers:
        assert marker not in str(journal_row)
        assert marker not in str(wire_receipt)


def test_browser_session_sync_failure_preserves_sync_cause_and_cleans_up():
    sync_error = RuntimeError("hub-private-sync-detail")
    close_calls = []

    class Backend:
        def close_session(self, session_id):
            close_calls.append(session_id)

    class Transport:
        def post_json(self, _url, _body, _headers):
            raise sync_error

    client = NodeClient(
        NodeJournal(":memory:"), executor=type("Executor", (), {
            "browser_backend": Backend(),
        })(), node_id="node-a", credential="node-a:fixture",
        hub_url="https://hub.example.test", transport=Transport(),
    )
    command = {"action": "tool.browser.open", "command_id": "cmd-open"}
    result = {"result": {"session_id": "session-opaque-1234", "state": "open"}}

    with pytest.raises(BrowserSessionSyncError) as caught:
        client._materialize_browser_session_result(command, result)

    assert caught.value.code == "browser_session_sync_failed"
    assert caught.value.__cause__ is sync_error
    assert caught.value.cleanup_failed is False
    assert caught.value.sync_error is sync_error
    assert caught.value.cleanup_error is None
    assert close_calls == ["session-opaque-1234"]
    assert str(caught.value) == "browser_session_sync_failed"
    assert "hub-private-sync-detail" not in str(caught.value)


def test_browser_session_sync_failure_retains_cleanup_failure_without_leaking_details():
    sync_error = RuntimeError("hub-private-sync-detail")
    cleanup_error = RuntimeError("driver-private-cleanup-detail")

    class Backend:
        def close_session(self, _session_id):
            raise cleanup_error

    class Transport:
        def post_json(self, _url, _body, _headers):
            raise sync_error

    client = NodeClient(
        NodeJournal(":memory:"), executor=type("Executor", (), {
            "browser_backend": Backend(),
        })(), node_id="node-a", credential="node-a:fixture",
        hub_url="https://hub.example.test", transport=Transport(),
    )

    with pytest.raises(BrowserSessionSyncError) as caught:
        client._materialize_browser_session_result(
            {"action": "tool.browser.open", "command_id": "cmd-open"},
            {"result": {"session_id": "session-opaque-1234", "state": "open"}},
        )

    assert caught.value.cleanup_failed is True
    assert caught.value.__cause__ is sync_error
    assert caught.value.sync_error is sync_error
    assert caught.value.cleanup_error is cleanup_error
    assert str(caught.value) == "browser_session_sync_failed"
    assert "driver-private-cleanup-detail" not in str(caught.value)


def test_submit_policy_screens_selector_and_arguments():
    from tools.platform.browser_policy import BrowserPolicyError, validate_action

    result = validate_action("browser.submit", {"session_id": "s" * 24, "selector": "#go"})
    assert result == {"session_id": "s" * 24, "selector": "#go"}

    with pytest.raises(BrowserPolicyError, match="sensitive_field_forbidden"):
        validate_action("browser.submit", {"session_id": "s" * 24, "selector": "#password"})
    with pytest.raises(BrowserPolicyError, match="invalid_selector"):
        validate_action("browser.submit", {"session_id": "s" * 24, "selector": ""})
    with pytest.raises(BrowserPolicyError, match="invalid_arguments"):
        validate_action(
            "browser.submit", {"session_id": "s" * 24, "selector": "#go", "url": "x"})


def test_submit_requires_gate_and_dispatches_to_driver_submit():
    class Driver:
        def __init__(self):
            self.submits = []

        def open(self, _url):
            return None

        def submit(self, selector):
            self.submits.append(selector)
            return {"clicked": True}

    driver = Driver()
    gated = LocalBrowserBackend(lambda: driver, network_enabled=False)
    opened = gated.execute("browser.open", {"url": "http://localhost:3000"}, run_id="run-a")
    with pytest.raises(BrowserBackendError, match="submit_disabled"):
        gated.execute(
            "browser.submit",
            {"session_id": opened["session_id"], "selector": "#go"}, run_id="run-a")
    assert driver.submits == []

    enabled = LocalBrowserBackend(lambda: driver, network_enabled=False, submit_enabled=True)
    opened = enabled.execute("browser.open", {"url": "http://localhost:3000"}, run_id="run-a")
    receipt = enabled.execute(
        "browser.submit",
        {"session_id": opened["session_id"], "selector": "#go"}, run_id="run-a")
    assert receipt["result"] == {"clicked": True}
    assert driver.submits == ["#go"]


def test_submit_after_dispatch_failure_is_unknown_and_run_bound():
    class Driver:
        def open(self, _url):
            return None

        def submit(self, _selector):
            raise RuntimeError("driver-private-submit-failure")

    backend = LocalBrowserBackend(Driver, network_enabled=False, submit_enabled=True)
    opened = backend.execute("browser.open", {"url": "http://localhost:3000"}, run_id="run-a")
    with pytest.raises(BrowserExecutionError, match="backend_interrupted"):
        backend.execute(
            "browser.submit",
            {"session_id": opened["session_id"], "selector": "#go"}, run_id="run-a")

    other = LocalBrowserBackend(Driver, network_enabled=False, submit_enabled=True)
    opened = other.execute("browser.open", {"url": "http://localhost:3000"}, run_id="run-a")
    with pytest.raises(BrowserBackendError, match="session_not_found"):
        other.execute(
            "browser.submit",
            {"session_id": opened["session_id"], "selector": "#go"}, run_id="run-b")
