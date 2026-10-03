"""The release gate itself must fail closed, including skipped parameters."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('source,selector,expected', [
    ('def test_ok(): pass', 'test_ok', 0),
    ('def test_ok(): pass', 'test_missing', 1),
    ('import pytest\n@pytest.mark.skip(reason="missing browser")\ndef test_ok(): pass', 'test_ok', 1),
    ('import pytest\n@pytest.mark.xfail\ndef test_ok(): assert False', 'test_ok', 1),
    ('import pytest\n@pytest.mark.xfail\ndef test_ok(): pass', 'test_ok', 1),
    ('def test_ok(): pass\ndef test_unmapped(): assert False', 'test_ok', 1),
    ('import pytest\n@pytest.mark.parametrize("n", [1,2])\ndef test_ok(n):\n if n==2: pytest.skip("missing variant")', 'test_ok', 1),
    ('import pytest\n@pytest.mark.parametrize("n", [1,2])\ndef test_ok(n): assert n > 0', 'test_ok', 0),
    ('import pytest\n@pytest.fixture\ndef resource():\n yield\n assert False\ndef test_ok(resource): pass', 'test_ok', 1),
])
def test_required_journeys_use_real_collected_results(tmp_path, source, selector, expected):
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/test_sample.py').write_text(source + '\n')
    matrix = {'schema_version': 1, 'journeys': [{'id': 'sample', 'layer': 'contract', 'acceptance': 'real passing case',
               'tests': ['tests/test_sample.py::' + selector]}], 'external_checks': [{'id': 'real-model'}]}
    (tmp_path / 'matrix.json').write_text(json.dumps(matrix))
    result = subprocess.run([sys.executable, '-m', 'pytest', '-p', 'tools.testing.pytest_journeys',
                             '--require-journeys', '--journey-matrix=matrix.json', '--journey-report=result.json', '-q'],
                            cwd=tmp_path, env={**os.environ, 'PYTHONPATH': str(ROOT)}, capture_output=True, text=True, timeout=30)
    assert result.returncode == expected, result.stdout + result.stderr
    report = json.loads((tmp_path / 'result.json').read_text())
    assert report['ci_status'] == ('passed' if expected == 0 else 'failed')
    assert report['external_checks'] == [{'id': 'real-model', 'status': 'not_run'}]
    if 'parametrize' in source:
        assert report['journeys'][0]['selectors'][0]['collected'] == 2


def test_missing_matrix_cannot_silently_disable_gate(tmp_path):
    result = subprocess.run([sys.executable, '-m', 'pytest', '-p', 'tools.testing.pytest_journeys', '--require-journeys'],
                            cwd=tmp_path, env={**os.environ, 'PYTHONPATH': str(ROOT)}, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert 'Invalid or missing journey matrix' in result.stderr
