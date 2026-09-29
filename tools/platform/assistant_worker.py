"""Run worker adapters for the platform assistant.

The database adapter deliberately treats an interrupted model/tool execution as
``unknown``. A worker cannot infer that an external side effect did not happen,
so it must never turn an exception into a successful Run or blindly replay it.
"""
from __future__ import annotations

from .runtime.native import NativeAssistantRuntime


class AssistantWorker:
    def __init__(self, runtime, event_sink=None):
        self.runtime = runtime
        self.event_sink = event_sink or (lambda kind, payload: None)

    def execute(self, *, run_id, owner_id, epoch, messages, tools, should_cancel=None):
        self.event_sink("run_started", {"run_id": run_id})
        result = self.runtime.run(
            run_id=run_id, owner_id=owner_id, epoch=epoch,
            messages=messages, tools=tools, event_sink=self.event_sink,
            should_cancel=should_cancel,
        )
        self.event_sink("run_finished", {"run_id": run_id, "state": result.state, "steps": result.steps})
        return result


class PersistentAssistantWorker(AssistantWorker):
    """Persist worker lifecycle and private events through RunEventService."""

    def __init__(self, runtime, event_service):
        self.event_service = event_service
        super().__init__(runtime, self._append_event)
        self._run_id = None
        self._owner_id = None

    def _append_event(self, kind, payload):
        if self._run_id is None or self._owner_id is None:
            return
        self.event_service.append(self._owner_id, self._run_id, kind, payload,
                                  lease_id=getattr(self, "_lease_id", None),
                                  worker_id=getattr(self, "_worker_id", None))

    def execute(self, *, run_id, owner_id, epoch, messages, tools, should_cancel=None, lease_id=None, worker_id=None, attempt=1):
        self._run_id = run_id
        self._owner_id = owner_id
        self._lease_id = lease_id
        self._worker_id = worker_id
        try:
            if lease_id is None:
                self.event_service.state(owner_id, run_id, "running")
            self._append_event("run_started", {"run_id": run_id})
            result = self.runtime.run(
                run_id=run_id, owner_id=owner_id, epoch=epoch,
                messages=messages, tools=tools, event_sink=self._append_event,
                should_cancel=should_cancel, attempt=attempt,
            )
            # The full answer belongs to runs.result_text. Lifecycle events
            # carry metadata only, independent of UTF-8/JSON expansion.
            self._append_event("run_finished", {
                "run_id": run_id, "state": result.state,
                "steps": result.steps,
                "usage": result.usage.as_dict(),
            })
            if lease_id is None:
                self.event_service.state(
                    owner_id, run_id, result.state, result_text=result.text,
                    usage=result.usage.as_dict(),
                )
            return result
        except Exception:
            try:
                if lease_id is None:
                    self._append_event("run_unknown", {"run_id": run_id})
                    usage = None
                    meter = getattr(self.runtime, "usage_meter", None)
                    if meter is not None:
                        try:
                            usage = meter.get_run_usage(owner_id, run_id)
                        except Exception:
                            usage = None
                    self.event_service.state(owner_id, run_id, "unknown", usage=usage)
            except Exception:
                pass
            raise
        finally:
            self._run_id = None
            self._owner_id = None
            self._lease_id = None
            self._worker_id = None


__all__ = ["AssistantWorker", "PersistentAssistantWorker"]
