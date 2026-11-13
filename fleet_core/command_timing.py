"""Bounded queue, execution and receipt budgets shared by Hub and Node.

The default receipt allowance includes Transport's nominal two-route budget:
three attempts per route, 15-second socket timeout and four 0.4-second backoffs
(91.6 seconds). A socket timeout is not a total wall-clock network deadline;
uncertain work still settles at the persisted Hub receipt deadline.
Three requests cover screenshot ticket, content upload and final receipt. The
receipt allowance includes that result materialization after tool execution.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

MAX_COMMAND_LIFETIME_S = 3600.0
DEFAULT_TRANSPORT_BUDGET_S = 2 * 3 * 15.0 + 2 * 2 * 0.4


def _positive(value: float) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError("invalid_command_timing")
    return float(value)


@dataclass(frozen=True)
class CommandTiming:
    queue_deadline: float
    execution_timeout_s: float
    receipt_timeout_s: float

    def __post_init__(self):
        for value in (self.queue_deadline, self.execution_timeout_s, self.receipt_timeout_s):
            _positive(value)
        if (self.execution_timeout_s + self.receipt_timeout_s > MAX_COMMAND_LIFETIME_S
                or not math.isfinite(self.hard_deadline)):
            raise ValueError("invalid_command_timing")

    @property
    def hard_deadline(self) -> float:
        return self.queue_deadline + self.execution_timeout_s + self.receipt_timeout_s

    def bounded_deadline(self, *, now: float, deadline: float) -> CommandTiming:
        """Fit all phases inside an earlier admission limit, never extend it.

        Preserve the relative phase allowances when a Workflow's remaining
        lifetime is shorter than the ordinary command policy. Expired queue
        budgets cannot be renewed by preparing a command again.
        """
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
            raise ValueError("invalid_command_timing")
        deadline = _positive(deadline)
        if self.queue_deadline <= now or deadline <= now:
            raise ValueError("invalid_command_timing")
        if deadline >= self.hard_deadline:
            return self
        scale = (deadline - now) / (self.hard_deadline - now)
        queue = now + (self.queue_deadline - now) * scale
        execution = self.execution_timeout_s * scale
        return CommandTiming(queue, execution, deadline - (queue + execution))


@dataclass(frozen=True)
class CommandTimingPolicy:
    queue_timeout_s: float = 30.0
    execution_timeout_s: float = 30.0
    receipt_timeout_s: float = 300.0

    def __post_init__(self):
        for value in (self.queue_timeout_s, self.execution_timeout_s, self.receipt_timeout_s):
            _positive(value)
        if self.total_timeout_s > MAX_COMMAND_LIFETIME_S:
            raise ValueError("invalid_command_timing")

    @property
    def total_timeout_s(self) -> float:
        return self.queue_timeout_s + self.execution_timeout_s + self.receipt_timeout_s

    def start(self, now: float) -> CommandTiming:
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
            raise ValueError("invalid_command_timing")
        return CommandTiming(now + self.queue_timeout_s, self.execution_timeout_s, self.receipt_timeout_s)


__all__ = ["CommandTiming", "CommandTimingPolicy", "MAX_COMMAND_LIFETIME_S", "DEFAULT_TRANSPORT_BUDGET_S"]
