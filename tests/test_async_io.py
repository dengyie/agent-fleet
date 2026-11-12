"""Cancellation must retain ownership and the underlying exception chain."""
from __future__ import annotations

import asyncio
import threading

import pytest

from fleet_core.async_io import run_owned_io, wait_owned


def test_owned_io_preserves_results_and_worker_errors() -> None:
    assert asyncio.run(run_owned_io(pow, 2, exp=4)) == 16
    with pytest.raises(ZeroDivisionError):
        asyncio.run(run_owned_io(divmod, 1, 0))


def test_owned_io_preserves_callable_operation_keyword() -> None:
    def lookup(*, operation: str) -> str:
        return operation

    assert asyncio.run(run_owned_io(lookup, operation="test")) == "test"


def test_repeated_cancellation_joins_worker_and_preserves_its_failure() -> None:
    started, release = threading.Event(), threading.Event()
    failure = OSError("publication failed")

    def publish() -> None:
        started.set()
        assert release.wait(5)
        raise failure

    async def scenario() -> None:
        task = asyncio.create_task(run_owned_io(publish))
        try:
            for _ in range(500):
                if started.is_set():
                    break
                await asyncio.sleep(.01)
            assert started.is_set()
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(.01)
                assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        # Python 3.10 Task adds a cancellation wrapper via __context__;
        # newer versions propagate the original cancellation directly.
        error: BaseException | None = caught.value
        seen: set[int] = set()
        while error is not None and id(error) not in seen and error is not failure:
            seen.add(id(error))
            error = error.__cause__ or error.__context__
        assert error is failure

    asyncio.run(scenario())


@pytest.mark.parametrize("timeout_first", [False, True])
def test_async_boundary_joins_owned_worker_through_cancel_and_timeout(timeout_first: bool) -> None:
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    failure = OSError("bounded I/O failed")

    def blocked() -> None:
        entered.set()
        try:
            if not release.wait(5):
                raise TimeoutError("test release missing")
            raise failure
        finally:
            finished.set()

    async def operation() -> None:
        await run_owned_io(blocked)

    async def scenario() -> None:
        task = asyncio.create_task(wait_owned(operation(), timeout=.02 if timeout_first else 5))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            if timeout_first:
                await asyncio.sleep(.04)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(.01)
                assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert finished.is_set()
        error: BaseException | None = caught.value
        seen: set[int] = set()
        while error is not None and id(error) not in seen and error is not failure:
            seen.add(id(error))
            error = error.__cause__ or error.__context__
        assert error is failure

    asyncio.run(scenario())


def test_async_boundary_returns_results_and_preserves_timeout_cause() -> None:
    async def result() -> int:
        return 42

    async def slow() -> None:
        await asyncio.sleep(5)

    failure = OSError("original worker failure")

    async def failed() -> None:
        raise failure

    assert asyncio.run(wait_owned(result(), timeout=1)) == 42
    with pytest.raises(OSError) as caught:
        asyncio.run(wait_owned(failed(), timeout=1))
    assert caught.value is failure and caught.value.__cause__ is None
    with pytest.raises(asyncio.TimeoutError) as error:
        asyncio.run(wait_owned(slow(), timeout=.01))
    assert isinstance(error.value.__cause__, asyncio.CancelledError)
