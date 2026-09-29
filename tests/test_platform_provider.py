import json

import pytest

from tools.platform.providers.openai_compatible import (
    OpenAICompatibleProvider,
    ProviderError,
    ProviderFactory,
    ProviderUnavailable,
    TransportResponse,
)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class Broker:
    def __init__(self, values):
        self.values = values
        self.refs = []

    def resolve(self, secret_ref):
        self.refs.append(secret_ref)
        return self.values.get(secret_ref)


def _response(payload, *, status=200):
    return TransportResponse(status, {"content-type": "application/json"}, json.dumps(payload).encode())


def _profile(**overrides):
    profile = {
        "provider": "openai_compatible",
        "model": "gpt-test",
        "secret_ref": "env://TEST_PROVIDER_KEY",
        "provider_config": {"endpoint": "https://llm.example.test", "timeout_s": 7, "max_retries": 1},
    }
    profile.update(overrides)
    return profile


def test_non_streaming_request_uses_frozen_endpoint_model_and_redacts_key():
    transport = FakeTransport([_response({
        "choices": [{"message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    })])
    broker = Broker({"env://TEST_PROVIDER_KEY": "super-secret"})
    provider = ProviderFactory(secret_broker=broker, transport=transport)(_profile())

    result = provider.complete([{"role": "user", "content": "hi"}], [])

    assert result.kind == "final"
    assert result.text == "hello"
    assert result.usage == {"input_tokens": 3, "output_tokens": 2}
    call = transport.calls[0]
    assert call["url"] == "https://llm.example.test/v1/chat/completions"
    assert json.loads(call["body"])["model"] == "gpt-test"
    assert call["headers"]["Authorization"] == "Bearer super-secret"
    assert call["headers"]["User-Agent"] == "agent-fleet/1.0"
    assert "super-secret" not in repr(result)
    assert broker.refs == ["env://TEST_PROVIDER_KEY"]


def test_structured_tool_call_arguments_are_normalized():
    transport = FakeTransport([_response({
        "choices": [{"message": {"role": "assistant", "tool_calls": [{
            "id": "call-1", "type": "function",
            "function": {"name": "workspace.write", "arguments": '{"path":"x","content":"y"}'},
        }]}, "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 5},
    })])
    result = ProviderFactory(secret_broker=Broker({"env://TEST_PROVIDER_KEY": "x"}), transport=transport)(
        _profile()).complete([], [{"name": "workspace.write"}])
    assert result.kind == "tool_call"
    assert result.tool == "workspace.write"
    assert result.arguments == {"path": "x", "content": "y"}
    assert result.tool_call_id == "call-1"
    assert result.usage == {"input_tokens": 4, "output_tokens": 5}


def test_sse_preserves_text_and_tool_delta_order():
    def event(payload):
        return ("data: " + json.dumps(payload, separators=(",", ":")) + "\n\n").encode()

    events = [
        event({"choices": [{"delta": {"content": "hel"}, "finish_reason": None}]}),
        event({"choices": [{"delta": {"content": "lo"}, "finish_reason": None}]}),
        event({"choices": [{"delta": {"tool_calls": [{"id": "c1", "function": {"name": "workspace.write", "arguments": '{"path":"x",'}}]}, "finish_reason": None}]}),
        event({"choices": [{"delta": {"tool_calls": [{"function": {"arguments": '"content":"y"}'}}]}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 2, "completion_tokens": 3}}),
        b"data: [DONE]\n\n",
    ]
    transport = FakeTransport([TransportResponse(200, {"content-type": "text/event-stream"}, events)])
    profile = _profile(provider_config={"endpoint": "https://llm.example.test", "stream": True})
    result = ProviderFactory(secret_broker=Broker({"env://TEST_PROVIDER_KEY": "x"}), transport=transport)(profile).complete([], [])
    # A stream may contain narration before a tool call; the adapter keeps both
    # in order and exposes the structured call to the finite runtime.
    assert result.kind == "tool_call"
    assert result.text == "hello"
    assert result.tool == "workspace.write"
    assert result.arguments == {"path": "x", "content": "y"}
    assert result.usage == {"input_tokens": 2, "output_tokens": 3}


@pytest.mark.parametrize(("status", "code", "retryable"), [
    (401, "auth_error", False),
    (403, "auth_error", False),
    (429, "rate_limit", True),
    (500, "transient_http", True),
    (400, "request_rejected", False),
])
def test_http_failure_classes_are_stable(status, code, retryable):
    transport = FakeTransport([TransportResponse(status, {}, b"secret provider detail")])
    provider = ProviderFactory(secret_broker=Broker({"env://TEST_PROVIDER_KEY": "x"}), transport=transport)(
        _profile(provider_config={"endpoint": "https://llm.example.test", "max_retries": 0}))
    with pytest.raises(ProviderError) as caught:
        provider.complete([], [])
    assert caught.value.code == code
    assert caught.value.retryable is retryable
    assert "secret provider detail" not in str(caught.value)


def test_retry_only_retries_transient_response_and_then_succeeds():
    transport = FakeTransport([
        TransportResponse(503, {}, b""),
        _response({"choices": [{"message": {"content": "ok"}}]}),
    ])
    sleeps = []
    profile = _profile(provider_config={"endpoint": "https://llm.example.test", "max_retries": 1})
    provider = OpenAICompatibleProvider.from_profile(
        profile, secret_broker=Broker({"env://TEST_PROVIDER_KEY": "x"}),
        transport=transport, sleeper=sleeps.append)
    assert provider.complete([], []).text == "ok"
    assert len(transport.calls) == 2
    assert sleeps


def test_timeout_is_retryable_but_missing_secret_endpoint_fails_closed():
    transport = FakeTransport([TimeoutError()])
    provider = OpenAICompatibleProvider.from_profile(
        _profile(provider_config={"endpoint": "https://llm.example.test", "max_retries": 0}),
        secret_broker=Broker({"env://TEST_PROVIDER_KEY": "x"}), transport=transport)
    with pytest.raises(ProviderError) as caught:
        provider.complete([], [])
    assert caught.value.code == "timeout"
    with pytest.raises(ProviderUnavailable) as missing:
        ProviderFactory(secret_broker=Broker({}), transport=transport)(_profile())
    assert missing.value.code == "provider_secret_unavailable"


def test_factory_does_not_treat_compatible_as_a_real_provider():
    with pytest.raises(ProviderUnavailable):
        ProviderFactory()(dict(_profile(), provider="compatible"))


def test_real_network_transport_is_disabled_by_default():
    provider = ProviderFactory(secret_broker=Broker({"env://TEST_PROVIDER_KEY": "x"}))(_profile())
    with pytest.raises(ProviderUnavailable) as caught:
        provider.complete([], [])
    assert caught.value.code == "provider_network_disabled"


def test_endpoint_rejects_embedded_query_credentials():
    with pytest.raises(ProviderUnavailable) as caught:
        ProviderFactory(secret_broker=Broker({"env://TEST_PROVIDER_KEY": "x"}), transport=FakeTransport([]))(
            _profile(provider_config={"endpoint": "https://llm.example.test/v1?api_key=secret"}))
    assert caught.value.code == "provider_endpoint_invalid"


def test_workspace_tool_contract_reaches_real_provider_request():
    from tools.platform.tool_broker import ToolBroker
    transport = FakeTransport([_response({
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
    })])
    provider = ProviderFactory(secret_broker=Broker({"env://TEST_PROVIDER_KEY": "secret"}), transport=transport)(_profile())
    provider.complete([{"role": "user", "content": "write a file"}], ToolBroker.tool_definitions())
    definitions = {row["function"]["name"]: row["function"] for row in json.loads(transport.calls[0]["body"])["tools"]}
    write = definitions["workspace.write"]["parameters"]
    assert set(write["required"]) == {"path", "content"}
    assert write["properties"]["path"]["type"] == "string"
    assert write["properties"]["content"]["type"] == "string"
    assert definitions["workspace.exec"]["parameters"]["properties"]["argv"]["type"] == "array"
    assert all(tool["description"] for tool in definitions.values())
