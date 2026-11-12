"""Durable local worker for the platform assistant Run queue.

This is an intentionally small worker: SQLite owns the Run lease, the
workspace backend is selected from the immutable config snapshot, and the
provider is selected through an explicit ProviderFactory. No worker thread is
started during app construction.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from hub.platform.assistant_worker import PersistentAssistantWorker
from fleet_core.backends.sandbox import SandboxBackend
from fleet_core.command_timing import CommandTimingPolicy
from hub.platform.providers.openai_compatible import ProviderFactory, ProviderUnavailable
from hub.platform.resource_lease import ResourceLeaseError, ResourceLeaseManager
from hub.platform.runtime.base import RuntimeLimits
from hub.platform.runtime.native import NativeAssistantRuntime, ProviderBoundaryUnknown
from hub.application.platform_memory_context_service import (
    PlatformMemoryContextError,
    build_context_message,
)
from hub.platform.tool_broker import ToolBroker
from hub.platform.remote_tool_broker import RemoteToolBroker
from fleet_core.web_agent_contract import WEB_AGENT_TOOLS
from hub.domain.platform import ACCEPTANCE_TOOL_POLICY
from hub.domain.workflow_delivery import DeliveryRunLease
from hub.domain.workflow_native_memory import (
    NATIVE_CODE_MODE_MARKER, NATIVE_MEDIA_MODE_MARKER, NATIVE_MEMORY_MARKER, NATIVE_TEXT_MODE_MARKER, NativeWorkflowMemoryGuard,
    WorkflowNativeMemoryError,
)
from hub.application.workflow_code_inputs import WorkflowCodeInputs
from hub.application.workflow_test_execution import WorkflowTestExecutionError, WorkflowTestExecutionService
from hub.application.workflow_review_execution import WorkflowReviewExecutionError, WorkflowReviewExecutionService


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
                 browser_allowed_origins: tuple[str, ...] = (), browser_resolver=None,
                 browser_submit_enabled: bool = False, submit_approvals=None,
                 conversation_service=None, service_actions=None,
                 defaults_service=None, remote_timing_policy: CommandTimingPolicy | None = None,
                 mcp_catalog=None, mcp_enabled: bool = False,
                 workflow_test_executor: WorkflowTestExecutionService | None = None,
                 workflow_remote_test_executor: WorkflowTestExecutionService | None = None,
                 workflow_review_executor: WorkflowReviewExecutionService | None = None,
                 workflow_memory_guard: NativeWorkflowMemoryGuard | None = None,
                 workflow_code_inputs: WorkflowCodeInputs | None = None):
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
        self.remote_timing_policy = remote_timing_policy or CommandTimingPolicy()
        self.usage_meter = usage_meter
        self.sandbox_launcher = sandbox_launcher
        self.browser_network_enabled = bool(browser_network_enabled)
        self.browser_allowed_origins = tuple(browser_allowed_origins or ())
        self.browser_resolver = browser_resolver
        self.browser_submit_enabled = bool(browser_submit_enabled and browser_enabled and browser_network_enabled)
        self.submit_approvals = submit_approvals
        self.conversation_service = conversation_service
        self.service_actions = service_actions
        self.defaults_service = defaults_service
        self.mcp_catalog = mcp_catalog
        self.mcp_enabled = bool(mcp_enabled and mcp_catalog is not None)
        self.workflow_test_executor = workflow_test_executor
        self.workflow_remote_test_executor = workflow_remote_test_executor
        self.workflow_review_executor = workflow_review_executor
        self.workflow_memory_guard = workflow_memory_guard
        self.workflow_code_inputs = workflow_code_inputs

    def configure_workspace_concurrency(self, capacity: int) -> None:
        """Align tool lease capacity with the scheduler's durable admission."""
        self.resource_leases.set_capacity(capacity)

    @staticmethod
    def _media_request_transform(parts: tuple[dict[str, Any], ...], trigger: dict[str, Any]):
        """Attach attempt-local media only to the outgoing provider request copy."""
        def transform(messages):
            outgoing = [dict(message) for message in messages]
            selected = [message for message in outgoing if message.get("message_id") == trigger["message_id"]]
            if (len(selected) != 1 or selected[0].get("role") != "user"
                    or selected[0].get("content") != trigger["content"]):
                raise WorkflowNativeMemoryError("native_memory_mismatch")
            selected[0]["content"] = [{"type": "text", "text": trigger["content"]}, *parts]
            return outgoing
        return transform

    def _finish(self, claim: dict[str, Any], state: str, *, text: str = "", usage=None) -> dict[str, Any]:
        # Run termination is deterministic: once this Run leaves the queue
        # (succeeded/failed/unknown/cancelled), any approval it still holds
        # can no longer be dispatched, so expire it to free the budgets.
        if self.submit_approvals is not None:
            try:
                self.submit_approvals.expire_run_submit_approvals(
                    claim["owner_id"], claim["run_id"])
            except Exception:
                logger.exception("platform_submit_approval_sweep_failed",
                                 extra={"run_id": claim["run_id"],
                                        "worker_id": self.worker_id})
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
        source_view_context = None
        source_view = None
        try:
            def save_checkpoint(value):
                try:
                    self.repository.save_runtime_checkpoint(
                        claim["owner_id"], claim["run_id"], value, lease_id=lease_id,
                        worker_id=self.worker_id, now=float(self.clock()))
                except RuntimeError as exc:
                    if getattr(exc, 'code', None) == 'lease_mismatch':
                        raise RunLeaseLost('lease_mismatch') from exc
                    raise OSError('runtime_checkpoint_unavailable') from exc

            checkpoint = self.repository.load_runtime_checkpoint(
                claim["owner_id"], claim["run_id"], lease_id=lease_id,
                worker_id=self.worker_id, now=float(self.clock()))
            if checkpoint and checkpoint['phase'] == 'complete':
                saved = checkpoint['result']
                return self._finish(claim, saved['state'], text=saved['text'], usage=saved['usage'])
            config = claim.get("config_snapshot") or {}
            if checkpoint and checkpoint['phase'] == 'remote_wait':
                if self.remote_delivery is None:
                    raise OSError('remote_observation_unavailable')
                observer = RemoteToolBroker(
                    self.remote_delivery, node_id=config.get('execution_node_id'),
                    resource_id=config.get('workspace_id'), run_id=claim['run_id'],
                    should_cancel=lambda: self.repository.run_cancel_requested(
                        claim['owner_id'], claim['run_id'], lease_id=lease_id),
                    event_sink=lambda kind, payload: self._event(claim, kind, payload))
                receipt = observer.observe(command_id=f"{claim['run_id']}:step:{checkpoint['step']}",
                                           tool=checkpoint['response']['tool'])
                if receipt.state == 'waiting_node':
                    return self.repository.suspend_native_run(
                        claim['owner_id'], claim['run_id'], lease_id=lease_id,
                        worker_id=self.worker_id, now=float(self.clock()))
                checkpoint = {**checkpoint, 'phase': 'receipt', 'receipt': asdict(receipt)}
                save_checkpoint(checkpoint)
            if checkpoint and checkpoint['phase'] == 'receipt' and (
                    checkpoint['receipt']['state'] != 'succeeded'
                    or self.repository.run_cancel_requested(claim['owner_id'], claim['run_id'], lease_id=lease_id)):
                # A terminal tool outcome needs no new provider, sandbox or
                # workspace. Runtime owns outcome/cancellation precedence.
                result = NativeAssistantRuntime(None, None, checkpoint=checkpoint,
                    save_checkpoint=save_checkpoint).run(
                        run_id=claim['run_id'], owner_id=claim['owner_id'], epoch=0,
                        messages=[], tools=[],
                        event_sink=lambda kind, payload: self._event(claim, kind, payload),
                        should_cancel=lambda: self.repository.run_cancel_requested(
                            claim['owner_id'], claim['run_id'], lease_id=lease_id))
                return self._finish(claim, result.state, text=result.text, usage=result.usage.as_dict())
            context_message = build_context_message(config)
            native_memory = None
            if self.workflow_memory_guard is not None:
                native_memory = self.workflow_memory_guard.prepare(claim, now=float(self.clock()))
            elif {NATIVE_MEMORY_MARKER, NATIVE_TEXT_MODE_MARKER, NATIVE_CODE_MODE_MARKER, NATIVE_MEDIA_MODE_MARKER} & config.keys():
                raise WorkflowNativeMemoryError("native_memory_guard_unavailable")
            elif str(claim.get("run_id", "")).startswith("wf-run-") and "workflow_dispatch" not in config:
                raise WorkflowNativeMemoryError("invalid_native_memory_run")
            policy = config.get("tool_policy")
            if policy not in (None, ACCEPTANCE_TOOL_POLICY):
                raise RuntimeError("tool_policy_unavailable")
            allowed_tools = frozenset({"workspace.list"}) if policy == ACCEPTANCE_TOOL_POLICY else None
            remote_allowed_tools = (allowed_tools if allowed_tools is not None else
                                    ToolBroker.TOOLS - ToolBroker.LOCAL_ONLY_TOOLS)
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

            def remote_wait_cancelled():
                # The background heartbeat owns renewal cadence. A Node wait
                # polls every 100ms and must not acquire SQLite's write lock
                # for each cancellation/ownership read.
                heartbeat.check()
                return self.repository.run_cancel_requested(
                    claim["owner_id"], claim["run_id"], lease_id=lease_id,
                )

            if "workflow_remote_test" in config and "workflow_test" not in config:
                raise WorkflowTestExecutionError("invalid_remote_test_configuration")
            if "workflow_test" in config:
                if "workflow_review" in config:
                    raise RuntimeError("invalid_review_run_configuration")
                remote_test = "workflow_remote_test" in config
                if remote_test and not self.remote_execution_enabled:
                    raise RuntimeError("remote_execution_disabled")
                test_executor = self.workflow_remote_test_executor if remote_test else self.workflow_test_executor
                if test_executor is None:
                    raise RuntimeError("workflow_test_unavailable")
                execution = asyncio.run(test_executor.execute(
                    DeliveryRunLease(claim["owner_id"], claim["run_id"], lease_id, self.worker_id, claim["attempt"]),
                    should_cancel=should_cancel,
                    on_started=lambda: self._event(claim, "run_started", {"run_id": claim["run_id"]}),
                ))
                self._event(claim, "workflow_test_finished", {"run_id": claim["run_id"], "state": execution.state,
                    "passed": execution.passed, "report_hash": execution.report_hash})
                return self._finish(claim, execution.state, text="fixed_test_completed" if execution.receipt is not None else "test_cancelled",
                    usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "provider_requests": 0})

            if "workflow_review" in config:
                if self.workflow_review_executor is None:
                    raise RuntimeError("workflow_review_unavailable")
                review = asyncio.run(self.workflow_review_executor.execute(
                    DeliveryRunLease(claim["owner_id"], claim["run_id"], lease_id, self.worker_id, claim["attempt"]),
                    should_cancel=should_cancel,
                    on_started=lambda: self._event(claim, "run_started", {"run_id": claim["run_id"]}),
                ))
                self._event(claim, "workflow_review_finished", {"run_id": claim["run_id"], "state": review.state,
                    "verdict": review.verdict, "report_hash": review.report_hash})
                return self._finish(claim, review.state, text="review_completed" if review.receipt is not None else "review_cancelled",
                    usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "provider_requests": 0})

            execution_node_id = config.get("execution_node_id")
            local_node = execution_node_id in (None, "", "local", "hub", "host")
            remote = bool(execution_node_id and not local_node)
            if remote and not self.remote_execution_enabled:
                raise RuntimeError("remote_execution_disabled")
            if remote and self.remote_delivery is None:
                raise RuntimeError("remote_delivery_unavailable")
            remote_browser_enabled = False
            remote_submit_enabled = False
            remote_web_agent_enabled = False
            if remote:
                agent_node = self.repository.get_node(claim["owner_id"], execution_node_id)
                agent_caps = agent_node.get("capabilities", {}) if agent_node and agent_node.get("enabled") else {}
                enabled_agent_tools = {name for name in WEB_AGENT_TOOLS if agent_caps.get(name) is True}
                remote_web_agent_enabled = bool(enabled_agent_tools)
                remote_allowed_tools = (remote_allowed_tools - WEB_AGENT_TOOLS) | (remote_allowed_tools & enabled_agent_tools)
            if remote and self.browser_enabled:
                node = self.repository.get_node(claim["owner_id"], execution_node_id)
                capabilities = node.get("capabilities") if isinstance(node, dict) else {}
                remote_browser_enabled = bool(
                    node and node.get("enabled") and isinstance(capabilities, dict)
                    and capabilities.get("browser.session")
                )
                remote_submit_enabled = bool(remote_browser_enabled and self.browser_submit_enabled
                                             and capabilities.get("browser.submit") and self.submit_approvals is not None)
            backend = SandboxBackend(Path(workspace["root_path"]), launcher=self.sandbox_launcher) if not remote else None
            if native_memory is not None and native_memory.codes:
                if remote or self.workflow_code_inputs is None:
                    raise WorkflowNativeMemoryError("native_code_guard_unavailable")
                code_view_context = self.workflow_code_inputs.open_view_for_scope(
                    claim["owner_id"], workspace["workspace_id"], native_memory.codes, should_cancel)
                source_view = code_view_context.__enter__()
                source_view_context = code_view_context
            media_parts = ()
            media_transform = None
            if native_memory is not None and (native_memory.uses_media or native_memory.uses_audio or native_memory.uses_video):
                if remote or self.workflow_memory_guard is None:
                    raise WorkflowNativeMemoryError("native_media_guard_unavailable")
                if native_memory.uses_video:
                    media_parts = self.workflow_memory_guard.load_video(claim, native_memory, now=float(self.clock()))
                elif native_memory.uses_audio:
                    media_parts = self.workflow_memory_guard.load_audio(claim, native_memory, now=float(self.clock()))
                else:
                    media_parts = self.workflow_memory_guard.load_media(claim, native_memory, now=float(self.clock()))
                media_transform = self._media_request_transform(media_parts, claim["message"])
                media_transform(checkpoint["transcript"] if checkpoint else claim.get("messages") or [])
            provider_profile = None
            profile_id = config.get("model_profile_id")
            if profile_id:
                provider_profile = self.repository.get_model(claim["owner_id"], profile_id)
                if not provider_profile or not provider_profile.get("enabled"):
                    raise RuntimeError("model_unavailable")
                frozen = config.get("provider_snapshot")
                if isinstance(frozen, dict):
                    # Older snapshots lack a credential binding. They can use
                    # the live reference only while the destination is unchanged.
                    if "secret_ref" not in frozen and (
                        frozen.get("provider") != provider_profile.get("provider")
                        or (frozen.get("provider_config") or {}).get("endpoint")
                        != (provider_profile.get("provider_config") or {}).get("endpoint")
                    ):
                        raise RuntimeError("model_binding_changed")
                    provider_profile = {
                        **provider_profile,
                        "provider": frozen.get("provider"),
                        "model": frozen.get("model"),
                        "provider_config": frozen.get("provider_config") or {},
                        "capabilities": frozen.get("capabilities") or {},
                        "secret_ref": frozen.get("secret_ref", provider_profile.get("secret_ref")),
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
                    should_cancel=remote_wait_cancelled,
                    defer_wait=self.remote_waiter is None,
                    timing_policy=self.remote_timing_policy, clock=self.clock,
                    allowed_tools=remote_allowed_tools,
                    event_sink=lambda kind, payload: self._event(claim, kind, payload),
                    browser_enabled=remote_browser_enabled,
                    web_agent_enabled=remote_web_agent_enabled,
                    browser_submit_enabled=remote_submit_enabled,
                    submit_approvals=self.submit_approvals,
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
                    conversation_service=self.conversation_service,
                    service_actions=self.service_actions,
                    defaults_service=self.defaults_service,
                    mcp_catalog=self.mcp_catalog, mcp_enabled=self.mcp_enabled,
                    mcp_pins=config.get("mcp_tools", {}),
                    workflow_code_view=source_view,
                )
            runtime = NativeAssistantRuntime(
                provider, broker, limits=self.limits,
                usage_meter=self.usage_meter, model_key=profile_id or "default",
                clock=self.clock, checkpoint=checkpoint, save_checkpoint=save_checkpoint,
                request_message_transform=media_transform,
            )
            worker = PersistentAssistantWorker(runtime, self.run_events)
            messages = list(claim.get("messages") or [])
            if native_memory is not None:
                messages.insert(0, native_memory.message)
            memory_snapshot = config.get("memory_context")
            if context_message is not None:
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
                    remote_allowed_tools if remote else allowed_tools,
                    browser_enabled=remote_browser_enabled, browser_submit_enabled=remote_submit_enabled,
                    web_agent_enabled=remote_web_agent_enabled,
                    service_actions_enabled=not remote and self.service_actions is not None,
                    mcp_catalog=self.mcp_catalog if not remote else None,
                    mcp_enabled=self.mcp_enabled if not remote else False,
                    mcp_owner_id=claim["owner_id"] if not remote else None,
                    mcp_workspace_id=workspace["workspace_id"] if not remote else None,
                    mcp_pins=config.get("mcp_tools", {}),
                    workflow_code_inputs=source_view is not None),
                should_cancel=should_cancel,
                lease_id=lease_id, worker_id=self.worker_id,
                attempt=int(claim.get("attempt") or 1),
            )
            if result.state == 'waiting_node':
                return self.repository.suspend_native_run(
                    claim['owner_id'], claim['run_id'], lease_id=lease_id,
                    worker_id=self.worker_id, now=float(self.clock()))
            final = self._finish(claim, result.state, text=result.text, usage=result.usage.as_dict())
            return final
        except (WorkflowTestExecutionError, WorkflowReviewExecutionError, WorkflowNativeMemoryError) as exc:
            if exc.code == "binding_not_ready":
                returned = self.repository.requeue_claimed_run(claim["owner_id"], claim["run_id"], lease_id=lease_id,
                    worker_id=self.worker_id, now=float(self.clock()))
                if returned:
                    return self.repository.get_run(claim["owner_id"], claim["run_id"])
                return {"run_id": claim["run_id"], "state": "lease_lost", "attempt": claim["attempt"]}
            log_failure(logger, "platform_workflow_validation_failure", exc, run_id=claim["run_id"], worker_id=self.worker_id)
            try:
                state = "unknown" if getattr(exc, "uncertain", False) else "failed"
                self._event(claim, "run_" + state, {"run_id": claim["run_id"], "error_code": exc.code})
                return self._finish(claim, state, text=exc.code)
            except Exception as persist_error:
                log_failure(logger, "platform_failure_persistence_failed", persist_error,
                            run_id=claim["run_id"], worker_id=self.worker_id)
                return {"run_id": claim["run_id"], "state": "lease_lost", "attempt": claim["attempt"]}
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
            if source_view_context is not None:
                source_view_context.__exit__(None, None, None)
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
