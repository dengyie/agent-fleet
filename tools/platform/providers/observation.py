"""One explicitly observed provider request, independent of response content."""
import time

from tools.platform.request_metadata import normalized_tokens


class RequestObservation:
    def __init__(self, observer, *, model, provider, index=1, parameters=None,
                 clock=time.time, monotonic=time.monotonic):
        self.observer = observer
        self.monotonic = monotonic
        self.data = {'request_index': index, 'requested_model': model, 'model': model,
                     'provider': provider, 'started_at': clock(), 'duration_ms': None,
                     'status': 'running', 'usage': normalized_tokens(None), 'cost': None,
                     'parameters': parameters or {}}
        self.started = monotonic()
        if observer is not None:
            observer('provider_request_started', dict(self.data))

    def finish(self, *, metadata=None, error=None, http_status=None):
        self.data.update(metadata or {})
        self.data.update(duration_ms=max(0, round((self.monotonic() - self.started) * 1000)),
                         status='succeeded' if error is None else 'failed', http_status=http_status)
        if error is not None:
            self.data.update(error.diagnostic())
            if http_status is None or 200 <= http_status < 300:
                self.data['status'] = 'unknown'
        if self.observer is not None:
            self.observer('provider_request_finished', dict(self.data))
