"""Durable local worker for the platform assistant Run queue.

This is an intentionally small worker: SQLite owns the Run lease, the
workspace backend is selected from the immutable config snapshot, and the
provider is selected through an explicit ProviderFactory. No worker thread is
started during app construction.
"""
from __future__ import annotations

import logging
import secrets
import time
from pathlib import Path
from typing import Any, Callable

from tools.platform.assistant_worker import PersistentAssistantWorker
from tools.platform.backends.sandbox import SandboxBackend
from tools.platform.providers.openai_compatible import ProviderFactory, ProviderUnavailable
from tools.platform.resource_lease import ResourceLeaseError, ResourceLeaseManager
from tools.platform.runtime.base import RuntimeLimits
from tools.platform.runtime.native import NativeAssistantRuntime, ProviderBoundaryUnknown
from hub.application.platform_memory_context_service import (
    PlatformMemoryContextError,
    build_context_message,
)
from tools.platform.tool_broker import ToolBroker
from tools.platform.remote_tool_broker import RemoteToolBroker
from hub.domain.platform import ACCEPTANCE_TOOL_POLICY


from hub.diagnostics import log_failure

logger = logging.getLogger(__name__)


from hub.application.run_lease_heartbeat import LeaseHeartbeat, RunLeaseLost


class LocalRunWorkerService:
    """Claim and execute at most one durable Run per call."""

    def __init__(self, repository, run_events, *, worker_id: str | None = None,
                 lease_s: float = 60.0, clock: Callable[[], float] = time.time,
                 provider_factory: Callable[[dict[str, Any]], Any] | None = None,
                 limits: RuntimeLimits | None = None, artifact_store=None,
                 diagnostics=None, secret_broker=None, provider_transport=None,
                 provider_network_enabled: bool = False,
                 remote_execution_enabled: bool = False, remote_delivery=None,
                 remote_waiter=None, usage_meter=None, sandbox_launcher=None,
                 browser_enabled: bool = False, browser_network_enabled: bool = False,
                 browser_allowed_origins: tuple[str, ...] = (), browser_resolver=None):
        self.repository = repository
        self.run_events = run_events
        self.worker_id = worker_id or ("local-worker_" + secrets.token_hex(8))
        self.lease_s = max(5.0, min(3600.0, float(lease_s)))
        self.clock = clock
        self.provider_factory = provider_factory or ProviderFactory(
            secret_broker=secret_broker, transport=provider_transport,
            allow_network=provider_network_enabled)
        self.limits = limits or RuntimeLimits()
        self.artifact_store = artifact_store
        self.diagnostics = diagnostics
        self.resource_leases = ResourceLeaseManager(clock=self.clock)
        self.remote_execution_enabled = bool(remote_execution_enabled)
        self.browser_enabled = bool(browser_enabled)
        self.remote_delivery = remote_delivery
        self.remote_waiter = remote_waiter
        self.usage_meter = usage_meter
        self.sandbox_launcher = sandbox_launcher
        self.browser_network_enabled = bool(browser_network_enabled)
        self.browser_allowed_origins = tuple(browser_allowed_origins or ())
        self.browser_resolver = browser_resolver

    def _finish(self, claim: dict[str, Any], state: str, *, text: str = "", usage=None) -> dict[str, Any]:
        return self.run_events.state(
            claim["owner_id"], claim["run_id"], state, result_text=text,
            usage=usage,
            lease_id=claim["lease_id"], worker_id=self.worker_id,
        )

    def _event(self, claim: dict[str, Any], kind: str, payload: dict[str, Any]):
        return self.run_events.append(
            claim["owner_id"], claim["run_id"], kind, payload,
            lease_id=claim["lease_id"], worker_id=self.worker_id,
        )

    def run_once(self, owner_id: str) -> dict[str, Any] | None:
        claim = self.repository.claim_run(
            owner_id, worker_id=self.worker_id, now=float(self.clock()), lease_s=self.lease_s,
        )
        if claim is None:
            return None
        return self.execute_claim(claim)

    def execute_claim(self, claim: dict[str, Any]) -> dict[str, Any]:
        """Execute a Run already fenced to this worker."""
        if claim.get("recovered_before_claim") or claim.get("cancelled_before_claim"):
            return claim
        if claim.get("lease_owner") != self.worker_id:
            return {"run_id": claim.get("run_id"), "state": "lease_lost",
                    "attempt": claim.get("attempt", 0)}

        lease_id = claim["lease_id"]
        workspace = None
        lease = None
        resource_id = None
        lease_owner = None
        heartbeat = None
        try:
            config = claim.get("config_snapshot") or {}
            policy = config.get("tool_policy")
            if policy not in (None, ACCEPTANCE_TOOL_POLICY):
                raise RuntimeError("tool_policy_unavailable")
            allowed_tools = frozenset({"workspace.list"}) if policy == ACCEPTANCE_TOOL_POLICY else None
            workspace_id = config.get("workspace_id") or claim.get("conversation_workspace_id")
            if not workspace_id:
                raise RuntimeError("workspace_unavailable")
            workspace = self.repository.get_workspace(claim["owner_id"], workspace_id)
            if not workspace or not workspace.get("enabled"):
                raise RuntimeError("workspace_unavailable")
            if workspace.get("backend") != "directory":
                raise RuntimeError("workspace_backend_unavailable")

            leases = self.resource_leases
            resource_id = f"{claim['owner_id']}:{workspace['workspace_id']}"
            lease_owner = f"{claim['owner_id']}:{claim['run_id']}:{claim['attempt']}"
            lease = leases.acquire(resource_id, lease_owner, ttl_s=self.lease_s)

            def renew_leases():
                now = float(self.clock())
                if not self.repository.renew_run_lease(
                    claim["owner_id"], claim["run_id"], lease_id=lease_id,
                    worker_id=self.worker_id, now=now, lease_s=self.lease_s,
                ):
                    raise RunLeaseLost("lease_mismatch")
                self.resource_leases.renew(
                    resource_id, lease_owner, lease["epoch"], ttl_s=self.lease_s,
                )
            heartbeat = LeaseHeartbeat(renew_leases, interval=self.lease_s / 3, name=claim['run_id'])
            heartbeat.start()

            def should_cancel():
                heartbeat.pulse()
                return self.repository.run_cancel_requested(
                    claim["owner_id"], claim["run_id"], lease_id=lease_id,
                )

            execution_node_id = config.get("execution_node_id")
            local_node = execution_node_id in (None, "", "local", "hub", "host")
            remote = bool(execution_node_id and not local_node)
            if remote and not self.remote_execution_enabled:
                raise RuntimeError("remote_execution_disabled")
            if remote and self.remote_delivery is None:
                raise RuntimeError("remote_delivery_unavailable")
            remote_browser_enabled = False
            if remote and self.browser_enabled:
                node = self.repository.get_node(claim["owner_id"], execution_node_id)
                capabilities = node.get("capabilities") if isinstance(node, dict) else {}
                remote_browser_enabled = bool(
                    node and node.get("enabled") and isinstance(capabilities, dict)
                    and capabilities.get("browser.session")
                )
            backend = SandboxBackend(Path(workspace["root_path"]), launcher=self.sandbox_launcher) if not remote else None
            provider_profile = None
            profile_id = config.get("model_profile_id")
            if profile_id:
                provider_profile = self.repository.get_model(claim["owner_id"], profile_id)
                if not provider_profile or not provider_profile.get("enabled"):
                    raise RuntimeError("model_unavailable")
                frozen = config.get("provider_snapshot")
                if isinstance(frozen, dict):
                    # The secret reference remains catalog-owned so rotation
                    # can happen without mutating queued Run state.  All
                    # non-secret provider inputs are taken from the snapshot.
                    provider_profile = {
                        **provider_profile,
                        "provider": frozen.get("provider"),
                        "model": frozen.get("model"),
                        "provider_config": frozen.get("provider_config") or {},
                        "capabilities": frozen.get("capabilities") or {},
                    }
            try:
                provider = self.provider_factory(provider_profile)
            except ProviderUnavailable as exc:
                raise RuntimeError(str(exc)) from exc
            if remote:
                broker = RemoteToolBroker(
                    self.remote_delivery, node_id=execution_node_id,
                    resource_id=workspace["workspace_id"], run_id=claim["run_id"],
                    waiter=self.remote_waiter,
                    allowed_tools=allowed_tools,
                    event_sink=lambda kind, payload: self._event(claim, kind, payload),
                    browser_enabled=remote_browser_enabled,
                    browser_network_enabled=self.browser_network_enabled,
                    browser_allowed_origins=self.browser_allowed_origins,
                    browser_resolver=self.browser_resolver,
                )
            else:
                broker = ToolBroker(
                    backend, leases, resource_id=resource_id,
                    lease_owner_id=lease_owner, artifact_store=self.artifact_store,
                    diagnostics=self.diagnostics,
                    artifact_workspace_id=workspace["workspace_id"],
                    allowed_tools=allowed_tools,
                )
            runtime = NativeAssistantRuntime(
                provider, broker, limits=self.limits,
                usage_meter=self.usage_meter, model_key=profile_id or "default",
                clock=self.clock,
            )
            worker = PersistentAssistantWorker(runtime, self.run_events)
            messages = list(claim.get("messages") or [])
            memory_snapshot = config.get("memory_context")
            if isinstance(memory_snapshot, dict) and memory_snapshot.get("enabled"):
                context_message = build_context_message(config)
                if context_message is None:
                    raise PlatformMemoryContextError("memory_context_invalid")
                selected_ids = [
                    str(item.get("memory_id"))[:128]
                    for item in (memory_snapshot.get("items") or [])
                    if isinstance(item, dict) and item.get("memory_id")
                ][:20]
                self._event(claim, "memory_context_selected", {
                    "run_id": claim["run_id"],
                    "mode": str(memory_snapshot.get("mode") or "none")[:16],
                    "memory_ids": selected_ids,
                    "item_count": int(memory_snapshot.get("item_count", 0) or 0),
                    "bytes": int(memory_snapshot.get("bytes", 0) or 0),
                    "max_items": int(memory_snapshot.get("max_items", 0) or 0),
                    "max_bytes": int(memory_snapshot.get("max_bytes", 0) or 0),
                })
                messages.insert(0, context_message)
            result = worker.execute(
                run_id=claim["run_id"], owner_id=claim["owner_id"],
                epoch=lease["epoch"], messages=messages,
                tools=ToolBroker.tool_definitions(
                    allowed_tools,
                    browser_enabled=remote_browser_enabled),
                should_cancel=should_cancel,
                lease_id=lease_id, worker_id=self.worker_id,
                attempt=int(claim.get("attempt") or 1),
            )
            final = self._finish(claim, result.state, text=result.text, usage=result.usage.as_dict())
            return final
        except RunLeaseLost:
            return {"run_id": claim["run_id"], "state": "lease_lost", "attempt": claim["attempt"]}
        except PlatformMemoryContextError as exc:
            code = str(exc)[:120]
            try:
                self._event(claim, "run_failed", {
                    "run_id": claim["run_id"], "error_code": code,
                })
                return self._finish(claim, "failed", text=code)
            except Exception:
                return {"run_id": claim["run_id"], "state": "lease_lost", "attempt": claim["attempt"]}
        except ProviderBoundaryUnknown as exc:
            diagnostic = {'run_id': claim['run_id'], 'error_code': str(exc),
                          'attempt': claim['attempt'], **exc.diagnostic}
            log_failure(logger, 'platform_run_failure', exc,
                        worker_id=self.worker_id, **diagnostic)
            try:
                self._event(claim, "run_unknown", diagnostic)
                usage = self.usage_meter.get_run_usage(
                    claim["owner_id"], claim["run_id"]
                ) if self.usage_meter is not None else None
                detail = '模型调用结果待确认：' + diagnostic.get('provider_error', 'provider_error')
                if diagnostic.get('upstream_code'): detail += ' / ' + diagnostic['upstream_code']
                if diagnostic.get('provider_status'): detail += '（HTTP ' + str(diagnostic['provider_status']) + '）'
                return self._finish(claim, "unknown", text=detail, usage=usage)
            except Exception as persist_error:
                log_failure(logger, 'platform_failure_persistence_failed', persist_error,
                            run_id=claim['run_id'], worker_id=self.worker_id)
                return {"run_id": claim["run_id"], "state": "lease_lost", "attempt": claim["attempt"]}
        except (ResourceLeaseError, RuntimeError) as exc:
            log_failure(logger, 'platform_run_failure', exc,
                        run_id=claim['run_id'], worker_id=self.worker_id)
            code = str(exc)[:120]
            try:
                self._event(claim, "run_failed", {"run_id": claim["run_id"], "error_code": code})
                usage = self.usage_meter.get_run_usage(
                    claim["owner_id"], claim["run_id"]
                ) if self.usage_meter is not None else None
                return self._finish(claim, "failed", text=code, usage=usage)
            except Exception:
                return {"run_id": claim["run_id"], "state": "lease_lost", "attempt": claim["attempt"]}
        except Exception as exc:
            log_failure(logger, 'platform_run_failure', exc,
                        run_id=claim['run_id'], worker_id=self.worker_id)
            try:
                self._event(claim, "run_unknown", {"run_id": claim["run_id"]})
                usage = self.usage_meter.get_run_usage(
                    claim["owner_id"], claim["run_id"]
                ) if self.usage_meter is not None else None
                return self._finish(claim, "unknown", usage=usage)
            except Exception:
                return {"run_id": claim["run_id"], "state": "lease_lost", "attempt": claim["attempt"]}
        finally:
            if heartbeat is not None:
                heartbeat.close()
            if lease is not None and resource_id is not None and lease_owner is not None:
                try:
                    self.resource_leases.release(resource_id, lease_owner, lease["epoch"])
                except ResourceLeaseError:
                    pass

    def run_until_idle(self, owner_id: str, *, max_runs: int = 1) -> list[dict[str, Any]]:
        results = []
        for _ in range(max(1, min(int(max_runs), 100))):
            result = self.run_once(owner_id)
            if result is None:
                break
            results.append(result)
        return results


__all__ = ["LocalRunWorkerService", "RunLeaseLost"]
