import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig
from tools.platform.providers.openai_compatible import (
    OpenAICompatibleProvider,
    ProviderError,
)


OWNER = "owner@example.test"
CANARY_KEY = "canary-secret-value"


class _CanaryHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *_args):
        return

    def do_POST(self):  # noqa: N802 - stdlib handler hook
        fixture = self.server.fixture
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            payload = None
        fixture.requests.append({
            "path": self.path,
            "authorization_present": bool(self.headers.get("Authorization")),
            "authorization_correct": self.headers.get("Authorization") == f"Bearer {CANARY_KEY}",
            "payload": payload,
        })
        parts = self.path.rstrip("/").split("/")
        route = parts[-3] if parts[-1] == "completions" and len(parts) >= 5 else (parts[-2] if parts[-1] == "completions" else parts[-1])
        if route == 'tool-contract':
            definitions = {t['function']['name']: t['function'] for t in payload.get('tools', [])}
            write = definitions.get('workspace.write', {})
            schema = write.get('parameters', {})
            valid = set(schema.get('required', [])) == {'path', 'content'} and write.get('description')
            valid = valid and schema.get('properties', {}).get('path', {}).get('type') == 'string'
            if not valid:
                self._send(400, b'{"error":{"code":"invalid_tool_schema"}}'); return
            if payload['messages'][-1]['role'] == 'tool':
                result = json.loads(payload['messages'][-1]['content'])
                if result.get('path') != 'contract.txt':
                    self._send(400, b'{"error":{"code":"invalid_tool_result"}}'); return
                response = {'choices': [{'message': {'content': 'file verified'}, 'finish_reason': 'stop'}]}
            else:
                response = {'choices': [{'message': {'tool_calls': [{'id': 'call-contract', 'type': 'function',
                    'function': {'name': 'workspace.write', 'arguments': json.dumps({'path': 'contract.txt', 'content': 'contract-evidence'})}}]}, 'finish_reason': 'tool_calls'}]}
            self._send(200, json.dumps(response).encode()); return
        if route == "retry":
            fixture.counts[route] = fixture.counts.get(route, 0) + 1
            if fixture.counts[route] == 1:
                self._send(503, b"provider internal detail")
                return
        if route == "timeout":
            time.sleep(1.5)
        if route == "auth":
            self._send(401, b"secret provider detail")
            return
        if route == "rate":
            self._send(429, b"secret provider detail")
            return
        if route == "unavailable":
            self._send(503, b"secret provider detail")
            return
        if route == "stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            for chunk in fixture.sse_events:
                self.wfile.write(chunk)
                self.wfile.flush()
            return
        self._send(200, json.dumps(fixture.response_payload).encode("utf-8"))

    def _send(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _CanaryServer:
    def __init__(self):
        self.requests = []
        self.counts = {}
        self.response_payload = {
            "choices": [{"message": {"role": "assistant", "content": "canary ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        }

        def event(payload):
            return ("data: " + json.dumps(payload, separators=(",", ":")) + "\n\n").encode()

        wire = b"".join([
            event({"choices": [{"delta": {"content": "hel"}}]}),
            event({"choices": [{"delta": {"content": "lo"}}]}),
            event({"choices": [{"delta": {"tool_calls": [{"id": "call-canary", "function": {"name": "workspace.write", "arguments": "{\"path\":\"x\","}}]}}]}),
            event({"choices": [{"delta": {"tool_calls": [{"function": {"arguments": "\"content\":\"y\"}"}}]}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 2, "completion_tokens": 3}}),
            b"data: [DONE]\n\n",
        ])
        self.sse_events = [wire[:17], wire[17:43], wire[43:91], wire[91:]]
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _CanaryHandler)
        self.server.fixture = self
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self):
        host, port = self.server.server_address
        assert host == "127.0.0.1"
        return f"http://{host}:{port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        assert not self.thread.is_alive()


class _Broker:
    def resolve(self, secret_ref):
        assert secret_ref == "env://AGENT_FLEET_CANARY_KEY"
        return CANARY_KEY


def _profile(server, *, route="chat/completions", **config):
    settings = {
        "endpoint": f"{server.base_url}/v1/{route}",
        "timeout_s": 2,
        "max_retries": 0,
    }
    settings.update(config)
    return {
        "provider": "openai_compatible",
        "model": "canary-model",
        "secret_ref": "env://AGENT_FLEET_CANARY_KEY",
        "provider_config": settings,
    }


def test_real_urllib_non_stream_canary_redacts_secret():
    with _CanaryServer() as server:
        provider = OpenAICompatibleProvider.from_profile(
            _profile(server), secret_broker=_Broker(), allow_network=True)
        result = provider.complete(
            [{"role": "user", "content": "hello"}],
            [{"name": "workspace.read"}],
        )

        assert result.text == "canary ok"
        assert result.usage == {"input_tokens": 3, "output_tokens": 2}
        request = server.requests[0]
        assert request["path"] == "/v1/chat/completions"
        assert request["authorization_present"] is True
        assert request["authorization_correct"] is True
        assert request["payload"]["model"] == "canary-model"
        assert request["payload"]["messages"][0]["content"] == "hello"
        assert CANARY_KEY not in repr(result)
        assert CANARY_KEY not in repr(server.requests)


def test_real_urllib_sse_canary_handles_fragmented_tool_call():
    with _CanaryServer() as server:
        provider = OpenAICompatibleProvider.from_profile(
            _profile(server, route="stream/chat/completions", stream=True),
            secret_broker=_Broker(), allow_network=True)
        result = provider.complete([], [])

        assert result.kind == "tool_call"
        assert result.text == "hello"
        assert result.tool == "workspace.write"
        assert result.arguments == {"path": "x", "content": "y"}
        assert result.usage == {"input_tokens": 2, "output_tokens": 3}


@pytest.mark.parametrize(("route", "code", "retryable"), [
    ("auth/chat/completions", "auth_error", False),
    ("rate/chat/completions", "rate_limit", True),
    ("unavailable/chat/completions", "transient_http", True),
])
def test_real_urllib_http_failures_are_bounded(route, code, retryable):
    with _CanaryServer() as server:
        provider = OpenAICompatibleProvider.from_profile(
            _profile(server, route=route), secret_broker=_Broker(), allow_network=True)
        with pytest.raises(ProviderError) as caught:
            provider.complete([], [])

        assert caught.value.code == code
        assert caught.value.retryable is retryable
        assert "secret provider detail" not in str(caught.value)


def test_real_urllib_retry_and_timeout_are_classified():
    with _CanaryServer() as server:
        provider = OpenAICompatibleProvider.from_profile(
            _profile(server, route="retry/chat/completions", max_retries=1),
            secret_broker=_Broker(), allow_network=True, sleeper=lambda _delay: None)
        assert provider.complete([], []).text == "canary ok"
        assert server.counts["retry"] == 2

        timeout_provider = OpenAICompatibleProvider.from_profile(
            _profile(server, route="timeout/chat/completions", timeout_s=1, max_retries=0),
            secret_broker=_Broker(), allow_network=True)
        with pytest.raises(ProviderError) as caught:
            timeout_provider.complete([], [])
        assert caught.value.code == "timeout"
        assert caught.value.retryable is True


def test_worker_canary_uses_provider_gate_and_does_not_leak_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_FLEET_CANARY_KEY", CANARY_KEY)
    with _CanaryServer() as server:
        app = create_app(FleetConfig.from_root(
            tmp_path, ingest_token="x", dev_operator=OWNER,
            platform_enabled=True, platform_worker_enabled=True,
            platform_provider_network_enabled=True,
        ))
        repo = app.extensions["fleet"]["platform_repository"]
        repo.upsert_model(OWNER, {
            "profile_id": "remote", "provider": "openai_compatible",
            "model": "canary-model", "secret_ref": "env://AGENT_FLEET_CANARY_KEY",
            "provider_config": {
                "endpoint": f"{server.base_url}/v1/chat/completions",
                "timeout_s": 2, "max_retries": 0,
            },
        })
        repo.upsert_workspace(OWNER, {
            "workspace_id": "home", "backend": "directory",
            "root_path": str(tmp_path / "workspace"),
        })
        repo.update_defaults(OWNER, {
            "model_profile_id": "remote", "workspace_id": "home",
        }, 0)
        client = app.test_client()
        conversation = client.post("/api/platform/v1/conversations", json={}).get_json()["conversation"]
        run = client.post(
            f"/api/platform/v1/conversations/{conversation['conversation_id']}/turns",
            json={"text": "hello", "client_token": "canary-turn"},
        ).get_json()["run"]
        result = app.extensions["fleet"]["services"]["platform_worker"].run_once(OWNER)

        assert result["state"] == "succeeded"
        assert result["result_text"] == "canary ok"
        stored = repo.get_run(OWNER, run["run_id"])
        events = app.extensions["fleet"]["services"]["run_events"].list(OWNER, run["run_id"])["events"]
        public = json.dumps({"run": stored, "events": events}, ensure_ascii=False)
        assert CANARY_KEY not in public
        assert server.requests[0]["authorization_correct"] is True


def test_real_http_tool_contract_drives_worker_file_and_second_turn(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_FLEET_CANARY_KEY', CANARY_KEY)
    with _CanaryServer() as server:
        app = create_app(FleetConfig.from_root(tmp_path, dev_operator=OWNER,
            platform_enabled=True, platform_worker_enabled=True, platform_provider_network_enabled=True))
        repo = app.extensions['fleet']['platform_repository']
        repo.upsert_model(OWNER, {'profile_id':'contract','provider':'openai_compatible','model':'contract',
            'secret_ref':'env://AGENT_FLEET_CANARY_KEY','provider_config':{'endpoint':server.base_url + '/v1/tool-contract/chat/completions'}})
        repo.upsert_workspace(OWNER, {'workspace_id':'home','root_path':str(tmp_path/'workspace')})
        repo.update_defaults(OWNER, {'model_profile_id':'contract','workspace_id':'home'}, 0)
        client = app.test_client()
        conv = client.post('/api/platform/v1/conversations',json={}).get_json()['conversation']
        client.post('/api/platform/v1/conversations/'+conv['conversation_id']+'/turns', json={'text':'write a file','client_token':'contract'})
        result = app.extensions['fleet']['services']['platform_worker'].run_once(OWNER)
        assert result['state'] == 'succeeded'
        assert result['result_text'] == 'file verified'
        assert (tmp_path/'workspace/contract.txt').read_text() == 'contract-evidence'
        assert len(server.requests) == 2
        assert all(row['authorization_correct'] for row in server.requests)
