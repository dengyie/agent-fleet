"""Linux namespace launcher used only by workflow isolation acceptance tests."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys

from fleet_core.backends.sandbox import SandboxBackend


_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "workflow" / "linux_sandbox.py"


def _bwrap_path() -> Path:
    executable = shutil.which("bwrap")
    if executable is None:
        raise RuntimeError("Linux workflow isolation requires bubblewrap")
    path = Path(executable).resolve(strict=True)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError("invalid bubblewrap executable")
    return path


def _launcher(*, mode: str, source: Path, extra: tuple[str, ...]) -> tuple[str, ...]:
    return (
        sys.executable, "-I", str(_FIXTURE), "--bwrap", str(_bwrap_path()),
        "--base", str(Path(sys.base_prefix).resolve(strict=True)),
        "--venv", str(Path(sys.prefix).resolve(strict=True)),
        "--python", str(Path(sys.executable).absolute()),
        "--mode", mode, "--source", str(source), *extra, "--",
    )


def linux_test_backend(workdir: Path) -> SandboxBackend:
    root = workdir.resolve(strict=True)
    script = Path(__file__).resolve().parents[1] / "fleet_core/workflow/linux_launcher.py"
    launcher = (sys.executable, "-I", str(script), "--fixed-test", "--bwrap", str(_bwrap_path()), "--")
    return SandboxBackend(root, launcher=launcher)


def linux_review_launcher(script: Path) -> tuple[str, ...]:
    reviewer = script.resolve(strict=True)
    if not reviewer.is_file():
        raise ValueError("reviewer script must be a regular file")
    return _launcher(mode="review", source=Path("{source}"), extra=(
        "--control", "{control}", "--scratch", "{scratch}", "--reviewer", str(reviewer),
    ))
