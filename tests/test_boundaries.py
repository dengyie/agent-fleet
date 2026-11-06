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

- Package direction is ``hub -> fleet_core <- tools`` (``connectors -> tools``
  allowed): ``hub`` never imports ``tools``, ``tools``/``connectors`` never
  import ``hub``, ``fleet_core`` and the repository-root schema modules import
  none of ``hub``/``tools``/``connectors``. Checked over every import form
  (top-level, function-local, ``importlib.import_module``/``__import__`` string
  literals), and node entrypoints are proven importable with ``hub`` blocked.

Unknown 404/405/500 error conversion is deliberately out of scope here
(Task 11); this file only pins the boundary introduced by Phase 1.
"""
import ast
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

ROOT_MODULES = ("agent_profiles.py", "report_schema.py", "session_schema.py", "platform_schema.py")
# package (or root module) -> top-level packages it must never import
FORBIDDEN_IMPORTS = {
    "hub": {"tools"},
    "tools": {"hub"},
    "connectors": {"hub"},
    "fleet_core": {"hub", "tools", "connectors"},
    **{name: {"hub", "tools", "connectors"} for name in ROOT_MODULES},
}
# Node-side entrypoints launched by launchd / Windows tasks / operators.
NODE_ENTRY_MODULES = (
    "tools.browser_bridge.node",
    "tools.browser_bridge.muse_cli",
    "tools.session.sync",
    "tools.session.bridge",
    "tools.session.capture.hooks",
    "tools.supervisor.supervisor",
    "tools.supervisor.control_client",
    "tools.supervisor.control_client_cli",
    "tools.platform.node_runtime",
    "tools.platform.acceptance_check",
    "tools.platform.readiness_check",
    "tools.probe.discovery",
    "connectors",
)
NODE_ENTRY_SCRIPTS = ("tools/agent-runner.py", "tools/agent-self-report.py")


def _imported_modules(path):
    """Yield (lineno, absolute module name) for every import form in *path*."""
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                yield node.lineno, node.module
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if (name in ("import_module", "__import__") and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                yield node.lineno, node.args[0].value


def _cross_package_violations():
    violations = []
    for owner, forbidden in FORBIDDEN_IMPORTS.items():
        base = REPO_ROOT / owner
        files = [base] if base.is_file() else sorted(base.rglob("*.py"))
        assert files, owner
        for path in files:
            for lineno, module in _imported_modules(path):
                if module.split(".")[0] in forbidden:
                    violations.append(f"{path.relative_to(REPO_ROOT)}:{lineno} imports {module}")
    return violations


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


class PackageDirectionTests(unittest.TestCase):
    def test_no_cross_imports_between_hub_tools_and_fleet_core(self):
        self.assertEqual(_cross_package_violations(), [])

    def test_import_scanner_sees_function_local_and_importlib_forms(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            sample = Path(td) / "sample.py"
            sample.write_text(
                "def f():\n    from hub.x import y\n"
                "import importlib\nimportlib.import_module('tools.z')\n__import__('hub')\n")
            found = {module for _, module in _imported_modules(sample)}
        self.assertTrue({"hub.x", "tools.z", "hub", "importlib"} <= found)

    def test_fleet_core_is_a_regular_package(self):
        self.assertTrue((REPO_ROOT / "fleet_core" / "__init__.py").is_file())
        for directory in (REPO_ROOT / "fleet_core").rglob("*"):
            if directory.is_dir() and directory.name != "__pycache__":
                self.assertTrue((directory / "__init__.py").is_file(), directory)

    def test_node_entrypoints_import_without_hub_code(self):
        probe = (
            "import importlib, importlib.util, sys\n"
            "sys.modules['hub'] = None\n"
            "for name in sys.argv[1].split(','):\n"
            "    importlib.import_module(name)\n"
            "for index, script in enumerate(sys.argv[2].split(',')):\n"
            "    spec = importlib.util.spec_from_file_location(f'_node_entry_{index}', script)\n"
            "    module = importlib.util.module_from_spec(spec)\n"
            "    spec.loader.exec_module(module)\n"
            "leaked = sorted(m for m in sys.modules if m == 'hub' or m.startswith('hub.'))\n"
            "assert leaked == ['hub'], leaked\n"
            "print('ok')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe, ",".join(NODE_ENTRY_MODULES),
             ",".join(str(REPO_ROOT / script) for script in NODE_ENTRY_SCRIPTS)],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-4000:])
        self.assertEqual(proc.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
