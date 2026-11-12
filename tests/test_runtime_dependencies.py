"""Boot the real Hub without access to undeclared test-only distributions."""
from __future__ import annotations

import ast
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = """
import importlib.abc
import json
from pathlib import Path
import sys

root, state = map(Path, sys.argv[1:])
policy = json.loads(sys.stdin.read())
allowed, third_party = set(policy['allowed']), set(policy['third_party'])

class RuntimeOnly(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        top_level = fullname.split('.', 1)[0]
        if top_level in third_party and top_level not in allowed:
            raise ModuleNotFoundError('undeclared_runtime_dependency:' + top_level)
        return None

sys.meta_path.insert(0, RuntimeOnly())
sys.path.extend(policy['import_paths'])
sys.path.insert(0, str(root))
from hub.bootstrap import create_app
from hub.config import FleetConfig

app = create_app(FleetConfig.from_root(state, platform_enabled=True,
    platform_workflows_enabled=True, platform_fleet_mcp_enabled=True,
    platform_external_agents_enabled=True, dev_operator='runtime@example.test',
    serve_frontend=False))
assert app.extensions['fleet']['services']['workflow_coordinator'] is not None
response = app.test_client().get('/api/platform/v1/workflows')
assert response.status_code == 200 and response.get_json()['workflows'] == []
from hub.http.fleet_mcp_routes import bp
from hub.application.workflow_external_outputs import WorkflowExternalOutputs
assert bp is not None and WorkflowExternalOutputs is not None
assert 'jsonschema' in sys.modules
print(json.dumps({'workflow_status': response.status_code, 'schema_validator_loaded': True}))
"""


def test_workflow_and_fleet_mcp_boot_with_only_declared_runtime_dependencies(tmp_path: Path) -> None:
    pending = [Requirement(line) for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
               if line.strip() and not line.lstrip().startswith("#")]
    distributions: set[str] = set()
    while pending:
        requirement = pending.pop()
        if requirement.marker is not None and not requirement.marker.evaluate({"extra": ""}):
            continue
        name = canonicalize_name(requirement.name)
        if name in distributions:
            continue
        distributions.add(name)
        try:
            metadata = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            # A missing package still fails naturally if bootstrap imports it;
            # the guard only hides installed, undeclared test dependencies.
            continue
        pending.extend(Requirement(raw) for raw in metadata.requires or ())
    packages = importlib.metadata.packages_distributions()
    allowed = [module for module, owners in packages.items()
               if any(canonicalize_name(owner) in distributions for owner in owners)]
    import_paths = [path for path in sys.path if Path(path).name in {"site-packages", "dist-packages"}]
    result = subprocess.run([sys.executable, "-I", "-c", BOOTSTRAP, str(ROOT), str(tmp_path)],
                            input=json.dumps({"allowed": allowed, "third_party": list(packages),
                                              "import_paths": import_paths}),
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {"workflow_status": 200, "schema_validator_loaded": True}


DEPLOYMENT_CHECK = """
import importlib.abc
import json
from pathlib import Path
import runpy
import sys

root, missing, import_paths, action = sys.argv[1:]
sys.path.extend(json.loads(import_paths))

class MissingDependency(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.', 1)[0] == missing:
            raise ModuleNotFoundError('dependency-unavailable-private-detail', name=fullname)
        return None

sys.meta_path.insert(0, MissingDependency())
helper = str(Path(root) / 'deploy' / 'auto_deploy_container.py')
if action == 'check':
    namespace = runpy.run_path(helper)
    try:
        namespace['verify_runtime_dependencies']()
    except RuntimeError as exc:
        assert isinstance(exc.__cause__, ModuleNotFoundError)
        assert exc.__cause__.name == missing, repr(exc.__cause__)
        print('missing:' + missing)
    else:
        raise AssertionError('dependency was not checked: ' + missing)
    sys.exit(0)
runpy.run_path(helper, run_name='__main__')
"""


def _site_paths() -> str:
    return json.dumps([path for path in sys.path if Path(path).name in {"site-packages", "dist-packages"}])


def test_deployment_checks_each_direct_runtime_dependency() -> None:
    declared = {
        canonicalize_name(Requirement(line).name)
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    tree = ast.parse((ROOT / "deploy/auto_deploy_container.py").read_text(encoding="utf-8"))
    check = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name == "verify_runtime_dependencies")
    modules = {alias.name for node in ast.walk(check) if isinstance(node, ast.Import) for alias in node.names}
    package_names = {"yaml": "pyyaml", "websocket": "websocket-client"}
    checked = {canonicalize_name(package_names.get(module, module)) for module in modules}
    assert checked == declared


@pytest.mark.parametrize("missing", ["cryptography", "flask", "websocket", "yaml", "jsonschema"])
def test_deployment_runtime_check_identifies_missing_dependency(missing: str) -> None:
    result = subprocess.run(
        [sys.executable, "-I", "-c", DEPLOYMENT_CHECK, str(ROOT), missing, _site_paths(), "check"],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "missing:" + missing


@pytest.mark.parametrize("missing", ["cryptography", "flask", "websocket", "yaml", "jsonschema"])
@pytest.mark.parametrize("mode", ["preflight", "stage", "verify"])
def test_deployment_rejects_missing_runtime_before_accessing_live(tmp_path: Path, missing: str, mode: str) -> None:
    # No release import, live database access, process stop or probe may precede
    # the dependency rejection. The nonexistent live/stage paths expose bypasses.
    result = subprocess.run(
        [sys.executable, "-I", "-c", DEPLOYMENT_CHECK, str(ROOT), missing, _site_paths(), "deploy"],
        input=json.dumps({"mode": mode, "live": str(tmp_path / "live"), "stage": str(tmp_path / "stage")}),
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert json.loads(result.stdout) == {"status": "failed", "error": "RuntimeError"}
    assert result.stderr == ""
    assert list(tmp_path.iterdir()) == []


def test_deployment_runtime_check_loads_schema_validator() -> None:
    script = """
import runpy
import json
import sys
sys.path.extend(json.loads(sys.argv[2]))
helper = runpy.run_path(sys.argv[1])
helper['verify_runtime_dependencies']()
assert 'jsonschema' in sys.modules
print('runtime-ready')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(ROOT / "deploy/auto_deploy_container.py"), _site_paths()],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "runtime-ready"
