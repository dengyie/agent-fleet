from pathlib import Path
import struct
import zlib
import pytest

from flask import Request

from hub.application.run_worker_service import LocalRunWorkerService
from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.domain.platform_command import PlatformCommand
from hub.infrastructure.browser_repository import BrowserRepository
from hub.domain.control import generate_ed25519_keypair
from hub.http.node_routes import _png_dimensions
from tools.platform.backends.directory import DirectoryBackend
from tools.platform.node_client import NodeClient
from tools.platform.node_executor import BROWSER_TOOLS, NodeToolExecutor
from tools.platform.journal import NodeJournal
from tools.platform.providers.base import ModelResponse
from support.browser import ORIGIN, URL, browser_backend, resolve_origin


OWNER = "owner@example.test"
PNG = b"\x89PNG\r\n\x1a\nfixture-screenshot"


def _app(tmp_path: Path, *, browser=False, submit=False,
         execution_windows=False):
    signing_private, signing_public = generate_ed25519_keypair() if submit else (None, None)
    app = create_app(FleetConfig.from_root(
        tmp_path, ingest_token="ingest", dev_operator=OWNER,
        platform_enabled=True, platform_browser_enabled=browser,
        platform_browser_network_enabled=submit,
        platform_browser_submit_enabled=submit,
        platform_command_signing_raw=signing_private,
        execution_windows_enabled=execution_windows,
    ))
    if submit:
        app.extensions["fleet"]["submit_test_signing_public"] = signing_public
    return app


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    checksum = zlib.crc32(kind + data) & 0xffffffff
    return len(data).to_bytes(4, "big") + kind + data + checksum.to_bytes(4, "big")


def _png(width: int, height: int, *, compressed_pixels: bytes | None = None) -> bytes:

    header = struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0)
    pixels = b"".join(b"\x00" + b"\x11\x22\x33" * width for _ in range(height))
    compressed = zlib.compress(pixels) if compressed_pixels is None else compressed_pixels
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header)
            + _png_chunk(b"IDAT", compressed) + _png_chunk(b"IEND", b""))


def test_png_frame_dimensions_reject_incomplete_or_oversized_images():
    from hub.application.task_service import ApplicationError
    valid = _png(2, 1)
    assert _png_dimensions(valid) == (2, 1)
    with pytest.raises(ApplicationError) as incomplete:
        _png_dimensions(valid[:-4])
    assert incomplete.value.code == "invalid_png"
    with pytest.raises(ApplicationError) as oversized:
        _png_dimensions(_png(4097, 1))
    assert oversized.value.code == "invalid_png"
    malformed_idat = _png(2, 1, compressed_pixels=b"not-a-zlib-stream")
    with pytest.raises(ApplicationError) as malformed:
        _png_dimensions(malformed_idat)
    assert malformed.value.code == "invalid_png"


class FlaskTransport:
    """Injected in-process transport; no network socket is opened."""

    def __init__(self, client):
        self.client = client
        self.requests = []
        self.responses = []

    def post_json(self, url, body, headers):
        path = "/" + url.split("/", 3)[-1]
        self.requests.append((path, dict(body), dict(headers)))
        response = self.client.post(path, json=body, headers=headers)
        self.responses.append((path, response.get_json()))
        return response.status_code, response.get_json()

    def post_bytes(self, url, body, headers):
        path = "/" + url.split("/", 3)[-1]
        self.requests.append((path, bytes(body), dict(headers)))
        response = self.client.post(path, data=body, headers=headers)
        self.responses.append((path, response.get_json()))
        return response.status_code, response.get_json()


def test_worker_node_http_receipt_closes_remote_run_without_local_side_effect(tmp_path):
    app = _app(tmp_path)
    repo = app.extensions["fleet"]["platform_repository"]
    delivery = app.extensions["fleet"]["services"]["platform_delivery"]
    workspace_root = tmp_path / "remote-workspace"
    workspace_root.mkdir()
    (workspace_root / "report.md").write_text("from-node", encoding="utf-8")
    repo.upsert_node(OWNER, {"node_id": "node-http", "label": "HTTP node"})
    repo.provision_node_credential(OWNER, "node-http", secret="n" * 40)
    repo.upsert_model(OWNER, {"profile_id": "model", "provider": "deterministic", "model": "test"})
    repo.upsert_workspace(OWNER, {"workspace_id": "remote", "root_path": str(workspace_root)})
    conversation = repo.create_conversation(OWNER, "conv-http", title="", workspace_id="remote")
    repo.append_turn(
        OWNER, conversation["conversation_id"], "msg-http", "run-http",
        text="read report", client_token="http-e2e",
        config_snapshot={
            "workspace_id": "remote", "model_profile_id": "model",
            "execution_node_id": "node-http",
        }, now=1,
    )

    calls = []

    def executor(command):
        calls.append(command["command_id"])
        return NodeToolExecutor(DirectoryBackend(workspace_root))(command)

    journal = NodeJournal(tmp_path / "node-journal.db")
    journal.init()
    transport = FlaskTransport(app.test_client())
    node = NodeClient(
        journal, executor=executor, node_id="node-http",
        credential="node-http:" + "n" * 40,
        hub_url="https://hub.invalid", transport=transport,
        worker_id="node-worker",
    )

    class Provider:
        def __init__(self):
            self.calls = 0

        def complete(self, messages, tools, *, request_observer=None):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(kind="tool_call", tool="workspace.read", arguments={"path": "report.md"})
            return ModelResponse(kind="final", text="node read complete")

    class DeliveryBridge:
        def enqueue(self, command, *, idempotency_key=None):
            return delivery.enqueue(command, idempotency_key=idempotency_key)

        def wait_for_receipt(self, command_id, *, timeout_s=30.0):
            # A real Node loop would run independently. The bridge advances
            # the injected fixture once before the Hub performs bounded wait.
            node.poll_once()
            return delivery.wait_for_receipt(command_id, timeout_s=timeout_s, poll_interval_s=0.01)

    worker = LocalRunWorkerService(
        repo, app.extensions["fleet"]["services"]["run_events"],
        worker_id="hub-worker", provider_factory=lambda profile: Provider(),
        remote_execution_enabled=True, remote_delivery=DeliveryBridge(),
    )
    result = worker.run_once(OWNER)
    assert result["state"] == "succeeded"
    assert calls == ["run-http:step:1"]
    assert repo.get_run(OWNER, "run-http")["state"] == "succeeded"
    assert not (tmp_path / "assistant-report.md").exists()
    events = app.extensions["fleet"]["services"]["run_events"].list(OWNER, "run-http")["events"]
    assert [event["kind"] for event in events] == [
        "run_started", "tool_call", "node_command_queued", "node_receipt",
        "tool_result", "run_finished",
    ]
    assert [path for path, _, _ in transport.requests] == [
        "/api/platform/v1/nodes/poll", "/api/platform/v1/nodes/receipts",
    ]
    command_row = delivery.repository.get("run-http:step:1")
    assert command_row["status"] == "succeeded"

    # A duplicate delivery is absorbed by NodeJournal; the backend is not
    # called a second time and the terminal Hub receipt remains unchanged.
    duplicate = node.handle({
        "command_id": "run-http:step:1", "target_node": "node-http",
        "action": "tool.workspace.read", "resource_id": "remote",
        "arguments": {"path": "report.md"}, "retry_class": "read_only",
        "expires_at": 9_999_999_999,
        "args_hash": PlatformCommand.create(
            command_id="tmp", target_node="node-http", action="tool.workspace.read",
            resource_id="remote", arguments={"path": "report.md"},
            retry_class="read_only", expires_at=9_999_999_999,
        ).args_digest,
        "run_id": "run-http", "grant_id": None, "signature": None,
    })
    assert duplicate["status"] == "succeeded"
    assert calls == ["run-http:step:1"]


def test_browser_screenshot_uses_node_ticket_and_artifact_metadata(tmp_path):
    app = _app(tmp_path, browser=True)
    repo = app.extensions["fleet"]["platform_repository"]
    delivery = app.extensions["fleet"]["services"]["platform_delivery"]
    repo.upsert_node(OWNER, {"node_id": "node-browser", "label": "Browser"})
    credential = repo.provision_node_credential(
        OWNER, "node-browser", secret="b" * 40)["credential"]
    command = PlatformCommand.create(
        command_id="run-browser:step:1", target_node="node-browser",
        owner_id=OWNER, action="tool.browser.screenshot", resource_id="workspace-browser",
        arguments={"session_id": "session-opaque"}, retry_class="manual_only",
        expires_at=9_999_999_999, run_id="run-browser",
    )
    delivery.enqueue(command, idempotency_key=command.command_id)
    journal = NodeJournal(tmp_path / "browser-journal.db")
    journal.init()
    transport = FlaskTransport(app.test_client())
    node = NodeClient(
        journal, executor=lambda _command: {
            "state": "succeeded", "result": {"result": PNG},
        }, node_id="node-browser", credential=credential,
        hub_url="https://hub.invalid", transport=transport,
        worker_id="browser-worker",
    )

    result = node.poll_once()

    assert result == {"ok": True, "commands": 1, "receipts": 1}
    receipt = delivery.repository.get(command.command_id)
    assert receipt["status"] == "succeeded"
    assert receipt["result"]["result"]["artifact"]["content_type"] == "image/png"
    assert "result" not in receipt["result"]["result"]
    journal_row = journal.get(command.command_id)
    assert "result" not in journal_row["result"]["result"]
    artifact = app.extensions["fleet"]["services"]["platform_artifacts"].list(
        OWNER, "workspace-browser")
    assert len(artifact) == 1
    assert app.extensions["fleet"]["services"]["platform_artifacts"].read(
        OWNER, "workspace-browser", artifact[0]["artifact_id"]) == PNG
    paths = [item[0] for item in transport.requests]
    assert paths == [
        "/api/platform/v1/nodes/poll",
        "/api/platform/v1/nodes/browser-artifact-tickets",
        "/api/platform/v1/nodes/browser-artifact-tickets/" +
        transport.requests[2][0].rsplit("/", 2)[-2] + "/content",
        "/api/platform/v1/nodes/receipts",
    ]
    ticket_headers = transport.requests[1][2]
    upload_headers = transport.requests[2][2]
    assert ticket_headers["X-Platform-Node-Credential"] == credential
    assert upload_headers["X-Platform-Node-Credential"] == credential
    assert upload_headers["X-Platform-Worker-ID"] == "browser-worker"
    assert "X-Agent-Fleet-Token" not in ticket_headers
    assert "X-Agent-Fleet-Token" not in upload_headers


def test_browser_frame_upload_is_published_and_served_only_through_its_window(tmp_path):
    app = _app(tmp_path, browser=True, execution_windows=True)
    platform = app.extensions["fleet"]["platform_repository"]
    platform.upsert_node(OWNER, {"node_id": "node-frame", "label": "Frame node"})
    credential = platform.provision_node_credential(
        OWNER, "node-frame", secret="f" * 40,
    )["credential"]
    platform.upsert_workspace(OWNER, {
        "workspace_id": "workspace-frame", "root_path": str(tmp_path / "frame-workspace"),
    })
    platform.create_conversation(
        OWNER, "conv-frame", title="", workspace_id="workspace-frame",
    )
    platform.append_turn(
        OWNER, "conv-frame", "msg-frame", "run-frame", text="frame test",
        client_token="frame-turn",
        config_snapshot={"workspace_id": "workspace-frame", "execution_node_id": "node-frame"},
        now=100,
    )
    windows = app.extensions["fleet"]["repositories"]["execution_windows"]
    created = windows.create_window(OWNER, "run-frame")
    window_id = created["window"]["window_id"]
    windows.redeem_ticket(OWNER, window_id, created["attach_ticket"])
    browser = app.extensions["fleet"]["repositories"]["browser"]
    session_id = "session-frame-123456"
    browser.create_session(
        OWNER, workspace_id="workspace-frame", run_id="run-frame",
        node_id="node-frame", profile_id="profile-frame", session_id=session_id,
    )
    command = PlatformCommand.create(
        command_id="run-frame:step:1", target_node="node-frame", owner_id=OWNER,
        action="tool.browser.screenshot", resource_id="workspace-frame",
        arguments={"session_id": session_id}, retry_class="manual_only",
        expires_at=9999999999, run_id="run-frame",
    )
    delivery = app.extensions["fleet"]["services"]["platform_delivery"]
    delivery.enqueue(command, idempotency_key=command.command_id)
    client = app.test_client()
    node_headers = {"X-Platform-Node-Credential": credential}
    claimed = client.post(
        "/api/platform/v1/nodes/poll", json={"worker_id": "frame-worker"},
        headers=node_headers,
    ).get_json()["commands"][0]
    ticket_response = client.post(
        "/api/platform/v1/nodes/browser-artifact-tickets",
        json={"command_id": command.command_id, "worker_id": "frame-worker",
              "idempotency_key": command.command_id + ":screenshot"},
        headers=node_headers,
    )
    assert ticket_response.status_code == 200
    ticket = ticket_response.get_json()["ticket"]
    assert ticket["window_id"] == window_id
    png = _png(2, 1)
    uploaded = client.post(
        "/api/platform/v1/nodes/browser-artifact-tickets/" + ticket["ticket_id"] + "/content",
        data=png,
        headers={
            **node_headers, "Content-Type": "image/png",
            "X-Platform-Artifact-Upload-Token": ticket["upload_token"],
            "X-Platform-Command-ID": command.command_id,
            "X-Platform-Worker-ID": "frame-worker",
            "X-Platform-Artifact-Idempotency-Key": command.command_id + ":screenshot",
        },
    )
    assert uploaded.status_code == 200, uploaded.get_data(as_text=True)
    artifact_id = uploaded.get_json()["artifact"]["artifact_id"]
    events = windows.list_events(OWNER, window_id)["events"]
    assert [event["kind"] for event in events] == ["browser.frame"]
    assert events[0]["payload"]["artifact_id"] == artifact_id
    assert (events[0]["payload"]["width"], events[0]["payload"]["height"]) == (2, 1)

    frame_url = f"/api/platform/v1/execution-windows/{window_id}/frames/{artifact_id}"
    image = client.get(frame_url)
    assert image.status_code == 200
    assert image.mimetype == "image/png"
    assert image.data == png
    assert image.headers["Cache-Control"] == "private, no-store"
    foreign = client.get(frame_url, headers={
        "CF-Access-Authenticated-User-Email": "other@example.test",
    })
    assert foreign.status_code == 404


def _browser_run_app(tmp_path, *, submit=False):
    app = _app(tmp_path, browser=True, submit=submit)
    repo = app.extensions["fleet"]["platform_repository"]
    repo.upsert_node(OWNER, {
        "node_id": "node-browser-run", "label": "Browser run",
        "capabilities": {"browser.session": True, "browser.submit": submit},
    })
    credential = repo.provision_node_credential(
        OWNER, "node-browser-run", secret="r" * 40)["credential"]
    repo.upsert_model(OWNER, {"profile_id": "model", "provider": "deterministic", "model": "test"})
    repo.upsert_workspace(OWNER, {
        "workspace_id": "browser-workspace",
        "root_path": str(tmp_path / "browser-workspace"),
    })
    repo.create_conversation(OWNER, "conv-browser-run", title="",
                             workspace_id="browser-workspace")
    return app, repo, credential


def _browser_run_worker(app, repo, credential, tmp_path, *, driver_factory, provider,
                        submit=False):
    delivery = app.extensions["fleet"]["services"]["platform_delivery"]
    journal = NodeJournal(tmp_path / "browser-run-journal.db")
    journal.init()
    transport = FlaskTransport(app.test_client())
    node = NodeClient(
        journal,
        executor=NodeToolExecutor(
            None, allowed_tools=BROWSER_TOOLS,
            browser_backend=browser_backend(driver_factory, submit_enabled=submit),
            browser_enabled=True,
        ),
        node_id="node-browser-run", credential=credential,
        hub_url="https://hub.invalid", transport=transport,
        worker_id="browser-run-worker",
        public_key=app.extensions["fleet"].get("submit_test_signing_public"),
    )

    class DeliveryBridge:
        def enqueue(self, command, *, idempotency_key=None):
            return delivery.enqueue(command, idempotency_key=idempotency_key)

        def enqueue_submit(self, command, *, approvals, idempotency_key=None):
            return delivery.enqueue_submit(
                command, approvals=approvals, idempotency_key=idempotency_key)

        def wait_for_receipt(self, command_id, *, timeout_s=30.0):
            node.poll_once()
            return delivery.wait_for_receipt(
                command_id, timeout_s=timeout_s, poll_interval_s=0.01)

    worker = LocalRunWorkerService(
        repo, app.extensions["fleet"]["services"]["run_events"],
        worker_id="browser-run-hub-worker", provider_factory=lambda profile: provider,
        remote_execution_enabled=True, remote_delivery=DeliveryBridge(),
        browser_enabled=True,
        browser_network_enabled=True,
        browser_allowed_origins=(ORIGIN,), browser_resolver=resolve_origin,
        browser_submit_enabled=submit,
        submit_approvals=app.extensions["fleet"]["services"].get("submit_approvals"),
    )
    return worker, node, journal, transport


def _append_browser_turn(repo, *, run_id, client_token):
    repo.append_turn(
        OWNER, "conv-browser-run", "msg-" + run_id, run_id, text="use browser",
        client_token=client_token,
        config_snapshot={"workspace_id": "browser-workspace",
                         "model_profile_id": "model",
                         "execution_node_id": "node-browser-run"}, now=1,
    )


class _OpenThenProvider:
    def __init__(self, second_call):
        self.messages = []
        self._second_call = second_call

    def complete(self, messages, tools, *, request_observer=None):
        self.messages.append(list(messages))
        if not any(m["role"] == "tool" for m in messages):
            return ModelResponse(kind="tool_call", tool="browser.open",
                                 arguments={"url": URL})
        return self._second_call(messages)


def test_browser_dispatch_failure_markers_stay_off_hub_boundaries(tmp_path, caplog):
    markers = (
        "https://203.0.113.77/private?token=url-marker", "redirect-location-marker",
        "response-body-marker", "tls-detail-marker",
    )
    cause = RuntimeError(" ".join(markers))

    class Driver:
        def open(self, _url):
            return None

        def navigate(self, _url):
            raise cause

        def close(self):
            return None

    app, repo, credential = _browser_run_app(tmp_path)
    _append_browser_turn(repo, run_id="run-leak", client_token="leak-e2e")
    provider = _OpenThenProvider(lambda messages: ModelResponse(
        kind="tool_call", tool="browser.navigate",
        arguments={"session_id": messages[-1]["result"]["session_id"],
                   "url": ORIGIN + "/next"},
    ))
    worker, node, journal, transport = _browser_run_worker(
        app, repo, credential, tmp_path, driver_factory=lambda: Driver(),
        provider=provider,
    )

    import logging
    caplog.set_level(logging.DEBUG)
    result = worker.run_once(OWNER)

    assert result["state"] == "unknown"
    delivery = app.extensions["fleet"]["services"]["platform_delivery"]
    events = app.extensions["fleet"]["services"]["run_events"].list(OWNER, "run-leak")["events"]
    surfaces = {
        "run_events": str(events),
        "model_transcript": str(provider.messages),
        "hub_command_rows": str(delivery.repository.get("run-leak:step:1"))
        + str(delivery.repository.get("run-leak:step:2")),
        "wire_requests": str(transport.requests),
        "hub_response_bodies": str(transport.responses),
        "run_row": str(repo.get_run(OWNER, "run-leak")),
        "node_journal": str(journal.get("run-leak:step:2")),
        "captured_logs": caplog.text,
    }
    for name, text in surfaces.items():
        assert all(marker not in text for marker in markers), name
    tool_call = next(e["payload"] for e in events
                     if e["kind"] == "tool_call" and e["payload"]["step"] == 2)
    assert tool_call["argument_keys"] == ["session_id", "url"]
    tool_result = next(e["payload"] for e in events
                       if e["kind"] == "tool_result" and e["payload"]["step"] == 2)
    assert tool_result["state"] == "unknown"
    assert tool_result["error_code"] == "receipt_unknown"


def test_browser_success_content_flows_only_through_approved_planes(tmp_path):
    page = {"title": "page-content-marker", "text": "visible page body"}

    class Driver:
        def open(self, _url):
            return None

        def snapshot(self):
            return dict(page)

        def close(self):
            return None

    app, repo, credential = _browser_run_app(tmp_path)
    _append_browser_turn(repo, run_id="run-page", client_token="page-e2e")

    def snapshot_call(messages):
        if sum(m["role"] == "tool" for m in messages) >= 2:
            return ModelResponse(kind="final", text="done-page")
        return ModelResponse(kind="tool_call", tool="browser.snapshot",
                             arguments={"session_id": messages[-1]["result"]["session_id"]})

    provider = _OpenThenProvider(snapshot_call)
    worker, _node, _journal, _transport = _browser_run_worker(
        app, repo, credential, tmp_path, driver_factory=lambda: Driver(),
        provider=provider,
    )

    result = worker.run_once(OWNER)

    assert result["state"] == "succeeded"
    # Approved plane: the model transcript carries the backend result.
    tool_message = next(m for m in provider.messages[-1]
                        if m.get("tool") == "browser.snapshot")
    assert tool_message["result"]["result"]["title"] == "page-content-marker"
    # Approved plane: the Hub command receipt stores the backend result verbatim.
    # Deliberate policy lock: the Hub DB keeps page content as the model data
    # plane; a future metadata-only tightening is a conscious spec change.
    delivery = app.extensions["fleet"]["services"]["platform_delivery"]
    assert "page-content-marker" in str(delivery.repository.get("run-page:step:2"))
    # Metadata-only plane: run events carry status/error codes, never page content.
    events = app.extensions["fleet"]["services"]["run_events"].list(OWNER, "run-page")["events"]
    assert "page-content-marker" not in str(events)
    tool_result = next(e["payload"] for e in events
                       if e["kind"] == "tool_result" and e["payload"]["step"] == 2)
    assert tool_result["state"] == "succeeded"
    assert repo.get_run(OWNER, "run-page")["result_text"] == "done-page"


def test_browser_screenshot_pixels_stay_out_of_hub_metadata_planes(tmp_path, caplog):
    png = b"\x89PNG\r\n\x1a\n" + b"screenshot-pixel-marker" + b"\x00" * 16

    class Driver:
        def open(self, _url):
            return None

        def screenshot(self):
            return png

        def close(self):
            return None

    app, repo, credential = _browser_run_app(tmp_path)
    _append_browser_turn(repo, run_id="run-shot", client_token="shot-e2e")

    def screenshot_call(messages):
        if sum(m["role"] == "tool" for m in messages) >= 2:
            return ModelResponse(kind="final", text="shot-done")
        return ModelResponse(kind="tool_call", tool="browser.screenshot",
                             arguments={"session_id": messages[-1]["result"]["session_id"]})

    provider = _OpenThenProvider(screenshot_call)
    worker, _node, journal, transport = _browser_run_worker(
        app, repo, credential, tmp_path, driver_factory=lambda: Driver(),
        provider=provider,
    )

    import logging
    caplog.set_level(logging.DEBUG)
    result = worker.run_once(OWNER)

    assert result["state"] == "succeeded"
    delivery = app.extensions["fleet"]["services"]["platform_delivery"]
    events = app.extensions["fleet"]["services"]["run_events"].list(OWNER, "run-shot")["events"]
    artifacts = app.extensions["fleet"]["services"]["platform_artifacts"].list(
        OWNER, "browser-workspace")
    metadata_surfaces = {
        "run_events": str(events),
        "model_transcript": str(provider.messages),
        "hub_command_row": str(delivery.repository.get("run-shot:step:2")),
        "receipt_and_ticket_posts": str(
            [body for path, body, _headers in transport.requests
             if path.endswith(("/nodes/receipts", "/browser-artifact-tickets"))]),
        "artifact_registry_row": str(artifacts),
        "hub_response_bodies": str(transport.responses),
        "run_row": str(repo.get_run(OWNER, "run-shot")),
        "node_journal": str(journal.get("run-shot:step:2")),
        "captured_logs": caplog.text,
    }
    for name, text in metadata_surfaces.items():
        assert "screenshot-pixel-marker" not in text, name
    tool_message = next(m for m in provider.messages[-1]
                        if m.get("tool") == "browser.screenshot")
    assert tool_message["result"]["artifact"]["content_type"] == "image/png"
    assert "result" not in tool_message["result"]
    command_row = delivery.repository.get("run-shot:step:2")
    artifact_meta = command_row["result"]["result"]["artifact"]
    assert artifact_meta["content_type"] == "image/png"
    # Approved content plane: the artifact store keeps the exact uploaded bytes.
    assert len(artifacts) == 1
    assert app.extensions["fleet"]["services"]["platform_artifacts"].read(
        OWNER, "browser-workspace", artifacts[0]["artifact_id"]) == png


class _OpenSubmitThenFinal:
    def __init__(self, approve=None):
        self.messages = []
        self.approve = approve

    def complete(self, messages, tools, *, request_observer=None):
        self.messages.append(list(messages))
        if not any(m["role"] == "tool" for m in messages):
            return ModelResponse(kind="tool_call", tool="browser.open",
                                 arguments={"url": URL})
        if not any(m.get("tool") == "browser.submit" for m in messages):
            open_receipt = next(m for m in messages if m.get("tool") == "browser.open")
            if self.approve is not None:
                self.approve(open_receipt["result"]["session_id"])
            return ModelResponse(
                kind="tool_call", tool="browser.submit",
                arguments={"session_id": open_receipt["result"]["session_id"],
                           "selector": "#checkout-go"})
        return ModelResponse(kind="final", text="submit-done")


def test_browser_submit_is_approval_gated_and_single_use(tmp_path):
    class SubmitDriver:
        def __init__(self):
            self.submits = []

        def open(self, _url):
            return None

        def submit(self, selector):
            self.submits.append(selector)
            return {"state": "submitted"}

        def close(self):
            return None

    driver = SubmitDriver()
    app, repo, credential = _browser_run_app(tmp_path, submit=True)
    _append_browser_turn(repo, run_id="run-submit-e2e", client_token="submit-e2e")

    service = app.extensions["fleet"]["services"]["submit_approvals"]
    granted = {}
    def approve(session_id):
        granted["approval"] = service.grant(OWNER, "run-submit-e2e", body={
            "session_id": session_id, "selector": "#checkout-go",
        })
    provider = _OpenSubmitThenFinal(approve)
    worker, _node, _journal, _transport = _browser_run_worker(
        app, repo, credential, tmp_path, driver_factory=lambda: driver,
        provider=provider, submit=True,
    )
    result = worker.run_once(OWNER)

    assert result["state"] == "succeeded"
    # The owner granted through the production service before submit dispatch.
    assert driver.submits == ["#checkout-go"]
    tool_message = next(m for m in provider.messages[-1]
                        if m.get("tool") == "browser.submit")
    assert tool_message["result"]["result"] == {"state": "submitted"}
    # The durable command carries the approval id it consumed.
    delivery = app.extensions["fleet"]["services"]["platform_delivery"]
    command_row = delivery.repository.get("run-submit-e2e:step:2")
    assert command_row["arguments"]["approval_id"]
    stored = app.extensions["fleet"]["repositories"]["browser"].get_submit_approval(
        OWNER, command_row["arguments"]["approval_id"])
    assert stored["state"] == "consumed"
    assert stored["consumed_command_id"] == "run-submit-e2e:step:2"
    assert stored["selector"] == "#checkout-go"
    assert stored["node_id"] == "node-browser-run"
    # Single use: a second submit for the same selector needs a fresh grant.
    import pytest
    from hub.infrastructure.browser_repository import BrowserRepositoryError
    session_id = tool_message["result"]["session_id"]
    with pytest.raises(BrowserRepositoryError) as err:
        app.extensions["fleet"]["repositories"]["browser"].consume_submit_approval(
            OWNER, "run-submit-e2e", session_id=session_id, selector="#checkout-go",
            command_id="cmd-again", now=stored["consumed_at"] + 1,
        )
    # The run is terminal after worker completion, so the session fence is
    # evaluated before a consumed-approval lookup and rejects re-dispatch.
    assert err.value.code == "session_not_found"
    # Run events carry status only, never the page-bound selector or approval id.
    events = app.extensions["fleet"]["services"]["run_events"].list(
        OWNER, "run-submit-e2e")["events"]
    tool_result = next(e["payload"] for e in events
                       if e["kind"] == "tool_result" and e["payload"]["step"] == 2)
    assert tool_result["state"] == "succeeded"
    # Node journal and wire receipt record the opaque approval_id.
    journal_row = _journal.get("run-submit-e2e:step:2")
    assert journal_row["result"]["result"]["approval_id"] == command_row["arguments"]["approval_id"]
    receipt_post = next(body for path, body, _headers in _transport.requests
                        if path.endswith("/nodes/receipts") and body.get("command_id") == "run-submit-e2e:step:2")
    assert receipt_post["result"]["result"]["approval_id"] == command_row["arguments"]["approval_id"]


def test_browser_submit_markers_stay_off_metadata_planes(tmp_path, caplog):
    markers = (
        "synthetic-form-field-marker",
        "synthetic-page-content-marker",
        "sensitive-card-number-marker",
        "driver-submit-exception-marker",
    )
    cause = RuntimeError(" ".join(markers))

    class FailingSubmitDriver:
        def __init__(self):
            self.submits = []

        def open(self, _url):
            return None

        def submit(self, selector):
            self.submits.append(selector)
            raise cause

        def close(self):
            return None

    driver = FailingSubmitDriver()
    app, repo, credential = _browser_run_app(tmp_path, submit=True)
    _append_browser_turn(repo, run_id="run-submit-leak", client_token="submit-leak-e2e")

    service = app.extensions["fleet"]["services"]["submit_approvals"]
    def approve(session_id):
        service.grant(OWNER, "run-submit-leak", body={
            "session_id": session_id, "selector": "#checkout-go",
        })
    provider = _OpenSubmitThenFinal(approve)
    worker, _node, journal, transport = _browser_run_worker(
        app, repo, credential, tmp_path, driver_factory=lambda: driver,
        provider=provider, submit=True,
    )
    import logging
    caplog.set_level(logging.DEBUG)
    result = worker.run_once(OWNER)

    # After-dispatch driver failure yields unknown state
    assert result["state"] == "unknown"
    assert driver.submits == ["#checkout-go"]

    delivery = app.extensions["fleet"]["services"]["platform_delivery"]
    events = app.extensions["fleet"]["services"]["run_events"].list(OWNER, "run-submit-leak")["events"]
    artifacts = app.extensions["fleet"]["services"]["platform_artifacts"].list(
        OWNER, "browser-workspace")
    metadata_surfaces = {
        "run_events": str(events),
        "model_transcript": str(provider.messages),
        "hub_command_rows": str(delivery.repository.get("run-submit-leak:step:1"))
        + str(delivery.repository.get("run-submit-leak:step:2")),
        "wire_requests": str(transport.requests),
        "hub_response_bodies": str(transport.responses),
        "run_row": str(repo.get_run(OWNER, "run-submit-leak")),
        "node_journal": str(journal.get("run-submit-leak:step:2")),
        "artifact_registry_row": str(artifacts),
        "captured_logs": caplog.text,
    }
    for name, text in metadata_surfaces.items():
        assert all(marker not in text for marker in markers), name

    # Approval was consumed before dispatch and stays consumed (fail-safe direction)
    command_row = delivery.repository.get("run-submit-leak:step:2")
    approval_id = command_row["arguments"]["approval_id"]
    assert approval_id
    stored = app.extensions["fleet"]["repositories"]["browser"].get_submit_approval(
        OWNER, approval_id)
    assert stored["state"] == "consumed"
    assert stored["consumed_command_id"] == "run-submit-leak:step:2"

    # Run events carry status only, never page content or selector
    tool_call = next(e["payload"] for e in events
                     if e["kind"] == "tool_call" and e["payload"]["step"] == 2)
    assert tool_call["argument_keys"] == ["selector", "session_id"]
    tool_result = next(e["payload"] for e in events
                       if e["kind"] == "tool_result" and e["payload"]["step"] == 2)
    assert tool_result["state"] == "unknown"
    assert tool_result["error_code"] == "receipt_unknown"


def test_browser_submit_consumes_through_production_service_assembly(tmp_path):
    """No test double anywhere: the worker keeps the bootstrap-wired
    SubmitApprovalService, the owner grants through its public grant method
    between the open and submit steps, and the broker consumes through the
    real service layer (broker -> service -> repository)."""
    class SubmitDriver:
        def __init__(self):
            self.submits = []

        def open(self, _url):
            return None

        def submit(self, selector):
            self.submits.append(selector)
            return {"state": "submitted"}

        def close(self):
            return None

    driver = SubmitDriver()
    app, repo, credential = _browser_run_app(tmp_path, submit=True)
    _append_browser_turn(repo, run_id="run-submit-real", client_token="submit-real")
    service = app.extensions["fleet"]["services"]["submit_approvals"]

    granted = {}

    class OwnerGrantsOnOpen:
        def __init__(self):
            self.messages = []

        def complete(self, messages, tools, *, request_observer=None):
            self.messages.append(list(messages))
            if not any(m["role"] == "tool" for m in messages):
                return ModelResponse(kind="tool_call", tool="browser.open",
                                     arguments={"url": URL})
            if not any(m.get("tool") == "browser.submit" for m in messages):
                # The owner watches the open receipt and approves before the
                # submit step is dispatched — through the production service.
                open_receipt = next(m for m in messages if m.get("tool") == "browser.open")
                session_id = open_receipt["result"]["session_id"]
                granted["approval"] = service.grant(
                    OWNER, "run-submit-real", body={
                        "session_id": session_id, "selector": "#checkout-go",
                    })
                return ModelResponse(
                    kind="tool_call", tool="browser.submit",
                    arguments={"session_id": session_id,
                               "selector": "#checkout-go"})
            return ModelResponse(kind="final", text="submit-done")

    provider = OwnerGrantsOnOpen()
    worker, _node, journal, _transport = _browser_run_worker(
        app, repo, credential, tmp_path, driver_factory=lambda: driver,
        provider=provider, submit=True,
    )
    # The bootstrap-wired production service stays in place (no override).
    assert worker.submit_approvals is service

    result = worker.run_once(OWNER)

    assert result["state"] == "succeeded"
    assert driver.submits == ["#checkout-go"]
    approval_id = granted["approval"]["approval"]["approval_id"]
    delivery = app.extensions["fleet"]["services"]["platform_delivery"]
    command_row = delivery.repository.get("run-submit-real:step:2")
    # The broker consumed through the service and bound the same approval.
    assert command_row["arguments"]["approval_id"] == approval_id
    browser_repo = app.extensions["fleet"]["repositories"]["browser"]
    stored = browser_repo.get_submit_approval(OWNER, approval_id)
    assert stored["state"] == "consumed"
    assert stored["consumed_command_id"] == "run-submit-real:step:2"
    assert stored["selector"] == "#checkout-go"
    # The node journal carries the same opaque approval id end to end.
    journal_row = journal.get("run-submit-real:step:2")
    assert journal_row["result"]["result"]["approval_id"] == approval_id
    # Run terminalization expires nothing that was already consumed.
    assert service.expire_run_submit_approvals(OWNER, "run-submit-real") == 0





def test_browser_upload_reads_a_bounded_stream(tmp_path, monkeypatch):
    app = _app(tmp_path, browser=True)
    repo = app.extensions["fleet"]["platform_repository"]
    delivery = app.extensions["fleet"]["services"]["platform_delivery"]
    repo.upsert_node(OWNER, {"node_id": "node-upload", "label": "Browser"})
    credential = repo.provision_node_credential(
        OWNER, "node-upload", secret="u" * 40)["credential"]
    command = PlatformCommand.create(
        command_id="run-upload:step:1", target_node="node-upload", owner_id=OWNER,
        action="tool.browser.screenshot", resource_id="workspace-upload",
        arguments={"session_id": "session-opaque"}, retry_class="manual_only",
        expires_at=9_999_999_999, run_id="run-upload",
    )
    delivery.enqueue(command, idempotency_key=command.command_id)
    client = app.test_client()
    headers = {"X-Platform-Node-Credential": credential}
    assert client.post(
        "/api/platform/v1/nodes/poll", json={"worker_id": "upload-worker"},
        headers=headers,
    ).status_code == 200
    ticket_response = client.post(
        "/api/platform/v1/nodes/browser-artifact-tickets", json={
            "command_id": command.command_id, "worker_id": "upload-worker",
            "idempotency_key": command.command_id + ":screenshot",
        }, headers=headers,
    )
    assert ticket_response.status_code == 200
    ticket = ticket_response.get_json()["ticket"]

    def fail_get_data(self, *args, **kwargs):
        raise AssertionError("upload route must use a bounded request stream")

    monkeypatch.setattr(Request, "get_data", fail_get_data)
    uploaded = client.post(
        "/api/platform/v1/nodes/browser-artifact-tickets/"
        + ticket["ticket_id"] + "/content",
        data=PNG,
        headers={
            **headers,
            "Content-Type": "image/png",
            "X-Platform-Artifact-Upload-Token": ticket["upload_token"],
            "X-Platform-Command-ID": command.command_id,
            "X-Platform-Worker-ID": "upload-worker",
            "X-Platform-Artifact-Idempotency-Key": command.command_id + ":screenshot",
        },
    )
    assert uploaded.status_code == 200


def test_browser_upload_rejects_wrong_credential_and_invalid_png(tmp_path):
    app = _app(tmp_path, browser=True)
    repo = app.extensions["fleet"]["platform_repository"]
    delivery = app.extensions["fleet"]["services"]["platform_delivery"]
    repo.upsert_node(OWNER, {"node_id": "node-negative", "label": "Browser"})
    credential = repo.provision_node_credential(
        OWNER, "node-negative", secret="v" * 40)["credential"]
    command = PlatformCommand.create(
        command_id="run-negative:step:1", target_node="node-negative", owner_id=OWNER,
        action="tool.browser.screenshot", resource_id="workspace-negative",
        arguments={"session_id": "session-opaque"}, retry_class="manual_only",
        expires_at=9_999_999_999, run_id="run-negative",
    )
    delivery.enqueue(command, idempotency_key=command.command_id)
    client = app.test_client()
    headers = {"X-Platform-Node-Credential": credential}
    assert client.post(
        "/api/platform/v1/nodes/poll", json={"worker_id": "negative-worker"},
        headers=headers,
    ).status_code == 200
    ticket_response = client.post(
        "/api/platform/v1/nodes/browser-artifact-tickets", json={
            "command_id": command.command_id, "worker_id": "negative-worker",
            "idempotency_key": command.command_id + ":screenshot",
        }, headers=headers,
    )
    ticket = ticket_response.get_json()["ticket"]
    upload_path = "/api/platform/v1/nodes/browser-artifact-tickets/" + ticket["ticket_id"] + "/content"
    upload_headers = {
        "Content-Type": "image/png",
        "X-Platform-Artifact-Upload-Token": ticket["upload_token"],
        "X-Platform-Command-ID": command.command_id,
        "X-Platform-Worker-ID": "negative-worker",
        "X-Platform-Artifact-Idempotency-Key": command.command_id + ":screenshot",
    }
    wrong_credential = client.post(
        upload_path, data=PNG,
        headers={**upload_headers, "X-Platform-Node-Credential": "node-negative:" + "w" * 40},
    )
    assert wrong_credential.status_code == 403
    invalid_png = client.post(
        upload_path, data=b"not-png",
        headers={**upload_headers, "X-Platform-Node-Credential": credential},
    )
    assert invalid_png.status_code == 415
    wrong_mime = client.post(
        upload_path, data=PNG,
        headers={**upload_headers, "Content-Type": "application/octet-stream",
                "X-Platform-Node-Credential": credential},
    )
    assert wrong_mime.status_code == 415


def test_browser_upload_rejects_oversized_body_before_artifact_write(tmp_path):
    app = _app(tmp_path, browser=True)
    repo = app.extensions["fleet"]["platform_repository"]
    delivery = app.extensions["fleet"]["services"]["platform_delivery"]
    repo.upsert_node(OWNER, {"node_id": "node-large", "label": "Browser"})
    credential = repo.provision_node_credential(
        OWNER, "node-large", secret="l" * 40)["credential"]
    command = PlatformCommand.create(
        command_id="run-large:step:1", target_node="node-large", owner_id=OWNER,
        action="tool.browser.screenshot", resource_id="workspace-large",
        arguments={"session_id": "session-opaque"}, retry_class="manual_only",
        expires_at=9_999_999_999, run_id="run-large",
    )
    delivery.enqueue(command, idempotency_key=command.command_id)
    client = app.test_client()
    headers = {"X-Platform-Node-Credential": credential}
    assert client.post(
        "/api/platform/v1/nodes/poll", json={"worker_id": "large-worker"},
        headers=headers,
    ).status_code == 200
    ticket = client.post(
        "/api/platform/v1/nodes/browser-artifact-tickets", json={
            "command_id": command.command_id, "worker_id": "large-worker",
            "idempotency_key": command.command_id + ":screenshot",
        }, headers=headers,
    ).get_json()["ticket"]
    response = client.post(
        "/api/platform/v1/nodes/browser-artifact-tickets/"
        + ticket["ticket_id"] + "/content",
        data=b"\x89PNG\r\n\x1a\n" + b"x" * (256 * 1024),
        headers={
            **headers,
            "Content-Type": "image/png",
            "X-Platform-Artifact-Upload-Token": ticket["upload_token"],
            "X-Platform-Command-ID": command.command_id,
            "X-Platform-Worker-ID": "upload-worker",
            "X-Platform-Artifact-Idempotency-Key": command.command_id + ":screenshot",
        },
    )
    assert response.status_code == 413
    assert response.get_json()["error"] == "artifact_too_large"
    assert app.extensions["fleet"]["services"]["platform_artifacts"].list(
        OWNER, "workspace-large") == []


def test_browser_artifact_ticket_rejects_wrong_command_action_worker_and_lease(tmp_path):
    app = _app(tmp_path, browser=True)
    repo = app.extensions["fleet"]["platform_repository"]
    delivery = app.extensions["fleet"]["services"]["platform_delivery"]
    repo.upsert_node(OWNER, {"node_id": "node-ticket", "label": "Browser"})
    credential = repo.provision_node_credential(
        OWNER, "node-ticket", secret="t" * 40)["credential"]
    screenshot = PlatformCommand.create(
        command_id="run-ticket:step:1", target_node="node-ticket", owner_id=OWNER,
        action="tool.browser.screenshot", resource_id="workspace-ticket",
        arguments={"session_id": "session-opaque"}, retry_class="manual_only",
        expires_at=9_999_999_999, run_id="run-ticket",
    )
    wrong_action = PlatformCommand.create(
        command_id="run-ticket:step:2", target_node="node-ticket", owner_id=OWNER,
        action="tool.browser.snapshot", resource_id="workspace-ticket",
        arguments={"session_id": "session-opaque"}, retry_class="manual_only",
        expires_at=9_999_999_999, run_id="run-ticket",
    )
    delivery.enqueue(screenshot, idempotency_key=screenshot.command_id)
    delivery.enqueue(wrong_action, idempotency_key=wrong_action.command_id)
    client = app.test_client()
    headers = {"X-Platform-Node-Credential": credential}
    assert client.post(
        "/api/platform/v1/nodes/poll", json={"worker_id": "ticket-worker"},
        headers=headers,
    ).status_code == 200
    assert client.post(
        "/api/platform/v1/nodes/poll", json={"worker_id": "other-worker"},
        headers=headers,
    ).status_code == 200

    def issue(command_id, worker_id):
        return client.post(
            "/api/platform/v1/nodes/browser-artifact-tickets", json={
                "command_id": command_id, "worker_id": worker_id,
                "idempotency_key": command_id + ":screenshot",
            }, headers=headers,
        )

    assert issue("missing-command", "ticket-worker").status_code == 404
    assert issue(wrong_action.command_id, "other-worker").status_code == 400
    assert issue(screenshot.command_id, "other-worker").status_code == 409
    assert issue(wrong_action.command_id, "ticket-worker").status_code == 400

    commands = delivery.repository
    conn = commands._connect()
    try:
        conn.execute(
            "UPDATE platform_commands SET lease_until=0 WHERE command_id=?",
            (screenshot.command_id,),
        )
    finally:
        conn.close()
    expired_lease = issue(screenshot.command_id, "ticket-worker")
    assert expired_lease.status_code == 409
    assert expired_lease.get_json()["error"] == "artifact_lease_expired"

    second = PlatformCommand.create(
        command_id="run-ticket:step:3", target_node="node-ticket", owner_id=OWNER,
        action="tool.browser.screenshot", resource_id="workspace-ticket",
        arguments={"session_id": "session-opaque"}, retry_class="manual_only",
        expires_at=9_999_999_999, run_id="run-ticket",
    )
    delivery.enqueue(second, idempotency_key=second.command_id)
    client.post("/api/platform/v1/nodes/poll", json={"worker_id": "ticket-worker"}, headers=headers)
    connection = commands._connect()
    try:
        connection.execute(
            "UPDATE platform_commands SET expires_at=0 WHERE command_id=?",
            (second.command_id,),
        )
    finally:
        connection.close()
    expired_command = issue(second.command_id, "ticket-worker")
    assert expired_command.status_code == 409
    assert expired_command.get_json()["error"] == "artifact_command_expired"


def test_browser_artifact_upload_duplicate_and_concurrent_replay(tmp_path):
    app = _app(tmp_path, browser=True)
    repo = app.extensions["fleet"]["platform_repository"]
    delivery = app.extensions["fleet"]["services"]["platform_delivery"]
    repo.upsert_node(OWNER, {"node_id": "node-replay", "label": "Browser"})
    credential = repo.provision_node_credential(
        OWNER, "node-replay", secret="r" * 40)["credential"]
    command = PlatformCommand.create(
        command_id="run-replay:step:1", target_node="node-replay", owner_id=OWNER,
        action="tool.browser.screenshot", resource_id="workspace-replay",
        arguments={"session_id": "session-opaque"}, retry_class="manual_only",
        expires_at=9_999_999_999, run_id="run-replay",
    )
    delivery.enqueue(command, idempotency_key=command.command_id)
    client = app.test_client()
    headers = {"X-Platform-Node-Credential": credential}
    assert client.post(
        "/api/platform/v1/nodes/poll", json={"worker_id": "replay-worker"},
        headers=headers,
    ).status_code == 200
    ticket = client.post(
        "/api/platform/v1/nodes/browser-artifact-tickets", json={
            "command_id": command.command_id, "worker_id": "replay-worker",
            "idempotency_key": command.command_id + ":screenshot",
        }, headers=headers,
    ).get_json()["ticket"]
    upload_path = "/api/platform/v1/nodes/browser-artifact-tickets/" + ticket["ticket_id"] + "/content"
    upload_headers = {
        **headers, "Content-Type": "image/png",
        "X-Platform-Artifact-Upload-Token": ticket["upload_token"],
        "X-Platform-Command-ID": command.command_id,
        "X-Platform-Worker-ID": "replay-worker",
        "X-Platform-Artifact-Idempotency-Key": command.command_id + ":screenshot",
    }
    repository = app.extensions["fleet"]["repositories"]["browser"]
    repository.begin_artifact_upload(
        ticket["ticket_id"], ticket["upload_token"], node_id="node-replay",
        command_id=command.command_id,
        idempotency_key=command.command_id + ":screenshot",
    )
    concurrent = client.post(upload_path, data=PNG, headers=upload_headers)
    assert concurrent.status_code == 409
    assert concurrent.get_json()["error"] == "artifact_upload_in_progress"
    repository.reset_artifact_upload(ticket["ticket_id"])
    wrong_command = client.post(
        upload_path, data=PNG,
        headers={**upload_headers, "X-Platform-Command-ID": "other-command"},
    )
    assert wrong_command.status_code == 404
    assert wrong_command.get_json()["error"] == "command_not_found"

    uploaded = client.post(upload_path, data=PNG, headers=upload_headers)
    assert uploaded.status_code == 200
    consumed = client.post(
        "/api/platform/v1/nodes/browser-artifact-tickets", json={
            "command_id": command.command_id, "worker_id": "replay-worker",
            "idempotency_key": command.command_id + ":screenshot",
        }, headers=headers,
    ).get_json()["ticket"]
    assert consumed["state"] == "consumed"
    assert "upload_token" not in consumed
    receipt = client.post(
        "/api/platform/v1/nodes/receipts", json={
            "command_id": command.command_id, "status": "succeeded",
            "worker_id": "replay-worker", "result": {},
        }, headers=headers,
    )
    assert receipt.status_code == 200
    replayed = client.post(upload_path, data=PNG, headers=upload_headers)
    assert replayed.status_code == 200
    assert replayed.get_json()["artifact"]["artifact_id"] == uploaded.get_json()["artifact"]["artifact_id"]

    expiring_command = PlatformCommand.create(
        command_id="run-replay:step:2", target_node="node-replay", owner_id=OWNER,
        action="tool.browser.screenshot", resource_id="workspace-replay",
        arguments={"session_id": "session-opaque"}, retry_class="manual_only",
        expires_at=9_999_999_999, run_id="run-replay",
    )
    delivery.enqueue(expiring_command, idempotency_key=expiring_command.command_id)
    client.post(
        "/api/platform/v1/nodes/poll", json={"worker_id": "replay-worker"},
        headers=headers,
    )
    expiring_ticket = client.post(
        "/api/platform/v1/nodes/browser-artifact-tickets", json={
            "command_id": expiring_command.command_id, "worker_id": "replay-worker",
            "idempotency_key": expiring_command.command_id + ":screenshot",
        }, headers=headers,
    ).get_json()["ticket"]
    browser_repo = app.extensions["fleet"]["repositories"]["browser"]
    conn = browser_repo._connect()
    try:
        conn.execute(
            "UPDATE browser_artifact_tickets SET expires_at=0 WHERE ticket_id=?",
            (expiring_ticket["ticket_id"],),
        )
    finally:
        conn.close()
    expired = client.post(
        "/api/platform/v1/nodes/browser-artifact-tickets/"
        + expiring_ticket["ticket_id"] + "/content", data=PNG,
        headers={
            **headers, "Content-Type": "image/png",
            "X-Platform-Artifact-Upload-Token": expiring_ticket["upload_token"],
            "X-Platform-Command-ID": expiring_command.command_id,
            "X-Platform-Worker-ID": "replay-worker",
            "X-Platform-Artifact-Idempotency-Key": expiring_command.command_id + ":screenshot",
        },
    )
    assert expired.status_code == 409
    assert expired.get_json()["error"] == "artifact_ticket_expired"


def test_browser_upload_rejects_late_worker_after_lease_expiry(tmp_path):
    app = _app(tmp_path, browser=True)
    repo = app.extensions["fleet"]["platform_repository"]
    delivery = app.extensions["fleet"]["services"]["platform_delivery"]
    repo.upsert_node(OWNER, {"node_id": "node-late", "label": "Browser"})
    credential = repo.provision_node_credential(
        OWNER, "node-late", secret="z" * 40)["credential"]
    command = PlatformCommand.create(
        command_id="run-late:step:1", target_node="node-late", owner_id=OWNER,
        action="tool.browser.screenshot", resource_id="workspace-late",
        arguments={"session_id": "session-opaque"}, retry_class="manual_only",
        expires_at=9_999_999_999, run_id="run-late",
    )
    delivery.enqueue(command, idempotency_key=command.command_id)
    client = app.test_client()
    headers = {"X-Platform-Node-Credential": credential}
    client.post("/api/platform/v1/nodes/poll", json={"worker_id": "late-worker"}, headers=headers)
    ticket = client.post(
        "/api/platform/v1/nodes/browser-artifact-tickets", json={
            "command_id": command.command_id, "worker_id": "late-worker",
            "idempotency_key": command.command_id + ":screenshot",
        }, headers=headers,
    ).get_json()["ticket"]
    conn = delivery.repository._connect()
    try:
        conn.execute(
            "UPDATE platform_commands SET lease_until=0 WHERE command_id=?",
            (command.command_id,),
        )
    finally:
        conn.close()
    response = client.post(
        "/api/platform/v1/nodes/browser-artifact-tickets/"
        + ticket["ticket_id"] + "/content", data=PNG,
        headers={
            **headers, "Content-Type": "image/png",
            "X-Platform-Artifact-Upload-Token": ticket["upload_token"],
            "X-Platform-Command-ID": command.command_id,
            "X-Platform-Worker-ID": "late-worker",
            "X-Platform-Artifact-Idempotency-Key": command.command_id + ":screenshot",
        },
    )
    assert response.status_code == 409
    assert response.get_json()["error"] == "artifact_lease_expired"
    assert app.extensions["fleet"]["services"]["platform_artifacts"].list(
        OWNER, "workspace-late") == []


def test_browser_upload_hash_mismatch_resets_ticket_and_deletes_artifact(tmp_path, monkeypatch):
    app = _app(tmp_path, browser=True)
    repo = app.extensions["fleet"]["platform_repository"]
    delivery = app.extensions["fleet"]["services"]["platform_delivery"]
    repo.upsert_node(OWNER, {"node_id": "node-cleanup", "label": "Browser"})
    credential = repo.provision_node_credential(
        OWNER, "node-cleanup", secret="c" * 40)["credential"]
    command = PlatformCommand.create(
        command_id="run-cleanup:step:1", target_node="node-cleanup", owner_id=OWNER,
        action="tool.browser.screenshot", resource_id="workspace-cleanup",
        arguments={"session_id": "session-opaque"}, retry_class="manual_only",
        expires_at=9_999_999_999, run_id="run-cleanup",
    )
    delivery.enqueue(command, idempotency_key=command.command_id)
    client = app.test_client()
    headers = {"X-Platform-Node-Credential": credential}
    client.post("/api/platform/v1/nodes/poll", json={"worker_id": "cleanup-worker"}, headers=headers)
    ticket = client.post(
        "/api/platform/v1/nodes/browser-artifact-tickets", json={
            "command_id": command.command_id, "worker_id": "cleanup-worker",
            "idempotency_key": command.command_id + ":screenshot",
        }, headers=headers,
    ).get_json()["ticket"]
    store = app.extensions["fleet"]["services"]["platform_artifacts"]
    original_put = store.put_bytes
    written = []

    def mismatched_put(*args, **kwargs):
        artifact = original_put(*args, **kwargs)
        written.append(artifact["artifact_id"])
        return {**artifact, "sha256": "0" * 64}

    monkeypatch.setattr(store, "put_bytes", mismatched_put)
    response = client.post(
        "/api/platform/v1/nodes/browser-artifact-tickets/"
        + ticket["ticket_id"] + "/content", data=PNG,
        headers={
            **headers, "Content-Type": "image/png",
            "X-Platform-Artifact-Upload-Token": ticket["upload_token"],
            "X-Platform-Command-ID": command.command_id,
            "X-Platform-Worker-ID": "cleanup-worker",
            "X-Platform-Artifact-Idempotency-Key": command.command_id + ":screenshot",
        },
    )
    assert response.status_code == 503
    browser_repo = app.extensions["fleet"]["repositories"]["browser"]
    row = browser_repo._connect()
    try:
        state = row.execute(
            "SELECT state FROM browser_artifact_tickets WHERE ticket_id=?",
            (ticket["ticket_id"],),
        ).fetchone()[0]
    finally:
        row.close()
    assert state == "issued"
    assert store.get(OWNER, "workspace-cleanup", written[0]) is None


def test_browser_session_register_and_close_is_command_bound(tmp_path):
    app = _app(tmp_path, browser=True)
    repo = app.extensions["fleet"]["platform_repository"]
    delivery = app.extensions["fleet"]["services"]["platform_delivery"]
    repo.upsert_node(OWNER, {"node_id": "node-session", "label": "Browser"})
    credential = repo.provision_node_credential(
        OWNER, "node-session", secret="s" * 40)["credential"]
    open_command = PlatformCommand.create(
        command_id="run-session:step:1", target_node="node-session", owner_id=OWNER,
        action="tool.browser.open", resource_id="workspace-session",
        arguments={"url": "http://localhost:3000"}, retry_class="manual_only",
        expires_at=9_999_999_999, run_id="run-session",
    )
    delivery.enqueue(open_command, idempotency_key=open_command.command_id)
    client = app.test_client()
    headers = {"X-Platform-Node-Credential": credential}
    polled = client.post(
        "/api/platform/v1/nodes/poll",
        json={"worker_id": "session-worker"}, headers=headers,
    )
    assert polled.status_code == 200
    session_id = "opaque-session-id-1234"
    registered = client.post(
        "/api/platform/v1/nodes/browser-sessions", json={
            "command_id": open_command.command_id, "worker_id": "session-worker",
            "session_id": session_id, "backend": "cdp_local",
        }, headers=headers,
    )
    assert registered.status_code == 200
    session = app.extensions["fleet"]["repositories"]["browser"].get_session(
        OWNER, session_id)
    assert session["workspace_id"] == "workspace-session"
    assert session["run_id"] == "run-session"
    assert session["node_id"] == "node-session"
    assert session["state"] == "open"

    close_command = PlatformCommand.create(
        command_id="run-session:step:2", target_node="node-session", owner_id=OWNER,
        action="tool.browser.close", resource_id="workspace-session",
        arguments={"session_id": session_id}, retry_class="manual_only",
        expires_at=9_999_999_999, run_id="run-session",
    )
    delivery.enqueue(close_command, idempotency_key=close_command.command_id)
    polled = client.post(
        "/api/platform/v1/nodes/poll",
        json={"worker_id": "session-worker"}, headers=headers,
    )
    assert polled.status_code == 200
    closed = client.post(
        "/api/platform/v1/nodes/browser-sessions/" + session_id + "/close",
        json={"command_id": close_command.command_id, "worker_id": "session-worker"},
        headers=headers,
    )
    assert closed.status_code == 200
    assert app.extensions["fleet"]["repositories"]["browser"].get_session(
        OWNER, session_id)["state"] == "closed"

    reopened = client.post(
        "/api/platform/v1/nodes/browser-sessions", json={
            "command_id": open_command.command_id, "worker_id": "session-worker",
            "session_id": session_id, "backend": "cdp_local",
        }, headers=headers,
    )
    assert reopened.status_code == 409
    assert reopened.get_json()["error"] == "browser_session_terminal"


def test_browser_session_registration_rejects_wrong_worker_and_scope(tmp_path):
    app = _app(tmp_path, browser=True)
    repo = app.extensions["fleet"]["platform_repository"]
    delivery = app.extensions["fleet"]["services"]["platform_delivery"]
    repo.upsert_node(OWNER, {"node_id": "node-scope", "label": "Browser"})
    credential = repo.provision_node_credential(
        OWNER, "node-scope", secret="q" * 40)["credential"]
    command = PlatformCommand.create(
        command_id="run-scope:step:1", target_node="node-scope", owner_id=OWNER,
        action="tool.browser.open", resource_id="workspace-scope",
        arguments={"url": "http://localhost:3000"}, retry_class="manual_only",
        expires_at=9_999_999_999, run_id="run-scope",
    )
    delivery.enqueue(command, idempotency_key=command.command_id)
    client = app.test_client()
    headers = {"X-Platform-Node-Credential": credential}
    assert client.post(
        "/api/platform/v1/nodes/poll", json={"worker_id": "scope-worker"},
        headers=headers,
    ).status_code == 200

    body = {"command_id": command.command_id, "worker_id": "other-worker",
            "session_id": "opaque-session-scope-1234", "backend": "cdp_local"}
    wrong_worker = client.post(
        "/api/platform/v1/nodes/browser-sessions", json=body, headers=headers,
    )
    assert wrong_worker.status_code == 409
    assert wrong_worker.get_json()["error"] == "artifact_lease_mismatch"

    body["worker_id"] = "scope-worker"
    registered = client.post(
        "/api/platform/v1/nodes/browser-sessions", json=body, headers=headers,
    )
    assert registered.status_code == 200
    other_run = PlatformCommand.create(
        command_id="run-other:step:1", target_node="node-scope", owner_id=OWNER,
        action="tool.browser.open", resource_id="workspace-scope",
        arguments={"url": "http://localhost:3000"}, retry_class="manual_only",
        expires_at=9_999_999_999, run_id="run-other",
    )
    delivery.enqueue(other_run, idempotency_key=other_run.command_id)
    assert client.post(
        "/api/platform/v1/nodes/poll", json={"worker_id": "scope-worker"},
        headers=headers,
    ).status_code == 200
    conflict = client.post(
        "/api/platform/v1/nodes/browser-sessions", json={
            "command_id": other_run.command_id, "worker_id": "scope-worker",
            "session_id": body["session_id"], "backend": "cdp_local",
        }, headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.get_json()["error"] == "browser_session_scope"
