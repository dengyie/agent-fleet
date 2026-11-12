"""The deployable launcher reuses the registered Linux fixed-test recipe."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

from fleet_core.backends.sandbox import SandboxBackend
from fleet_core.workflow.validators import FixedTestRunner
from test_workflow_repository import OWNER, WORKSPACE
from test_workflow_test_execution import ExecutionFixture


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "fleet_core/workflow/linux_launcher.py"
RECIPE = [sys.executable, "-m", "pytest", "-q", "--junitxml=.fleet-workflow-results.xml"]


def test_deployable_linux_launcher_help_works_with_isolated_python(tmp_path: Path) -> None:
    result = subprocess.run([sys.executable, "-I", str(LAUNCHER), "--fixed-test", "--help"],
        cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "--bwrap" in result.stdout
    assert list(tmp_path.iterdir()) == []


def test_fixed_test_shortcut_derives_runtime_cwd_and_junit(tmp_path: Path, monkeypatch) -> None:
    from fleet_core.workflow.linux_launcher import _arguments, _fixed_test_arguments

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda name: "/installed/bwrap" if name == "bwrap" else None)
    options, command = _arguments(_fixed_test_arguments(["--", *RECIPE]))
    assert command == RECIPE
    assert options == {"--mode": "test", "--bwrap": "/installed/bwrap",
        "--base": str(Path(sys.base_prefix).resolve()), "--venv": str(Path(sys.prefix).resolve()),
        "--python": str(Path(sys.executable).absolute()), "--source": str(tmp_path.resolve()),
        "--junit": str(tmp_path.resolve() / ".fleet-workflow-results.xml")}
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("command", [
    [sys.executable, "-c", "raise SystemExit(0)"],
    [sys.executable, "-m", "pytest", "-q", "--junitxml=other.xml"],
    [sys.executable, "-m", "pytest", "-q", "--junitxml=.fleet-workflow-results.xml", "extra.py"],
])
def test_launcher_rejects_other_recipes_before_invoking_bwrap(tmp_path: Path, command: list[str]) -> None:
    fake = tmp_path / "bwrap"
    fake.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    fake.chmod(0o700)
    result = subprocess.run([sys.executable, "-I", str(LAUNCHER), "--fixed-test", "--bwrap", str(fake), "--", *command],
        cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "fixed-test command does not match the registered recipe" in result.stderr
    assert not (tmp_path / ".fleet-workflow-results.xml").exists()


def test_launcher_missing_bwrap_fails_before_source_mutation(tmp_path: Path) -> None:
    result = subprocess.run([sys.executable, "-I", str(LAUNCHER), "--fixed-test", "--bwrap",
        str(tmp_path / "missing-bwrap"), "--", *RECIPE], cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "bubblewrap executable is invalid" in result.stderr
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("options", [[], ["--bwrap", "relative"], ["--bwrap", ""]])
def test_fixed_test_shortcut_requires_installed_absolute_bwrap(tmp_path: Path, monkeypatch, options: list[str]) -> None:
    from fleet_core.workflow.linux_launcher import _fixed_test_arguments

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(ValueError, match="absolute installed bubblewrap"):
        _fixed_test_arguments([*options, "--", *RECIPE])
    assert list(tmp_path.iterdir()) == []


def test_deployable_launcher_argv_wires_into_original_hub_and_node(tmp_path: Path) -> None:
    from hub.bootstrap import create_app
    from hub.config import FleetConfig
    from hub.infrastructure.platform_db import PlatformRepository
    from test_node_workflow_startup import node_configuration
    from test_workflow_startup import registration, seed
    from tools.plugins.node import load_runtime

    launcher = [sys.executable, "-I", str(LAUNCHER), "--fixed-test", "--bwrap", "/installed/bwrap", "--"]
    manifest = tmp_path / "registration.json"
    manifest.write_text(json.dumps(registration(OWNER, WORKSPACE)), encoding="utf-8")
    config = FleetConfig.from_root(tmp_path / "hub", platform_enabled=True, platform_workflows_enabled=True,
        platform_workflow_registration_file=manifest, platform_sandbox_launcher=launcher,
        dev_operator=OWNER, serve_frontend=False)
    seed(PlatformRepository(config.platform_db), tmp_path)
    app = create_app(config)
    runner = app.extensions["fleet"]["services"]["workflow_test_executor"].runner
    assert runner.backend_factory(tmp_path).launcher == tuple(launcher)
    node = tmp_path / "node"
    node.mkdir()
    path, value = node_configuration(node)
    value["workflow_test"]["sandbox_launcher"] = launcher
    path.write_text(json.dumps(value), encoding="utf-8")
    runtime, _ = load_runtime(path)
    try:
        assert runtime.config.sandbox_launcher == tuple(launcher)
        assert runtime.workflow_test_executor.runner.backend_factory(tmp_path).launcher == tuple(launcher)
    finally:
        runtime.close_all()


@pytest.mark.skipif(sys.platform != "linux", reason="deployable fixed-test launcher requires Linux namespaces")
@pytest.mark.parametrize("passes", [True, False])
def test_deployable_launcher_runs_original_fixed_validator_in_fresh_directories(tmp_path: Path, passes: bool) -> None:
    bwrap = shutil.which("bwrap")
    assert bwrap, "installed bubblewrap is required"
    launcher = (sys.executable, "-I", str(LAUNCHER), "--fixed-test", "--bwrap", str(Path(bwrap).resolve()), "--")
    for index in range(2):
        case = tmp_path / str(index)
        case.mkdir()
        fixture = ExecutionFixture(case, content="def test_result():\n    assert " + repr(passes) + "\n")
        fixture.dispatch()
        runner = FixedTestRunner(backend_factory=lambda directory: SandboxBackend(directory, launcher=launcher))
        result = fixture.worker(runner=runner).run_once(OWNER)
        assert result["state"] == "succeeded", result
        evidence = fixture.test_evidence()
        assert len(evidence) == 1 and evidence[0]["result"] == ("passed" if passes else "failed")
        outputs = fixture.outputs()
        report = json.loads(fixture.store.read(OWNER, WORKSPACE, outputs["test_report"]["artifact_id"]))
        assert report["passed"] is passes and report["environment"]["launcher_sha256"]
        junit = ET.fromstring(fixture.store.read(OWNER, WORKSPACE, outputs["junit"]["artifact_id"]))
        cases = list(junit.iter("testcase"))
        assert len(cases) == 1 and (cases[0].find("failure") is None) is passes
