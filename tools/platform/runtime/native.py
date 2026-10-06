"""Finite native assistant loop using only the ToolBroker boundary."""
from __future__ import annotations

import json
import logging

from hub.diagnostics import log_failure

logger = logging.getLogger(__name__)
import time

from .base import RuntimeLimits, UsageSummary, WorkerResult
from ..providers.openai_compatible import ProviderError

from hub.infrastructure.usage_repository import UsageLimitExceeded, normalize_usage


class ProviderBoundaryUnknown(RuntimeError):
    """The provider call crossed the boundary but its final outcome is unknown."""
    def __init__(self, code='provider_boundary_unknown', *, diagnostic=None):
        super().__init__(code)
        self.diagnostic = diagnostic or {}



class NativeAssistantRuntime:
    def __init__(self, provider, broker, *, limits: RuntimeLimits | None = None,
                 usage_meter=None, model_key: str | None = None, clock=time.time):
        self.provider = provider
        self.broker = broker
        self.limits = limits or RuntimeLimits()
        self.usage_meter = usage_meter
        self.model_key = model_key or "default"
        self.clock = clock

    def run(self, *, run_id: str, owner_id: str, epoch: int, messages: list[dict],
            tools: list[dict], event_sink=None, should_cancel=None, attempt: int = 1) -> WorkerResult:
        event_sink = event_sink or (lambda kind, payload: None)
        transcript = list(messages)
        total_tokens = 0
        input_tokens = 0
        output_tokens = 0
        measured_total_tokens = 0
        provider_requests = 0
        executed_commands: set[str] = set()
        for step in range(1, self.limits.max_steps + 1):
            if should_cancel is not None and should_cancel():
                return self._result("cancelled", "运行已取消。", step - 1, input_tokens, output_tokens, measured_total_tokens, provider_requests)
            reservation = None
            if self.usage_meter is not None:
                try:
                    reservation = self.usage_meter.admit(
                        owner_id, self.model_key, run_id, attempt, step, now=float(self.clock()),
                    )
                except UsageLimitExceeded:
                    return self._result("failed", "模型预算已用尽。", step - 1, input_tokens, output_tokens, measured_total_tokens, provider_requests)
            try:
                response = self.provider.complete(transcript, tools, request_observer=lambda kind, data:
                    event_sink(kind, {**data, 'step': step, 'attempt': attempt}))
            except Exception as exc:
                diagnostic = exc.diagnostic() if isinstance(exc, ProviderError) else {'provider_error': 'provider_exception'}
                diagnostic.update({'step': step, 'attempt': attempt, 'phase': 'provider_call'})
                if reservation is not None:
                    self._settle_unknown(reservation, run_id=run_id, step=step, attempt=attempt)
                raise ProviderBoundaryUnknown(diagnostic=diagnostic) from exc
            try:
                usage = normalize_usage(response.usage)
                if reservation is not None:
                    self.usage_meter.settle(reservation, usage, "succeeded", now=float(self.clock()))
            except Exception as exc:
                if reservation is not None:
                    self._settle_unknown(reservation, run_id=run_id, step=step, attempt=attempt)
                raise ProviderBoundaryUnknown('usage_accounting_unknown', diagnostic={
                    'step': step, 'attempt': attempt, 'phase': 'usage_accounting',
                    'provider_error': 'usage_accounting_unknown'}) from exc
            provider_requests += 1
            input_tokens += usage["input_tokens"]
            output_tokens += usage["output_tokens"]
            measured_total_tokens += usage["total_tokens"]
            total_tokens = measured_total_tokens
            if should_cancel is not None and should_cancel():
                return self._result("cancelled", "运行已取消。", step, input_tokens, output_tokens, measured_total_tokens, provider_requests)
            if total_tokens > self.limits.max_tokens:
                return self._result("failed", "模型预算已用尽。", step, input_tokens, output_tokens, measured_total_tokens, provider_requests)
            if response.kind == "final":
                return self._result("succeeded", response.text[:32768], step, input_tokens, output_tokens, measured_total_tokens, provider_requests)
            if response.kind != "tool_call" or not response.tool:
                return self._result("failed", "模型返回了不支持的响应。", step, input_tokens, output_tokens, measured_total_tokens, provider_requests)
            command_id = f"{run_id}:step:{step}"
            if command_id in executed_commands:
                return self._result("failed", "工具调用重复。", step, input_tokens, output_tokens, measured_total_tokens, provider_requests)
            executed_commands.add(command_id)
            event_sink("tool_call", {
                "run_id": run_id, "step": step, "command_id": command_id,
                "tool": response.tool,
                "argument_keys": sorted((response.arguments or {}).keys())[:32],
            })
            receipt = self.broker.execute(
                command_id=command_id, tool=response.tool,
                arguments=response.arguments or {}, owner_id=owner_id, epoch=epoch,
            )
            event_sink("tool_result", {
                "run_id": run_id, "step": step, "command_id": command_id,
                "tool": response.tool, "state": receipt.state,
                "error_code": receipt.error_code,
            })
            if receipt.state == "unknown":
                return self._result("unknown", "远程工具回执未知，等待后续核查。", step, input_tokens, output_tokens, measured_total_tokens, provider_requests)
            if receipt.state != "succeeded":
                return self._result("failed", "工具执行失败。", step, input_tokens, output_tokens, measured_total_tokens, provider_requests)
            if should_cancel is not None and should_cancel():
                return self._result("cancelled", "运行已取消。", step, input_tokens, output_tokens, measured_total_tokens, provider_requests)
            call_id = response.tool_call_id or command_id
            transcript.append({
                "role": "assistant",
                "content": response.text or "",
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": response.tool,
                        "arguments": json.dumps(response.arguments or {}, ensure_ascii=False, separators=(",", ":")),
                    },
                }],
            })
            transcript.append({
                "role": "tool",
                "tool_call_id": call_id,
                "tool": response.tool,
                "result": receipt.result,
            })
        return self._result("failed", "模型步骤已达到上限。", self.limits.max_steps, input_tokens, output_tokens, measured_total_tokens, provider_requests)

    def _settle_unknown(self, reservation, *, run_id, step, attempt):
        try:
            self.usage_meter.settle(reservation, None, "unknown", now=float(self.clock()))
        except Exception as exc:
            # The provider/accounting boundary remains uncertain; a second failure
            # must not replace its original cause or permit automatic replay.
            log_failure(logger, 'platform_usage_settlement_failed', exc,
                        run_id=run_id, step=step, attempt=attempt)

    @staticmethod
    def _result(state, text, steps, input_tokens, output_tokens, total_tokens, provider_requests):
        return WorkerResult(
            state, text, steps, total_tokens,
            UsageSummary(input_tokens, output_tokens, total_tokens, provider_requests),
        )


__all__ = ["NativeAssistantRuntime", "ProviderBoundaryUnknown"]
