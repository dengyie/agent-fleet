import subprocess

import pytest

from tools.platform.services import FixedServiceLogReader, ServiceLogReaderError, fixed_log_argv


def _command(adapter="systemd", alias="api.service", window_s=60, max_bytes=1024):
    return {
        "action": "reconcile.service.logs", "resource_id": "api",
        "arguments": {
            "adapter": adapter, "target_alias": alias, "service_version": 1,
            "action": "logs", "window_s": window_s, "max_bytes": max_bytes,
        },
    }


@pytest.mark.parametrize(
    ("adapter", "expected"),
    [
        ("systemd", ("journalctl", "--no-pager", "--output=short-iso", "--since=-60s", "--unit", "api.service")),
        ("supervisor", ("supervisorctl", "tail", "-100", "api", "stdout")),
        ("docker", ("docker", "logs", "--since", "60s", "--tail", "500", "api")),
    ],
)
def test_fixed_log_argv_is_adapter_owned(adapter, expected):
    alias = "api.service" if adapter == "systemd" else "api"
    assert fixed_log_argv(adapter, alias, 60) == expected


def test_reader_uses_shell_false_and_bounds_utf8_output():
    calls = []

    class Result:
        stdout = "日志行" * 1000

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return Result()

    reader = FixedServiceLogReader(runner=runner, clock=lambda: 10.0)
    result = reader(_command())
    assert len(result["text"].encode("utf-8")) <= 1024
    assert result["truncated"] is True
    assert calls[0][0] == list(fixed_log_argv("systemd", "api.service", 60))
    assert calls[0][1]["shell"] is False


def test_reader_rejects_forged_arguments_and_failures():
    forged = _command()
    forged["arguments"]["argv"] = ["cat"]
    with pytest.raises(ServiceLogReaderError) as rejected:
        FixedServiceLogReader(runner=lambda *_args, **_kwargs: None)(forged)
    assert rejected.value.code == "invalid_arguments"
    with pytest.raises(ServiceLogReaderError) as timeout:
        FixedServiceLogReader(
            runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                subprocess.TimeoutExpired(["journalctl"], 1)
            )
        )(_command())
    assert timeout.value.code == "timeout"
    with pytest.raises(ServiceLogReaderError) as private:
        fixed_log_argv("systemd", "bad\nunit", 60)
    assert private.value.code == "invalid_target_alias"
