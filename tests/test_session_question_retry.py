"""Execute question DOM handlers to protect frozen-answer retry semantics."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("scenario", ["network_retry", "validation_retry", "bounded_pages", "bounded_submissions"])
def test_question_retry_and_memory_boundaries(scenario):
    if not shutil.which("node"):
        pytest.skip("Node is optional for the production frontend")
    module = (ROOT / "frontend/assets/views/session-questions.js").as_uri()
    result = subprocess.run(["node", str(ROOT / "tests/fixtures/frontend/question_retry.mjs"), module, scenario],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
