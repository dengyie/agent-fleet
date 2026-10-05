"""One explicitly observed provider request, independent of response content."""
from __future__ import annotations

import time
from typing import Callable, TYPE_CHECKING

from tools.platform.request_metadata import RequestMetadata, RequestObserver, RequestParameters, normalized_tokens

if TYPE_CHECKING:
    from .openai_compatible import ProviderError


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
