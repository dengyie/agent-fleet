"""Keep bounded synchronous I/O owned by its caller through cancellation."""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Coroutine, ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")


async def run_owned_io(operation: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    """Join the worker before releasing resources, then propagate cancellation.

    Only use for operations with their own bounds. Cancelling a thread await
    cannot stop the underlying file/DB/process operation; even repeated caller
    cancellation must wait for it. A worker failure remains the cancellation's
    cause, so shutdown never loses an I/O error.
    """
    worker = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    cancelled: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(worker)
        except asyncio.CancelledError as exc:
            if worker.cancelled():
                if cancelled is not None:
                    raise cancelled from exc
                raise
            cancelled = cancelled or exc
            continue
        except BaseException as exc:
            if cancelled is not None:
                raise cancelled from exc
            raise
        if cancelled is not None:
            raise cancelled
        return result


async def wait_owned(operation: Coroutine[Any, Any, T], *, timeout: float) -> T:
    """Cancel an async I/O owner once, then join it despite repeated cancellation.

    asyncio.wait never cancels its child. Keeping that task explicitly avoids
    Python 3.10 wait_for's cancellable cancellation-join window. The operation
    must itself own any bounded synchronous work (for example run_owned_io).
    """
    worker = asyncio.create_task(operation)
    try:
        done, _pending = await asyncio.wait((worker,), timeout=timeout)
    except BaseException as exc:
        failure = exc
    else:
        if done:
            return worker.result()
        failure = asyncio.TimeoutError()
    worker.cancel()
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError as exc:
            if not worker.done() and not isinstance(failure, asyncio.CancelledError):
                failure = exc
        except BaseException as exc:
            raise failure from exc
    try:
        worker.result()
    except BaseException as worker_error:
        raise failure from worker_error
    raise failure
