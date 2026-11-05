"""Owned lease renewal while a synchronous provider or tool is in flight.

Only a *fencing* failure (``lease_mismatch``: another owner holds the lease)
is fatal immediately. Any other renewal error (store briefly locked, I/O
hiccup) is transient: it is retried, faster than the normal cadence, until the
lease could actually have expired (``ttl`` since the last successful renewal);
only then is the lease treated as lost.
"""
import time
from threading import Event, Lock, Thread


class RunLeaseLost(RuntimeError):
    pass


def _is_fencing_error(exc: BaseException) -> bool:
    # RunLeaseLost and ResourceLeaseError("lease_mismatch") both signal that
    # ownership moved; retrying cannot win the lease back.
    return isinstance(exc, RunLeaseLost) or str(exc) == 'lease_mismatch'


class LeaseHeartbeat:
    def __init__(self, renew, *, interval, name, ttl=None, clock=time.monotonic):
        self.renew = renew
        self.interval = interval
        # Callers renew every lease_s/3, so the lease itself lasts 3 intervals.
        self.ttl = interval * 3 if ttl is None else ttl
        self.clock = clock
        self.stopped = Event()
        self.lock = Lock()
        self.failure = None
        self.transient_failure = None
        self.last_success = None
        self.thread = Thread(target=self._loop, name='run-lease-' + name, daemon=True)

    def start(self):
        self.pulse()
        self.thread.start()

    def pulse(self):
        # Synchronous boundary checks and the heartbeat share one renewal order.
        with self.lock:
            if self.failure is not None:
                raise RunLeaseLost('lease_mismatch') from self.failure
            try:
                self.renew()
            except Exception as exc:
                now = self.clock()
                expired = (self.last_success is None
                           or now - self.last_success >= self.ttl)
                if _is_fencing_error(exc) or expired:
                    self.failure = exc
                    self.stopped.set()
                    raise RunLeaseLost('lease_mismatch') from exc
                # Transient: keep ownership, retry sooner from the loop.
                self.transient_failure = exc
                return
            self.last_success = self.clock()
            self.transient_failure = None

    def _next_wait(self):
        if self.transient_failure is None:
            return self.interval
        return max(min(self.interval / 4, 1.0), 0.001)

    def _loop(self):
        while not self.stopped.wait(self._next_wait()):
            try:
                self.pulse()
            except RunLeaseLost:
                return

    def close(self):
        self.stopped.set()
        if self.thread.ident is not None:
            self.thread.join()
