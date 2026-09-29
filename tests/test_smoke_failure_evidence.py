import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_failed_smoke_preserves_log_without_runtime_credentials(tmp_path):
    python = tmp_path / 'broken-python'
    python.write_text('#!/bin/sh\necho fixture-startup-failed\nexit 1\n')
    python.chmod(0o700)
    curl = tmp_path / 'curl'
    curl.write_text('#!/bin/sh\nexit 1\n'); curl.chmod(0o700)
    evidence = tmp_path / 'evidence'
    result = subprocess.run(['bash', str(ROOT / 'deploy/e2e-smoke.sh')], cwd=ROOT,
        env={**os.environ, 'PATH':str(tmp_path) + os.pathsep + os.environ['PATH'], 'PYTHON':str(python), 'FLEET_SMOKE_ARTIFACT_DIR':str(evidence)},
        capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert (evidence / 'hub.log').read_text().strip() == 'fixture-startup-failed'
    assert str(evidence) in result.stdout + result.stderr
    assert list(evidence.iterdir()) == [evidence / 'hub.log']
