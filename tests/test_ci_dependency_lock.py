"""CI installs a pinned dependency lock and gates on lint/shellcheck/pip-audit."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/ci.yml"
LOCK = ROOT / "requirements-lock.txt"
PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)(\s*;.*)?$")


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _direct_requirements(path: Path) -> set[str]:
    names: set[str] = set()
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-r "):
            names |= _direct_requirements(path.parent / line[3:].strip())
            continue
        names.add(_canonical(re.split(r"[<>=!~;\[ ]", line, maxsplit=1)[0]))
    return names


def _lock_pins() -> dict[str, set[str]]:
    pins: dict[str, set[str]] = {}
    for raw in LOCK.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = PIN_RE.match(line)
        assert match, f"unpinned lock entry: {line!r}"
        pins.setdefault(_canonical(match.group(1)), set()).add(match.group(2))
    return pins


def test_lock_pins_every_direct_requirement_exactly():
    pins = _lock_pins()
    missing = _direct_requirements(ROOT / "requirements-test.txt") - set(pins)
    assert not missing, f"requirements-lock.txt lacks {sorted(missing)}"
    # Python 3.10 backports needed by pytest/referencing must be locked too.
    assert {"tomli", "exceptiongroup", "typing-extensions"} <= set(pins)


def _steps(job: str) -> list[dict]:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"][job]["steps"]


def test_ci_jobs_install_from_lock():
    for job in ("test", "launch-policy-windows"):
        install = next(s["run"] for s in _steps(job) if s.get("name") == "Install dependencies")
        assert "pip install -r requirements-lock.txt" in install
        assert "requirements-test.txt" not in install


def test_ci_gates_are_blocking():
    runs = {s.get("name"): s.get("run", "") for s in _steps("test")}
    ruff = runs["Lint Python (syntax errors and undefined names)"]
    assert "ruff check" in ruff and "E9,F63,F7,F82,F401" in ruff
    assert "shellcheck" in runs["Lint deploy shell scripts"]
    assert "--severity=error" in runs["Lint deploy shell scripts"]
    audit = runs["Audit pinned dependencies"]
    assert "pip-audit -r requirements-lock.txt" in audit
    for name in ("Lint Python (syntax errors and undefined names)", "Lint deploy shell scripts",
                 "Audit pinned dependencies"):
        step = next(s for s in _steps("test") if s.get("name") == name)
        assert "|| true" not in step["run"] and not step.get("continue-on-error")
    names = [s.get("name") for s in _steps("test")]
    assert names.index("Audit pinned dependencies") < names.index("Run test suite")


def test_linux_ci_provisions_workflow_isolation_before_the_full_gate():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["test"]
    steps = job["steps"]
    suite_index = next(i for i, step in enumerate(steps) if step.get("name") == "Run test suite")
    isolation_steps = [
        step.get("run", "") for step in steps[:suite_index]
        if step.get("name") == "Install Linux isolation test tools"
    ]
    assert len(isolation_steps) == 1
    setup = isolation_steps[0]
    assert "sudo apt-get update" in setup
    assert "sudo apt-get install --no-install-recommends -y bubblewrap libseccomp2" in setup
    # The preserved Linux full gate already takes ~28 minutes before setup.
    assert 45 <= job["timeout-minutes"] <= 60
