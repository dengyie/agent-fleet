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
    _assert_gate(tmp_path, source, selector, expected)


def _assert_gate(tmp_path, source, selector, expected, extra_args=()):
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/test_sample.py').write_text(source + '\n')
    matrix = {'schema_version': 1, 'journeys': [{'id': 'sample', 'layer': 'contract', 'acceptance': 'real passing case',
               'tests': ['tests/test_sample.py::' + selector]}], 'external_checks': [{'id': 'real-model'}]}
    (tmp_path / 'matrix.json').write_text(json.dumps(matrix))
    result = subprocess.run([sys.executable, '-m', 'pytest', '-p', 'tools.testing.pytest_journeys',
                             '--require-journeys', '--journey-matrix=matrix.json', '--journey-report=result.json', '-q', *extra_args],
                            cwd=tmp_path, env={**os.environ, 'PYTHONPATH': str(ROOT)}, capture_output=True, text=True, timeout=30)
    assert result.returncode == expected, result.stdout + result.stderr
    report = json.loads((tmp_path / 'result.json').read_text())
    assert report['ci_status'] == ('passed' if expected == 0 else 'failed')
    assert report['external_checks'] == [{'id': 'real-model', 'status': 'not_run'}]
    if 'parametrize' in source:
        assert report['journeys'][0]['selectors'][0]['collected'] == 2


def test_deselected_required_parameter_cannot_count_as_full_coverage(tmp_path):
    _assert_gate(tmp_path, 'import pytest\n@pytest.mark.parametrize("n", [1,2])\ndef test_ok(n): assert n > 0',
                 'test_ok', 1, ['--deselect=tests/test_sample.py::test_ok[2]'])


@pytest.mark.parametrize('selection', [
    ['tests/test_sample.py::test_ok[1]'],
    ['tests/test_sample.py::test_ok', '-k', 'not 2'],
    ['--deselect=tests/test_sample.py::test_ok[2]'],
])
def test_hidden_failing_parameter_cannot_pass_journey(tmp_path, selection):
    _assert_gate(tmp_path, 'import pytest\n@pytest.mark.parametrize("n", [1,2])\ndef test_ok(n): assert n == 1',
                 'test_ok', 1, selection)


def test_missing_matrix_cannot_silently_disable_gate(tmp_path):
    result = subprocess.run([sys.executable, '-m', 'pytest', '-p', 'tools.testing.pytest_journeys', '--require-journeys'],
                            cwd=tmp_path, env={**os.environ, 'PYTHONPATH': str(ROOT)}, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert 'Invalid or missing journey matrix' in result.stderr


@pytest.mark.parametrize('flag', ['--lf', '--last-failed'])
@pytest.mark.parametrize('evidence_args', [
    ['--require-journeys'], ['--journey-report=result.json'],
    ['--require-journeys', '--journey-report=result.json'],
])
def test_last_failed_cache_cannot_hide_required_parameters(tmp_path, flag, evidence_args):
    (tmp_path / 'tests').mkdir()
    sample = tmp_path / 'tests/test_sample.py'
    sample.write_text('import pytest\n@pytest.mark.parametrize("n", [1,2])\ndef test_ok(n): assert n == 2\n')
    env = {**os.environ, 'PYTHONPATH': str(ROOT)}

    def run(*args):
        return subprocess.run([sys.executable, '-m', 'pytest', '-q', *args], cwd=tmp_path,
                              env=env, text=True, capture_output=True, timeout=30)

    assert run().returncode == 1
    assert json.loads((tmp_path / '.pytest_cache/v/cache/lastfailed').read_text()) == {
        'tests/test_sample.py::test_ok[1]': True}
    # Change the size too, so timestamp-based .pyc caching cannot hide the edit.
    sample.write_text('import pytest\n@pytest.mark.parametrize("n", [1,2])\ndef test_ok(n): assert n == 1 # changed\n')
    (tmp_path / 'matrix.json').write_text(json.dumps({'schema_version': 1, 'journeys': [
        {'id': 'sample', 'layer': 'contract', 'acceptance': 'both cases pass',
         'tests': ['tests/test_sample.py::test_ok']}]}))
    result = run('-p', 'tools.testing.pytest_journeys', flag, '--journey-matrix=matrix.json', *evidence_args)
    assert result.returncode == 4, result.stdout + result.stderr
    assert '--lf/--last-failed is incompatible with journey evidence' in result.stderr
    assert not (tmp_path / 'result.json').exists()
    # Ordinary last-failed runs remain available without journey evidence.
    assert run(flag).returncode == 0
    assert run().returncode == 1
