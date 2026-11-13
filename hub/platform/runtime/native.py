"""Finite native assistant loop using only the ToolBroker boundary."""
from __future__ import annotations

import json
from dataclasses import asdict
import logging

from hub.diagnostics import log_failure

logger = logging.getLogger(__name__)
import time

from .base import RuntimeLimits, UsageSummary, WorkerResult
from fleet_core.backends.base import ToolReceipt
from ..providers.openai_compatible import ProviderError
from ..providers.base import ModelResponse

from hub.infrastructure.usage_repository import UsageLimitExceeded, normalize_usage


class ProviderBoundaryUnknown(RuntimeError):
    """The provider call crossed the boundary but its final outcome is unknown."""
    def __init__(self, code='provider_boundary_unknown', *, diagnostic=None):
        super().__init__(code)
        self.diagnostic = diagnostic or {}


class _RunCancelled(Exception):
    """Stop consuming narration or dispatching retries after user cancellation."""


class NativeAssistantRuntime:
    def __init__(self, provider, broker, *, limits: RuntimeLimits | None = None,
                 usage_meter=None, model_key: str | None = None, clock=time.time,
                 checkpoint=None, save_checkpoint=None, request_message_transform=None):
        self.provider = provider
        self.broker = broker
        self.limits = RuntimeLimits(**checkpoint['limits']) if checkpoint and 'limits' in checkpoint else (limits or RuntimeLimits())
        self.usage_meter = usage_meter
        self.model_key = model_key or "default"
        self.clock = clock
        self.checkpoint = checkpoint
        self.save_checkpoint = save_checkpoint
        self.request_message_transform = request_message_transform

    def run(self, *, run_id: str, owner_id: str, epoch: int, messages: list[dict],
            tools: list[dict], event_sink=None, should_cancel=None, attempt: int = 1) -> WorkerResult:
        event_sink = event_sink or (lambda kind, payload: None)
        progress = self.checkpoint
        transcript = list(progress['transcript'] if progress else messages)
        restored_usage = progress['usage'] if progress else {}
        input_tokens = restored_usage.get('input_tokens', 0)
        output_tokens = restored_usage.get('output_tokens', 0)
        measured_total_tokens = restored_usage.get('total_tokens', 0)
        provider_requests = restored_usage.get('provider_requests', 0)
        total_tokens = measured_total_tokens
        start_step = progress['step'] if progress else 1

        def persist(phase, step, **extra):
            if self.save_checkpoint is not None:
                self.save_checkpoint({
                    'version': 1, 'phase': phase, 'step': step, 'limits': asdict(self.limits),
                    'transcript': transcript,
                    'usage': UsageSummary(input_tokens, output_tokens, measured_total_tokens,
                                          provider_requests).as_dict(), **extra})

        def finish(state, text, steps, *_):
            result = self._result(state, text, steps, input_tokens, output_tokens,
                                  measured_total_tokens, provider_requests)
            persist('complete', max(1, steps), result=asdict(result))
            return result

        if progress and progress['phase'] == 'complete':
            result = progress['result']
            return WorkerResult(result['state'], result['text'], result['steps'],
                                result['estimated_tokens'], UsageSummary(**result['usage']))
        if progress and progress['phase'] not in {'ready', 'response', 'receipt', 'remote_wait'}:
            return self._result('unknown', '执行结果待核查。', start_step,
                                input_tokens, output_tokens, measured_total_tokens, provider_requests)
        for step in range(start_step, self.limits.max_steps + 1):
            if (not progress or progress['phase'] not in {'receipt', 'remote_wait'}) and should_cancel is not None and should_cancel():
                return finish("cancelled", "运行已取消。", step - 1, input_tokens, output_tokens, measured_total_tokens, provider_requests)
            if progress and progress['phase'] in {'response', 'receipt', 'remote_wait'} and step == start_step:
                response = ModelResponse(**progress['response'])
            else:
                request_messages = (self.request_message_transform(transcript)
                    if self.request_message_transform is not None else transcript)
                reservation = None
                if self.usage_meter is not None:
                    try:
                        reservation = self.usage_meter.admit(
                            owner_id, self.model_key, run_id, attempt, step, now=float(self.clock()),
                        )
                    except UsageLimitExceeded:
                        return finish("failed", "模型预算已用尽。", step - 1, input_tokens, output_tokens, measured_total_tokens, provider_requests)
                persist('provider_pending', step)
                def observe_request(kind, data):
                    # Check before dispatch, but retain any text already received.
                    if kind == 'provider_request_started' and should_cancel is not None and should_cancel():
                        raise _RunCancelled()
                    event_sink(kind, {**data, 'step': step, 'attempt': attempt})
                    if kind in {'assistant_text_delta', 'provider_retry_scheduled'} and should_cancel is not None and should_cancel():
                        raise _RunCancelled()
                try:
                    response = self.provider.complete(request_messages, tools, request_observer=observe_request)
                except _RunCancelled:
                    if reservation is not None:
                        self._settle_unknown(reservation, run_id=run_id, step=step, attempt=attempt)
                    return finish('cancelled', '运行已取消。', step)
                except Exception as exc:
                    diagnostic = exc.diagnostic() if isinstance(exc, ProviderError) else {'provider_error': 'provider_exception'}
                    diagnostic.update({'step': step, 'attempt': attempt, 'phase': 'provider_call'})
                    if reservation is not None:
                        self._settle_unknown(reservation, run_id=run_id, step=step, attempt=attempt)
                    raise ProviderBoundaryUnknown(diagnostic=diagnostic) from exc
                try:
                    usage = normalize_usage(response.usage)
                    if reservation is not None:
                        self.usage_meter.settle(reservation, response.usage, "succeeded", now=float(self.clock()),
                                                metadata=getattr(response, "metadata", None))
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
                persist('response', step, response=asdict(response))
            # Publish the committed response before crossing any tool boundary.
            # The projection replaces by step, so checkpoint recovery is idempotent.
            event_sink('assistant_response', {
                'step': step, 'attempt': attempt,
                'text': response.text.encode('utf-8')[:24576].decode('utf-8', errors='ignore'),
                'response_kind': response.kind,
            })
            if (not progress or progress['phase'] not in {'receipt', 'remote_wait'}) and should_cancel is not None and should_cancel():
                return finish("cancelled", "运行已取消。", step, input_tokens, output_tokens, measured_total_tokens, provider_requests)
            if total_tokens > self.limits.max_tokens:
                return finish("failed", "模型预算已用尽。", step, input_tokens, output_tokens, measured_total_tokens, provider_requests)
            if response.kind == "final":
                return finish("succeeded", response.text[:32768], step, input_tokens, output_tokens, measured_total_tokens, provider_requests)
            if response.kind != "tool_call" or not response.tool:
                return finish("failed", "模型返回了不支持的响应。", step, input_tokens, output_tokens, measured_total_tokens, provider_requests)
            command_id = f"{run_id}:step:{step}"
            if progress and progress['phase'] == 'remote_wait' and step == start_step:
                receipt = self.broker.observe(command_id=command_id, tool=response.tool)
            elif progress and progress['phase'] == 'receipt' and step == start_step:
                receipt = ToolReceipt(**progress['receipt'])
            else:
                event_sink("tool_call", {
                    "run_id": run_id, "step": step, "command_id": command_id,
                    "tool": response.tool,
                    "argument_keys": sorted((response.arguments or {}).keys())[:32],
                })
                persist('tool_pending', step, response=asdict(response))
                receipt = self.broker.execute(
                    command_id=command_id, tool=response.tool,
                    arguments=response.arguments or {}, owner_id=owner_id, epoch=epoch,
                )
            if receipt.state == 'waiting_node':
                persist('remote_wait', step, response=asdict(response))
                return self._result('waiting_node', '', step, input_tokens, output_tokens, measured_total_tokens, provider_requests)
            persist('receipt', step, response=asdict(response), receipt=asdict(receipt))
            event_sink("tool_result", {
                "run_id": run_id, "step": step, "command_id": command_id,
                "tool": response.tool, "state": receipt.state,
                "error_code": receipt.error_code,
            })
            if receipt.state == "unknown":
                return finish("unknown", "远程工具回执未知，等待后续核查。", step, input_tokens, output_tokens, measured_total_tokens, provider_requests)
            if should_cancel is not None and should_cancel():
                return finish("cancelled", "运行已取消。", step, input_tokens, output_tokens, measured_total_tokens, provider_requests)
            if receipt.state != "succeeded":
                return finish("failed", "工具执行失败。", step, input_tokens, output_tokens, measured_total_tokens, provider_requests)
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
            persist('ready', step + 1)
            progress = None
        return finish("failed", "模型步骤已达到上限。", self.limits.max_steps, input_tokens, output_tokens, measured_total_tokens, provider_requests)

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
