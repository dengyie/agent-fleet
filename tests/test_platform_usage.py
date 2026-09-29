import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.infrastructure.usage_repository import UsageLimitExceeded, UsageRepository
from tools.platform.providers.base import ModelResponse
from tools.platform.runtime.native import NativeAssistantRuntime


class NoopBroker:
    def execute(self, **kwargs):
        raise AssertionError("tool broker should not be called")


class CountingProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        return self.responses.pop(0)


def test_usage_window_rejects_before_provider_and_duplicate_settlement_is_idempotent(tmp_path):
    repo = UsageRepository(tmp_path / "platform.db")
    repo.init()
    repo.set_limit("owner@example.test", "model-a", request_limit=1, token_limit=10, window_s=60)
    reservation = repo.admit("owner@example.test", "model-a", "run-1", 1, 1, now=100.0)
    repo.settle(reservation, {"input_tokens": 4, "output_tokens": 3, "total_tokens": 7}, "succeeded")
    repo.settle(reservation, {"input_tokens": 40, "output_tokens": 30, "total_tokens": 70}, "succeeded")
    with pytest.raises(UsageLimitExceeded):
        repo.admit("owner@example.test", "model-a", "run-2", 1, 1, now=101.0)
    assert repo.get_run_usage("owner@example.test", "run-1")["total_tokens"] == 7


def test_usage_window_expires_and_unknown_request_is_counted(tmp_path):
    repo = UsageRepository(tmp_path / "platform.db")
    repo.init()
    repo.set_limit("owner@example.test", "model-a", request_limit=1, token_limit=0, window_s=10)
    reservation = repo.admit("owner@example.test", "model-a", "run-1", 1, 1, now=100.0)
    repo.settle(reservation, None, "unknown")
    with pytest.raises(UsageLimitExceeded):
        repo.admit("owner@example.test", "model-a", "run-2", 1, 1, now=105.0)
    assert repo.admit("owner@example.test", "model-a", "run-2", 1, 1, now=111.0) is not None
    assert repo.get_run_usage("owner@example.test", "run-1")["provider_requests"] == 1


def test_admission_and_settlement_are_idempotent_for_same_provider_step(tmp_path):
    repo = UsageRepository(tmp_path / "platform.db")
    repo.init()
    repo.set_limit("owner@example.test", "model-a", request_limit=2, token_limit=20, window_s=60)
    first = repo.admit("owner@example.test", "model-a", "run-1", 1, 1, now=100.0)
    duplicate = repo.admit("owner@example.test", "model-a", "run-1", 1, 1, now=101.0)
    assert duplicate.reservation_id == first.reservation_id
    repo.settle(first, {"input_tokens": 1, "output_tokens": 2}, "succeeded")
    repo.settle(duplicate, {"input_tokens": 100, "output_tokens": 100}, "succeeded")
    assert repo.get_run_usage("owner@example.test", "run-1") == {
        "input_tokens": 1, "output_tokens": 2, "total_tokens": 3, "provider_requests": 1,
    }


def test_runtime_denies_before_provider_and_persists_normalized_usage(tmp_path):
    meter = UsageRepository(tmp_path / "platform.db")
    meter.init()
    meter.set_limit("owner@example.test", "model-a", request_limit=1, token_limit=20, window_s=60)
    provider = CountingProvider([ModelResponse(kind="final", text="ok", usage={"input_tokens": 4, "output_tokens": 6})])
    result = NativeAssistantRuntime(provider, NoopBroker(), usage_meter=meter, model_key="model-a", clock=lambda: 101.0).run(
        run_id="run-1", owner_id="owner@example.test", epoch=1, messages=[], tools=[])
    assert result.usage.total_tokens == 10
    assert result.usage.provider_requests == 1
    assert provider.calls == 1


def test_runtime_limit_hit_does_not_call_provider(tmp_path):
    meter = UsageRepository(tmp_path / "platform.db")
    meter.init()
    meter.set_limit("owner@example.test", "model-a", request_limit=1, token_limit=0, window_s=60)
    first = meter.admit("owner@example.test", "model-a", "prior", 1, 1, now=100.0)
    meter.settle(first, None, "unknown")
    provider = CountingProvider([ModelResponse(kind="final", text="must not run")])
    result = NativeAssistantRuntime(provider, NoopBroker(), usage_meter=meter, model_key="model-a", clock=lambda: 101.0).run(
        run_id="run-2", owner_id="owner@example.test", epoch=1, messages=[], tools=[])
    assert result.state == "failed"
    assert provider.calls == 0


def test_platform_usage_meter_is_off_by_default(tmp_path):
    app = create_app(FleetConfig.from_root(tmp_path, platform_enabled=True))
    services = app.extensions["fleet"]["services"]
    assert services.get("usage_repository") is not None
    assert services.get("platform_worker") is None


def test_provider_exception_is_counted_as_unknown_before_worker_rethrows(tmp_path):
    meter = UsageRepository(tmp_path / "platform.db")
    meter.init()

    class BrokenProvider:
        def complete(self, messages, tools):
            raise RuntimeError("transport failed")

    runtime = NativeAssistantRuntime(
        BrokenProvider(), NoopBroker(), usage_meter=meter, model_key="model-a",
        clock=lambda: 100.0,
    )
    with pytest.raises(RuntimeError):
        runtime.run(run_id="run-1", owner_id="owner@example.test", epoch=1, messages=[], tools=[])
    assert meter.get_run_usage("owner@example.test", "run-1")["provider_requests"] == 1


def test_invalid_provider_usage_is_counted_as_unknown_and_releases_reservation(tmp_path):
    meter = UsageRepository(tmp_path / "platform.db")
    meter.init()

    class BrokenUsageProvider:
        def complete(self, messages, tools):
            return ModelResponse(kind="final", text="ok", usage={"input_tokens": "not-a-number"})

    runtime = NativeAssistantRuntime(
        BrokenUsageProvider(), NoopBroker(), usage_meter=meter, model_key="model-a",
        clock=lambda: 100.0,
    )
    with pytest.raises(RuntimeError):
        runtime.run(run_id="run-1", owner_id="owner@example.test", epoch=1, messages=[], tools=[])
    assert meter.get_run_usage("owner@example.test", "run-1")["provider_requests"] == 1


def test_usage_is_preserved_when_tool_execution_fails_after_model_call(tmp_path):
    from hub.application.run_worker_service import LocalRunWorkerService
    from hub.infrastructure.platform_db import PlatformRepository
    from hub.application.run_event_service import RunEventService

    repo = PlatformRepository(tmp_path / "platform.db")
    repo.init()
    repo.upsert_model("owner@example.test", {"profile_id": "model-a", "provider": "deterministic", "model": "a"})
    repo.upsert_workspace("owner@example.test", {"workspace_id": "home", "root_path": str(tmp_path / "workspace")})
    conversation = repo.create_conversation("owner@example.test", "conv-1", title="", workspace_id="home")
    repo.append_turn("owner@example.test", conversation["conversation_id"], "msg-1", "run-1", text="x", client_token="x", config_snapshot={"workspace_id": "home", "model_profile_id": "model-a"}, now=1)
    meter = UsageRepository(tmp_path / "platform.db")
    meter.init()

    class Provider:
        def complete(self, messages, tools):
            return ModelResponse(kind="tool_call", tool="workspace.exec", arguments={"argv": ["forbidden"]}, usage={"input_tokens": 2, "output_tokens": 3})

    result = LocalRunWorkerService(
        repo, RunEventService(repo), worker_id="worker-a",
        provider_factory=lambda profile: Provider(), usage_meter=meter,
    ).run_once("owner@example.test")
    assert result["state"] == "failed"
    assert repo.get_run("owner@example.test", "run-1")["usage"]["total_tokens"] == 5


def test_worker_marks_provider_boundary_exception_unknown(tmp_path):
    from hub.application.run_event_service import RunEventService
    from hub.application.run_worker_service import LocalRunWorkerService
    from hub.infrastructure.platform_db import PlatformRepository

    repo = PlatformRepository(tmp_path / "platform.db")
    repo.init()
    repo.upsert_model("owner@example.test", {"profile_id": "model-a", "provider": "deterministic", "model": "a"})
    repo.upsert_workspace("owner@example.test", {"workspace_id": "home", "root_path": str(tmp_path / "workspace")})
    conversation = repo.create_conversation("owner@example.test", "conv-1", title="", workspace_id="home")
    repo.append_turn("owner@example.test", conversation["conversation_id"], "msg-1", "run-1", text="x", client_token="x", config_snapshot={"workspace_id": "home", "model_profile_id": "model-a"}, now=1)
    meter = UsageRepository(tmp_path / "platform.db")
    meter.init()

    class BrokenProvider:
        def complete(self, messages, tools):
            raise RuntimeError("transport failed")

    result = LocalRunWorkerService(
        repo, RunEventService(repo), worker_id="worker-a",
        provider_factory=lambda profile: BrokenProvider(), usage_meter=meter,
    ).run_once("owner@example.test")
    assert result["state"] == "unknown"
    assert repo.get_run("owner@example.test", "run-1")["state"] == "unknown"
