"""Wire-level regressions for the documented precision and safe-cause contract."""
from __future__ import annotations

import json
import logging
from typing import Any
from urllib.error import URLError

import pytest

from hub.diagnostics import log_failure
from tools.platform.providers.openai_compatible import (
    OpenAICompatibleProvider, ProviderError, TransportResponse,
)


class WireTransport:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def request(self, **kwargs: Any) -> TransportResponse:
        return TransportResponse(200, {}, self.body)


def provider(transport: Any, *, stream: bool = False) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(model='fixture', endpoint='http://fixture', api_key='fixture',
                                    transport=transport, stream=stream)


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('amount', ['123456.123456123456', '999999.999999999999', '0.000000000001', '0'])
def test_wire_cost_retains_exact_decimal_digits(stream: bool, amount: str) -> None:
    choice = b'"delta":{"content":"ok"}' if stream else b'"message":{"content":"ok"}'
    body = b'{"choices":[{' + choice + b',"finish_reason":"stop"}],"usage":{"cost":' + amount.encode() + b'}}'
    if stream:
        body = b'data: ' + body + b'\n\ndata: [DONE]\n\n'
    result = provider(WireTransport(body), stream=stream).complete([], [])
    assert result.metadata['cost']['amount'] == amount


@pytest.mark.parametrize('amount', ['1000000.000000000001', '999999.0000000000001'])
def test_wire_cost_limits_apply_before_any_rounding(amount: str) -> None:
    body = b'{"choices":[{"message":{"content":"ok"}}],"usage":{"cost":' + amount.encode() + b'}}'
    assert provider(WireTransport(body)).complete([], []).metadata['cost'] is None


def test_structured_argument_numbers_remain_plain_json_values() -> None:
    body = b'{"choices":[{"message":{"tool_calls":[{"function":{"name":"fixture","arguments":{"threshold":0.5,"nested":[{"ratio":1.25}],"count":2}}}]}}]}'
    result = provider(WireTransport(body)).complete([], [{'name': 'fixture'}])
    assert result.arguments == {'threshold': 0.5, 'nested': [{'ratio': 1.25}], 'count': 2}
    assert type(result.arguments['threshold']) is float
    assert type(result.arguments['count']) is int
    assert json.loads(json.dumps(result.arguments)) == result.arguments


def test_provider_failure_keeps_safe_root_cause(caplog: pytest.LogCaptureFixture) -> None:
    failure = TimeoutError('private-wire-sentinel')
    class BrokenTransport:
        def request(self, **kwargs: Any) -> TransportResponse:
            raise failure
    events: list[dict[str, Any]] = []
    with pytest.raises(ProviderError) as caught:
        provider(BrokenTransport()).complete([], [], request_observer=lambda kind, data: events.append(data))
    assert caught.value.__cause__ is failure
    with caplog.at_level(logging.WARNING):
        log_failure(logging.getLogger('contract-fidelity'), 'provider_failed', caught.value)
    record = json.loads(caplog.records[-1].message)
    assert record['exception_chain'] == ['ProviderError', 'TimeoutError']
    assert any(frame['function'] == 'request' for frame in record['frames'])
    assert 'private-wire-sentinel' not in caplog.text + json.dumps(events) + str(caught.value)


@pytest.mark.parametrize('during_read', [False, True])
@pytest.mark.parametrize('failure', [TimeoutError('private-wire-sentinel'), OSError('private-wire-sentinel'), URLError('private-wire-sentinel')])
def test_urllib_connection_and_body_causes_are_preserved(
    monkeypatch: pytest.MonkeyPatch, during_read: bool, failure: Exception,
) -> None:
    from tools.platform.providers import openai_compatible as adapter
    closed: list[bool] = []
    events: list[dict[str, Any]] = []
    class Accepted:
        status = 200
        headers: dict[str, str] = {}
        def read(self, size: int) -> bytes:
            raise failure
        def close(self) -> None:
            closed.append(True)
    def send(*args: Any, **kwargs: Any) -> Accepted:
        if during_read:
            return Accepted()
        raise failure
    monkeypatch.setattr(adapter, 'urlopen', send)
    p = OpenAICompatibleProvider(model='fixture', endpoint='http://fixture', api_key='fixture', allow_network=True)
    with pytest.raises(ProviderError) as caught:
        p.complete([], [], request_observer=lambda kind, data: events.append(data))
    assert caught.value.__cause__ is failure
    assert closed == ([True] if during_read else [])
    assert events[-1]['http_status'] == (200 if during_read else None)
    assert events[-1]['status'] == 'unknown'
    assert 'private-wire-sentinel' not in json.dumps(events) + str(caught.value)


@pytest.mark.parametrize(('body', 'stream', 'cause'), [
    (b'{"private-wire-sentinel":', False, 'JSONDecodeError'),
    (b'data: \xff\n\n', True, 'UnicodeDecodeError'),
    (b'{"choices":[{"message":{"tool_calls":[{"function":{"name":"fixture","arguments":"private-wire-sentinel"}}]}}]}', False, 'JSONDecodeError'),
    (b'{"usage":{"cost":1e99999999999999999999999}}', False, 'InvalidOperation'),
])
def test_parse_failure_keeps_cause_without_raw_payload(body: bytes, stream: bool, cause: str) -> None:
    events: list[dict[str, Any]] = []
    with pytest.raises(ProviderError) as caught:
        provider(WireTransport(body), stream=stream).complete([], [{'name': 'fixture'}],
            request_observer=lambda kind, data: events.append(data))
    assert type(caught.value.__cause__).__name__ == cause
    assert events[-1]['status'] == 'unknown' and events[-1]['duration_ms'] is not None
    assert 'private-wire-sentinel' not in json.dumps(events) + str(caught.value)


def test_secret_resolution_keeps_cause_without_exposing_value() -> None:
    failure = ValueError('private-wire-sentinel')
    class BrokenBroker:
        def resolve(self, secret_ref: str) -> str | None:
            raise failure
    with pytest.raises(ProviderError) as caught:
        OpenAICompatibleProvider.from_profile({'secret_ref': 'env://FIXTURE'}, secret_broker=BrokenBroker())
    assert caught.value.__cause__ is failure
    assert str(caught.value) == 'provider_secret_unavailable'
