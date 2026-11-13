"""Newly installed CLIs must be visible before their first session."""

import os
import subprocess
import sys

import pytest

from connectors import create


@pytest.mark.parametrize("family,command", [
    ("codex", "codex"), ("claude_code", "claude"),
    ("pi", "pi"), ("zcode", "zcode"),
])
@pytest.mark.parametrize("installed", [True, False])
def test_cli_detection_without_session_history(tmp_path, family, command, installed):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if installed:
        executable = bin_dir / command
        # Detection should inspect availability, never launch the agent.
        executable.write_text("#!/bin/sh\nexit 99\n")
        executable.chmod(0o755)

    class Context:
        def run_python(self, script):
            result = subprocess.run(
                [sys.executable, "-c", script], capture_output=True, text=True,
                env={**os.environ, "HOME": str(tmp_path), "PATH": str(bin_dir)},
                check=True, timeout=10,
            )
            return result.stdout

    state = create(family).collect(Context())
    assert state["installed"] is installed
    assert "error" not in state
    if installed:
        assert state.get("sessions", state.get("projects")) == []
        assert state["active_count"] == 0
