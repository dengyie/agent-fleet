import subprocess
from urllib.error import HTTPError

import pytest

from tools.platform.services import (
    CollectorError,
    DockerCollector,
    HttpCollector,
    SupervisorCollector,
    SystemdCollector,
)


class _Result:
    def __init__(self, code=0, stdout="active", stderr=""):
        self.returncode = code
        self.stdout = stdout
        self.stderr = stderr


def _runner(calls, result=None, error=None):
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if error:
            raise error
        return result or _Result()
    return run


@pytest.mark.parametrize(
    ("collector", "expected"),
    [
        (SystemdCollector(), ["systemctl", "is-active", "--quiet", "api.service"]),
        (SupervisorCollector(), ["supervisorctl", "status", "api"]),
        (DockerCollector(), ["docker", "inspect", "--format", "{{.State.Status}}", "api"]),
    ],
)
def test_fixed_collectors_only_use_adapter_owned_argv(collector, expected):
    calls = []
    evidence = collector.collect("svc-api", "api.service" if isinstance(collector, SystemdCollector) else "api", runner=_runner(calls))
    assert calls[0][0] == expected
    assert calls[0][1]["shell"] is not True
    assert evidence["state"] == "healthy"
    assert evidence["dimension"] == "process_state"


def test_fixed_collectors_distinguish_unhealthy_unsupported_and_timeout():
    calls = []
    failed = SystemdCollector().collect("svc", "api", runner=_runner(calls, result=_Result(code=3, stdout="inactive")))
    assert failed["state"] == "unhealthy"
    unsupported = SystemdCollector().collect("svc", "api", runner=_runner([], error=FileNotFoundError()))
    assert unsupported["state"] == "unsupported"
    timeout = SystemdCollector().collect("svc", "api", runner=_runner([], error=subprocess.TimeoutExpired(["systemctl"], 1)))
    assert timeout["state"] == "unknown"
    assert timeout["detail"]["collector_status"] == "timeout"


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size):
        return b"ok"


def test_http_collector_requires_registered_public_endpoint_and_bounds_response():
    calls = []
    def resolve(host, port, **kwargs):
        ip = "127.0.0.1" if host == "127.0.0.1" else "93.184.216.34"
        return [(None, None, None, None, (ip, port))]

    collector = HttpCollector(
        {"public": "https://example.test/health", "loopback": "http://127.0.0.1:8000/health"},
        opener=lambda request, timeout: calls.append((request.full_url, timeout)) or _Response(),
        resolve=resolve,
    )
    evidence = collector.collect("svc", "public")
    assert evidence["state"] == "healthy"
    assert calls[0][0] == "https://example.test/health"
    with pytest.raises(CollectorError) as denied:
        collector.collect("svc", "loopback")
    assert denied.value.code == "endpoint_address_denied"
    with pytest.raises(CollectorError) as missing:
        collector.collect("svc", "missing")
    assert missing.value.code == "endpoint_unregistered"


def test_http_collector_maps_http_failure_without_returning_body():
    class FailedResponse(_Response):
        status = 503

        def read(self, size):
            return b"secret internal stack trace" * 2000

    collector = HttpCollector(
        {"public": "https://example.test/health"},
        opener=lambda request, timeout: FailedResponse(),
        resolve=lambda host, port, **kwargs: [(None, None, None, None, ("93.184.216.34", port))],
    )
    evidence = collector.collect("svc", "public")
    assert evidence["state"] == "unhealthy"
    assert evidence["detail"]["status_code"] == 503
    assert evidence["detail"]["body_bytes"] == 16 * 1024
    assert "secret internal" not in str(evidence)


def test_http_collector_rejects_redirects_and_private_addresses():
    class RedirectResponse(_Response):
        status = 302

    collector = HttpCollector(
        {"public": "https://example.test/health"},
        opener=lambda request, timeout: RedirectResponse(),
        resolve=lambda host, port, **kwargs: [(None, None, None, None, ("93.184.216.34", port))],
    )
    with pytest.raises(CollectorError) as redirect:
        collector.collect("svc", "public")
    assert redirect.value.code == "redirect_denied"
    with pytest.raises(CollectorError) as denied:
        collector = HttpCollector(
            {"private": "http://10.0.0.5/health"},
            opener=lambda request, timeout: RedirectResponse(),
            resolve=lambda host, port, **kwargs: [(None, None, None, None, ("10.0.0.5", port))],
        )
        collector.collect("svc", "private")
    assert denied.value.code == "endpoint_address_denied"


def test_http_collector_maps_real_http_error_status_to_unhealthy():
    collector = HttpCollector(
        {"public": "https://example.test/health"},
        opener=lambda request, timeout: (_ for _ in ()).throw(
            HTTPError(request.full_url, 503, "unavailable", {}, None)
        ),
        resolve=lambda host, port, **kwargs: [(None, None, None, None, ("93.184.216.34", port))],
    )
    evidence = collector.collect("svc", "public")
    assert evidence["state"] == "unhealthy"
    assert evidence["detail"] == {
        "alias": "public", "status_code": 503,
        "body_bytes": 0, "truncated": False,
    }
