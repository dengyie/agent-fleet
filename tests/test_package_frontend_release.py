"""Behavioural tests for deploy/package-frontend-release.sh.

These run the real script against a synthetic frontend tree, including under a
non-C locale, so ordering, pruning and manifest escaping are proven end to end.
"""
import json
import os
import shutil
import subprocess
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit

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


def test_packaged_static_references_are_versioned_without_changing_sources(tmp_path):
    src = _frontend(tmp_path, ['assets/app.js', 'assets/views/settings.js',
                               'assets/styles/app.css', 'assets/styles/settings.css',
                               'assets/brand/logo.svg'])
    (src / 'index.html').write_text(
        '<link rel="stylesheet" href="./assets/styles/app.css">\n'
        '<script type="module" src="./assets/app.js"></script>\n'
        '<img src="/assets/brand/logo.svg">\n', encoding='utf-8')
    (src / 'assets/app.js').write_text(
        "import { mount } from './views/settings.js';\n"
        "import './views/settings.js';\n"
        "import('./views/settings.js');\n", encoding='utf-8')
    (src / 'assets/styles/app.css').write_text(
        "@import url('./settings.css');\n", encoding='utf-8')

    result = _package(src, tmp_path / 'out', {}, version='a1b2c3')

    assert result.returncode == 0, result.stderr
    assert 'href="./assets/styles/app.css?v=a1b2c3"' in (tmp_path / 'out/index.html').read_text()
    assert 'src="./assets/app.js?v=a1b2c3"' in (tmp_path / 'out/index.html').read_text()
    assert 'src="/assets/brand/logo.svg?v=a1b2c3"' in (tmp_path / 'out/index.html').read_text()
    assert "'./views/settings.js?v=a1b2c3'" in (tmp_path / 'out/assets/app.js').read_text()
    assert "import './views/settings.js?v=a1b2c3'" in (tmp_path / 'out/assets/app.js').read_text()
    assert "import('./views/settings.js?v=a1b2c3')" in (tmp_path / 'out/assets/app.js').read_text()
    assert "url('./settings.css?v=a1b2c3')" in (tmp_path / 'out/assets/styles/app.css').read_text()
    assert (src / 'index.html').read_text().find('?v=') == -1


def test_packaged_static_references_preserve_existing_query_and_external_urls(tmp_path):
    src = _frontend(tmp_path, ['assets/app.js'])
    (src / 'index.html').write_text(
        '<script src="/assets/app.js?mode=module"></script>\n'
        '<link href="./assets/app.js#entry">\n'
        '<script src="https://cdn.example/app.js"></script>\n', encoding='utf-8')

    result = _package(src, tmp_path / 'out', {}, version='release-2')

    assert result.returncode == 0, result.stderr
    html = (tmp_path / 'out/index.html').read_text()
    assert '/assets/app.js?mode=module&v=release-2' in html
    assert './assets/app.js?v=release-2#entry' in html
    assert 'https://cdn.example/app.js' in html


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


@pytest.mark.parametrize('versioned', [False, True])
def test_browser_upgrade_with_fresh_old_asset_cache(tmp_path, versioned):
    if not os.environ.get('FLEET_PLAYWRIGHT_MODULE'):
        pytest.skip('Set FLEET_PLAYWRIGHT_MODULE to run Chromium acceptance')
    releases = []
    for version, width in [('old', 101), ('new', 202)]:
        src = tmp_path / version / 'src'
        assets = src / 'assets'
        assets.mkdir(parents=True)
        (src / 'index.html').write_text(
            '<link rel="stylesheet" href="/assets/app.css">'
            '<div id="probe"></div><script type="module" src="/assets/app.js"></script>',
            encoding='utf-8')
        (assets / 'app.js').write_text(
            "import {value} from './named.js'; import './side-effect.js';"
            "const dynamic = await import('./dynamic.js');"
            f"window.cacheProbe = {{entry:'{version}',named:value,side:window.side,dynamic:dynamic.value}};",
            encoding='utf-8')
        (assets / 'named.js').write_text(f"export const value = '{version}';", encoding='utf-8')
        (assets / 'side-effect.js').write_text(f"window.side = '{version}';", encoding='utf-8')
        (assets / 'dynamic.js').write_text(f"export const value = '{version}';", encoding='utf-8')
        (assets / 'app.css').write_text("@import url('./nested.css');", encoding='utf-8')
        (assets / 'nested.css').write_text(f'#probe {{width:{width}px}}', encoding='utf-8')
        if versioned:
            out = tmp_path / version / 'release'
            result = _package(src, out, {}, version=version)
            assert result.returncode == 0, result.stderr
            releases.append(out)
        else:
            releases.append(src)
        # http.server's validator is second-granularity mtime (no ETag).
        # Distinguish same-sized documents built within one test second so its
        # HTML revalidation does not incorrectly return 304 for the new release.
        os.utime(releases[-1] / 'index.html', (1_700_000_000 + width, 1_700_000_000 + width))

    current = [0]
    hits = []

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(releases[current[0]]), **kwargs)

        def do_GET(self):
            hits.append(self.path)
            super().do_GET()

        def do_POST(self):
            assert self.path == '/_fixture/deploy'
            current[0] = 1
            self.send_response(204)
            self.end_headers()

        def end_headers(self):
            self.send_header('Cache-Control', 'max-age=14400' if '/assets/' in self.path else 'no-cache')
            super().end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run([
            'node', str(REPO_ROOT / 'tests/fixtures/frontend/cache_upgrade_browser.cjs'),
            f'http://127.0.0.1:{server.server_port}', 'new' if versioned else 'old',
        ], capture_output=True, text=True, timeout=45)
        assert result.returncode == 0, result.stdout + result.stderr
        # Prove the unversioned control really reused its 4-hour cache and the
        # fix fetched every dependency, rather than accidentally disabling cache.
        for name in ('app.js', 'named.js', 'side-effect.js', 'dynamic.js', 'app.css', 'nested.css'):
            matches = [hit for hit in hits if urlsplit(hit).path == '/assets/' + name]
            assert len(matches) == (2 if versioned else 1), (name, matches)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
