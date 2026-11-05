"""Review fix #9: parallel tool_calls must not be concatenated into one call.

The SSE parser previously appended every delta's ``tool_calls[0]`` name and
arguments into one buffer, so two parallel calls (index 0 and 1) became e.g.
``readwrite`` with ``{"a":1}{"b":2}``.  Requests now also send
``parallel_tool_calls: false`` whenever tools are offered.
"""
import json

from tools.platform.providers.openai_compatible import (OpenAICompatibleProvider,
                                                        TransportResponse)

TOOLS = [{"name": "fs.read", "description": "r", "parameters": {"type": "object"}},
         {"name": "fs.write", "description": "w", "parameters": {"type": "object"}}]


class _Transport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def _provider(transport, **kwargs):
    return OpenAICompatibleProvider(model="m", endpoint="http://fixture/v1/chat/completions",
                                    api_key="fixture-key", transport=transport, **kwargs)


def _sse(*chunks):
    text = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
    return TransportResponse(200, {}, text.encode())


def _delta(calls, finish=None):
    return {"choices": [{"delta": {"tool_calls": calls}, "finish_reason": finish}]}


def test_streamed_parallel_tool_calls_are_not_concatenated():
    body = _sse(
        _delta([{"index": 0, "id": "call_a", "function": {"name": "fs_read", "arguments": ""}}]),
        _delta([{"index": 0, "function": {"arguments": '{"path":'}}]),
        _delta([{"index": 1, "id": "call_b", "function": {"name": "fs_write", "arguments": '{"p'}}]),
        _delta([{"index": 0, "function": {"arguments": '"a.txt"}'}}]),
        _delta([{"index": 1, "function": {"arguments": 'ath":"b"}'}}], finish="tool_calls"),
    )
    transport = _Transport(body)
    result = _provider(transport, stream=True).complete([], TOOLS)
    assert result.kind == "tool_call"
    assert result.tool == "fs.read"
    assert result.arguments == {"path": "a.txt"}
    assert result.tool_call_id == "call_a"


def test_streamed_single_call_without_index_still_accumulates():
    body = _sse(
        _delta([{"id": "call_x", "function": {"name": "fs_", "arguments": '{"pa'}}]),
        _delta([{"function": {"name": "write", "arguments": 'th":"z"}'}}], finish="tool_calls"),
    )
    result = _provider(_Transport(body), stream=True).complete([], TOOLS)
    assert (result.tool, result.arguments, result.tool_call_id) == ("fs.write", {"path": "z"}, "call_x")


def test_request_disables_parallel_tool_calls_only_with_tools():
    reply = TransportResponse(200, {}, json.dumps({"choices": [
        {"message": {"content": "ok"}, "finish_reason": "stop"}]}).encode())
    transport = _Transport(reply)
    _provider(transport).complete([], TOOLS)
    assert json.loads(transport.calls[0]["body"])["parallel_tool_calls"] is False
    transport = _Transport(reply)
    _provider(transport).complete([], [])
    assert "parallel_tool_calls" not in json.loads(transport.calls[0]["body"])
