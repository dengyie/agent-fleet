"""Administrator-selected test launcher for a Node-owned private checkout."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys


def main() -> None:
    if sys.platform != "linux":
        raise RuntimeError("native remote-test acceptance requires Linux")
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        raise RuntimeError("native remote-test acceptance requires bubblewrap")
    source = Path.cwd().resolve(strict=True)
    script = Path(__file__).with_name("linux_sandbox.py").resolve(strict=True)
    command = [sys.executable, "-I", str(script), "--bwrap", bwrap,
        "--base", str(Path(sys.base_prefix).resolve(strict=True)),
        "--venv", str(Path(sys.prefix).resolve(strict=True)),
        "--python", str(Path(sys.executable).absolute()), "--mode", "test",
        "--source", str(source), "--junit", str(source / ".fleet-workflow-results.xml"),
        "--", *sys.argv[1:]]
    os.execv(sys.executable, command)


if __name__ == "__main__":
    main()
