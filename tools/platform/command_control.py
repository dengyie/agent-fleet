"""Owned, bounded control observations for one Node command execution."""
from __future__ import annotations

import math
from threading import Event, Thread
import time


class ControlRejected(RuntimeError):
    """A definitive ownership/control rejection, rather than transport loss."""


class CommandControl:
    def __init__(self, command, *, renew=None, should_stop=None, clock=time.time,
                 lease_s=15.0, monotonic=time.monotonic):
        self.command_id = command['command_id']
        self.expires_at = float(command['expires_at'])
        if not math.isfinite(self.expires_at):
            raise ControlRejected('invalid_command_expiry')
        self.renew, self.should_stop = renew, should_stop
        self.clock, self.monotonic = clock, monotonic
        self.execution_until = monotonic() + max(0.0, self.expires_at - clock())
        self.lease_until = None
        self.interval = max(.05, min(1.0, float(lease_s) / 3))
        self.closed, self.cancelled = Event(), Event()
        self.failure = None
        self.thread = Thread(target=self._loop, name='node-command-control-' + self.command_id, daemon=True)

    def should_cancel(self):
        if self.should_stop is not None and self.should_stop():
            return True
        now = self.monotonic()
        return (self.cancelled.is_set() or now >= self.execution_until
                or (self.lease_until is not None and now >= self.lease_until))

    def start(self):
        if self.renew is not None and not self.should_cancel():
            self._pulse(initial=True)
            if not self.should_cancel():
                self.thread.start()
        return self

    def _accept(self, control, *, started):
        if (not isinstance(control, dict) or control.get('command_id') != self.command_id
                or type(control.get('continue')) is not bool):
            raise ControlRejected('invalid_command_control')
        if not control['continue']:
            self.cancelled.set()
            return
        deadline, duration = control.get('execution_deadline'), control.get('valid_for_s')
        if (control.get('status') not in {'leased', 'accepted', 'running'}
                or type(deadline) not in (int, float) or not math.isfinite(deadline)
                or type(duration) not in (int, float) or not math.isfinite(duration)
                or not 0 < duration <= 3600 or deadline > self.expires_at):
            raise ControlRejected('invalid_command_control')
        now = self.monotonic()
        # Wall-clock adjustment or renewal must never buy more execution time.
        self.execution_until = min(self.execution_until, now + max(0.0, deadline - self.clock()))
        # Lease duration was measured by the Hub during this request. Charge
        # the entire round trip against it instead of starting a new duration
        # when an old response finally arrives.
        self.lease_until = min(started + duration, self.execution_until)

    def _pulse(self, *, initial=False):
        try:
            started = self.monotonic()
            control = self.renew(self.closed.is_set)
            self._accept(control, started=started)
            self.failure = None
        except Exception as exc:
            if self.closed.is_set():
                return
            self.failure = exc
            if initial or isinstance(exc, ControlRejected):
                self.cancelled.set()
            # Transient failures retry within the last confirmed lease.
            # should_cancel enforces that monotonic boundary independently.

    def _loop(self):
        while not self.closed.wait(min(.1, self.interval) if self.failure else self.interval):
            try:
                if self.should_cancel():
                    return
                self._pulse()
            except Exception as exc:
                self.failure = exc
                self.cancelled.set()
                return

    def close(self):
        self.closed.set()
        if self.thread.ident is not None:
            # The injected renewal uses request_bounded_json with an owned
            # cancellation callback and a fixed two-second transport deadline.
            self.thread.join()

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()
