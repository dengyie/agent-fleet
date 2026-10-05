"""Owned lease renewal while a synchronous provider or tool is in flight."""
from threading import Event, Lock, Thread


class RunLeaseLost(RuntimeError):
    pass


class LeaseHeartbeat:
    def __init__(self, renew, *, interval, name):
        self.renew = renew
        self.interval = interval
        self.stopped = Event()
        self.lock = Lock()
        self.failure = None
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
                self.failure = exc
                self.stopped.set()
                raise RunLeaseLost('lease_mismatch') from exc

    def _loop(self):
        while not self.stopped.wait(self.interval):
            try:
                self.pulse()
            except RunLeaseLost:
                return

    def close(self):
        self.stopped.set()
        if self.thread.ident is not None:
            self.thread.join()
