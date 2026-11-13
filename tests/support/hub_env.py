"""Explicit hub stores rooted in a temp dir.

Replaces the removed module-level facades (``hub.state.STATE_DIR``,
``hub.events.EVENT_LOG``, ``hub.task_store.DB_PATH``): tests build the
repositories they seed/inspect on explicit paths and hand the same paths to
``make_app`` / ``FleetConfig`` so the app under test reads the same files.
"""
from pathlib import Path

from hub.application.event_publisher import EventPublisher
from hub.config import FleetConfig
from hub.infrastructure.event_repository import JsonlEventRepository
from hub.infrastructure.state_repository import JsonlObservationRepository
from hub.infrastructure.task_repository import SqliteTaskRepository


class HubStores:
    """Observation/task/event stores laid out like the old tests did.

    ``root`` doubles as the observation state dir; ``events.jsonl`` and
    ``fleet.db`` live inside it.
    """

    def __init__(self, root, *, init_tasks=True):
        self.root = Path(root)
        self.state_dir = self.root
        self.event_log = self.root / "events.jsonl"
        self.task_db = self.root / "fleet.db"
        self.store = JsonlObservationRepository(self.state_dir)
        self.tasks = SqliteTaskRepository(self.task_db)
        if init_tasks:
            self.tasks.init()

    def paths(self):
        return {
            "state_dir": self.state_dir,
            "event_log": self.event_log,
            "task_db": self.task_db,
        }

    def publisher(self):
        """A fresh publisher appending to this env's event log."""
        return EventPublisher(JsonlEventRepository(self.event_log))

    def config(self, root=None, **kwargs):
        """``FleetConfig.from_root`` bound to these store paths."""
        return FleetConfig.from_root(
            self.root if root is None else root, **self.paths(), **kwargs)

    def make_app(self, **kwargs):
        """``hub.web.make_app`` bound to these store paths."""
        from hub import web

        return web.make_app(**self.paths(), **kwargs)
