"""Static source-boundary gates for the Phase 1 backend split.

These are push-only, source-level assertions (no app boot required for most of
them) that lock down the dependency direction established across the Phase 1
refactor:

- ``hub/http`` adapters are transport-only: the literal legacy module-level
  storage constants must not appear anywhere under ``hub/http``.
- ``hub.domain`` / ``hub.application`` / ``hub.infrastructure`` stay
  Flask-independent: none of them may import Flask.
- The legacy entrypoint ``hub.web.make_app`` still delegates to
  ``hub.bootstrap.create_app``.
- The compatibility facades (``state.save_snapshot``, ``events.emit``,
  ``task_store.create_task``, ``web.make_app``) remain callable.

Unknown 404/405/500 error conversion is deliberately out of scope here
(Task 11); this file only pins the boundary introduced by Phase 1.
"""
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


class BoundaryTests(unittest.TestCase):
    def test_http_adapters_do_not_reference_storage_globals(self):
        for path in sorted((REPO_ROOT / "hub" / "http").glob("*.py")):
            source = path.read_text()
            self.assertNotIn("STATE_DIR", source, path.name)
            self.assertNotIn("DB_PATH", source, path.name)
            self.assertNotIn("EVENT_LOG", source, path.name)

    def test_legacy_entrypoint_uses_bootstrap(self):
        source = (REPO_ROOT / "hub" / "web.py").read_text()
        self.assertIn("create_app", source)
        self.assertIn("from hub.bootstrap import create_app", source)

    def test_domain_application_infrastructure_are_flask_independent(self):
        for layer in ("domain", "application", "infrastructure"):
            layer_dir = REPO_ROOT / "hub" / layer
            for path in sorted(layer_dir.glob("*.py")):
                source = path.read_text()
                self.assertNotIn("import flask", source, path.name)
                self.assertNotIn("from flask", source, path.name)

    def test_legacy_facades_remain_callable(self):
        from hub import events, state, task_store, web

        self.assertTrue(callable(web.make_app))
        self.assertTrue(callable(state.save_snapshot))
        self.assertTrue(callable(events.emit))
        self.assertTrue(callable(task_store.create_task))


if __name__ == "__main__":
    unittest.main()
