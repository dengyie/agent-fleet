"""Behavioural tests for deploy/package-frontend-release.sh.

These run the real script against a synthetic frontend tree, including under a
non-C locale, so ordering, pruning and manifest escaping are proven end to end.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / 'deploy' / 'package-frontend-release.sh'

pytestmark = pytest.mark.skipif(shutil.which('bash') is None, reason='bash required')


def _frontend(tmp_path, names):
    src = tmp_path / 'frontend'
    for name in ('index.html', *names):
        path = src / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('x')
    return src


def _package(src, out, env_overrides, version='v1'):
    env = {k: v for k, v in os.environ.items() if not k.startswith('LC_') and k != 'LANG'}
    env.update(env_overrides)
    env['FRONTEND_SRC'] = str(src)
    return subprocess.run(['bash', str(SCRIPT), str(out), version],
                          capture_output=True, text=True, env=env, timeout=60)


@pytest.mark.parametrize('locale_env', [
    {'LANG': 'zh_CN.UTF-8'},
    {'LANG': 'zh_CN.UTF-8', 'LC_ALL': 'zh_CN.UTF-8'},
    {'LC_ALL': 'C'},
])
def test_manifest_order_is_byte_order_regardless_of_locale(tmp_path, locale_env):
    # Under zh_CN collation these sort as _x, a, B, Z; byte order is B, Z, _x, a.
    src = _frontend(tmp_path, ['B.js', 'a.js', '_x.js', 'Z.js', 'assets/b.css', 'assets/A.css'])
    result = _package(src, tmp_path / 'out', locale_env)
    assert result.returncode == 0, result.stderr
    manifest = json.loads((tmp_path / 'out' / 'manifest.json').read_text())
    assert manifest['files'] == sorted(manifest['files'])
    assert manifest['files'][:4] == ['B.js', 'Z.js', '_x.js', 'a.js']


def test_ds_store_is_pruned_at_every_level(tmp_path):
    src = _frontend(tmp_path, ['.DS_Store', 'assets/.DS_Store', 'assets/views/.DS_Store', 'assets/app.js'])
    result = _package(src, tmp_path / 'out', {'LANG': 'zh_CN.UTF-8'})
    assert result.returncode == 0, result.stderr
    manifest = json.loads((tmp_path / 'out' / 'manifest.json').read_text())
    assert manifest['files'] == ['assets/app.js', 'index.html']
    assert not list((tmp_path / 'out').rglob('.DS_Store'))


def test_manifest_escapes_quotes_and_backslashes(tmp_path):
    names = ['assets/quo"te.js', 'assets/back\\slash.js', 'assets/[ab]*.js']
    src = _frontend(tmp_path, names)
    result = _package(src, tmp_path / 'out', {'LANG': 'zh_CN.UTF-8'})
    assert result.returncode == 0, result.stderr
    manifest = json.loads((tmp_path / 'out' / 'manifest.json').read_text())
    assert manifest['version'] == 'v1'
    assert set(manifest['files']) == {'index.html', *names}
    for name in names:
        assert (tmp_path / 'out' / name).is_file()


def test_forbidden_component_is_detected_without_glob_expansion(tmp_path):
    # A literal '*' component must not be glob-expanded into sibling names,
    # and nested credential components are still rejected.
    src = _frontend(tmp_path, ['assets/*/ok.js', 'assets/credentials/token.txt'])
    result = _package(src, tmp_path / 'out', {'LANG': 'zh_CN.UTF-8'})
    assert result.returncode == 1
    assert 'assets/credentials/token.txt' in result.stderr
    assert 'assets/*/ok.js' not in result.stderr
    assert not (tmp_path / 'out').exists()


def test_control_characters_in_paths_fail_closed(tmp_path):
    src = _frontend(tmp_path, ['assets/tab\there.js'])
    result = _package(src, tmp_path / 'out', {'LANG': 'zh_CN.UTF-8'})
    assert result.returncode == 1
    assert 'control character' in result.stderr
    assert not (tmp_path / 'out').exists()


def test_script_does_not_leak_ifs_or_repeat_set_flags():
    source = SCRIPT.read_text()
    assert "IFS='/'" not in source
    assert source.count('set -euo pipefail') == 1
    assert '\nset -eu\n' not in source
    assert 'LC_ALL=C sort' in source
