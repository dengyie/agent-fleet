"""One explicitly observed provider request, independent of response content."""
from __future__ import annotations

import time
from typing import Callable, TYPE_CHECKING

from fleet_core.request_metadata import RequestMetadata, RequestObserver, RequestParameters, normalized_tokens

if TYPE_CHECKING:
    from .openai_compatible import ProviderError


class TextObservation:
    """Public narration and phase signals; never retain raw model reasoning."""
    def __init__(self, observer, index):
        self.observer, self.index = observer, index
        self.offset, self.bytes = 0, 0
        self.pending = ''
        self.last = time.monotonic()
        self.phase = None
        self.last_activity = self.last

    def activity(self, phase):
        if self.observer is None:
            return
        now = time.monotonic()
        if phase != self.phase or now - self.last_activity >= 1.0:
            self.observer('assistant_activity', {'phase': phase, 'request_index': self.index})
            self.phase, self.last_activity = phase, now

    def append(self, text):
        if self.observer is None:
            return
        text = text.encode('utf-8')[:max(0, 24576 - self.bytes)].decode('utf-8', errors='ignore')
        if not text:
            return
        self.bytes += len(text.encode('utf-8'))
        self.pending += text
        if self.offset == 0 or len(self.pending) >= 256 or time.monotonic() - self.last >= .05:
            self.flush()
        self.activity('responding')

    def flush(self):
        while self.pending:
            text, self.pending = self.pending[:1024], self.pending[1024:]
            self.observer('assistant_text_delta', {
                'text': text, 'offset': self.offset, 'request_index': self.index})
            self.offset += len(text)
        self.last = time.monotonic()


class RequestObservation:
    def __init__(self, observer: RequestObserver | None, *, model: str, provider: str,
                 index: int = 1, parameters: RequestParameters | None = None,
                 clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.observer = observer
        self.monotonic = monotonic
        self.data: RequestMetadata = {'request_index': index, 'requested_model': model, 'model': model,
                     'provider': provider, 'started_at': clock(), 'duration_ms': None,
                     'status': 'running', 'usage': normalized_tokens(None), 'cost': None,
                     'parameters': parameters or {}}
        self.started = monotonic()
        if observer is not None:
            observer('provider_request_started', self.data.copy())

    def finish(self, *, metadata: RequestMetadata | None = None,
               error: ProviderError | None = None, http_status: int | None = None) -> None:
        self.data.update(metadata or {})
        self.data.update(duration_ms=max(0, round((self.monotonic() - self.started) * 1000)),
                         status='succeeded' if error is None else 'failed', http_status=http_status)
        if error is not None:
            self.data.update(error.diagnostic())
            if http_status is None or 200 <= http_status < 300:
                self.data['status'] = 'unknown'
        if self.observer is not None:
            self.observer('provider_request_finished', self.data.copy())
