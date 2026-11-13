"""An original attempt's absolute deadline bounds every transport retry."""
import time

import pytest

from tools.transport import Transport


@pytest.mark.parametrize("method,body", [("post_json", {"a": 1}), ("post_bytes", b"source")])
def test_expired_or_cancelled_request_never_opens_a_connection(monkeypatch, method, body):
    transport = Transport()
    monkeypatch.setattr(transport, "_open", lambda *args: pytest.fail("request opened after stop"))
    call = getattr(transport, method)
    assert call("https://hub.example", body, {}, deadline=time.monotonic() - 1)[0] == 0
    assert call("https://hub.example", body, {}, deadline=time.monotonic() + 10, should_cancel=lambda: True)[0] == 0


def test_retries_share_the_original_deadline_and_do_not_replay_after_cancel(monkeypatch):
    transport = Transport(attempts=3, retry_sleep_s=0)
    now = [100.0]
    timeouts = []
    monkeypatch.setattr(time, "monotonic", lambda: now[0])

    def opened(opener, request, timeout):
        timeouts.append(timeout)
        now[0] += 2
        raise TimeoutError("fixture slow upload")

    monkeypatch.setattr(transport, "_open", opened)
    assert transport.post_bytes("https://hub.example", b"source", {}, deadline=103)[0] == 0
    assert timeouts == [3, 1]
    timeouts.clear()
    now[0] = 100
    assert transport.post_json("https://hub.example", {}, {}, deadline=110,
        should_cancel=lambda: bool(timeouts))[0] == 0
    assert timeouts == [10]


@pytest.mark.parametrize("method", ["get_stream", "request_bounded_json"])
@pytest.mark.parametrize("stop", ["cancelled", "expired", "insecure"])
def test_artifact_requests_check_stop_and_url_before_opening(monkeypatch, method, stop):
    transport = Transport()
    monkeypatch.setattr(transport, "_open", lambda *args: pytest.fail("artifact request opened after stop"))
    options = {"deadline": time.monotonic() + (-1 if stop == "expired" else 10),
               "should_cancel": lambda: stop == "cancelled"}
    url = "http://remote.example" if stop == "insecure" else "https://hub.example"
    error = ValueError if stop == "insecure" else OSError
    with pytest.raises(error):
        if method == "get_stream":
            with transport.get_stream(url, {}, **options):
                pytest.fail("stopped stream reached consumer")
        else:
            transport.request_bounded_json("POST", url, b"{}", {}, **options)


def test_bounded_artifact_json_enforces_response_limit_and_closes(monkeypatch):
    import io

    class Response(io.BytesIO):
        status = 200
        headers = {}

    response = Response(b"{" + b"x" * 65536)
    transport = Transport()
    monkeypatch.setattr(transport, "_open", lambda *args: response)
    with pytest.raises(ValueError, match="transport_response_too_large"):
        transport.request_bounded_json("GET", "https://hub.example", None, {},
            deadline=time.monotonic() + 10, should_cancel=lambda: False)
    assert response.closed
