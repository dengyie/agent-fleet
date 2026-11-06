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
- The legacy module-level storage facades (``hub.state`` / ``hub.events`` /
  ``hub.task_store``) are gone: stores are built from explicit paths by the
  composition root (``hub.bootstrap``) or the ``hub/scan.py`` CLI, and no
  ``hub`` module reassigns another module's path globals.

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

    def test_legacy_storage_facades_removed(self):
        import importlib
        import inspect

        from hub import web

        for name in ("hub.state", "hub.events", "hub.task_store"):
            with self.assertRaises(ModuleNotFoundError, msg=name):
                importlib.import_module(name)
        params = inspect.signature(web.make_app).parameters
        for name in ("state_dir", "event_log", "task_db", "publisher"):
            self.assertIn(name, params)
        for path in sorted((REPO_ROOT / "hub").rglob("*.py")):
            source = path.read_text()
            for name in ("STATE_DIR", "EVENT_LOG", "DB_PATH"):
                self.assertNotRegex(
                    source, r"\.\s*" + name + r"\s*=(?!=)",
                    f"{path.relative_to(REPO_ROOT)} reassigns a module global")
            self.assertNotIn("set_publisher", source, path.name)
            self.assertNotIn("_wire_legacy_paths", source, path.name)


if __name__ == "__main__":
    unittest.main()
