"""Static release layout contract tests for the standalone frontend.

The ``frontend/`` tree is an independent static release that must be servable
by a plain static server (``python3 -m http.server``) without a Flask process
and without any build chain.  ``ReleaseLayoutTests`` accumulates across tasks:

- Task 12: shell files (index.html, config.js, routes.js, app.css), no
  generated/build artifacts, no credentials, CSS migration fidelity.
- Task 13: the api/ client + contracts modules join the release tree and must
  stay credential-free.
- Task 14: realtime/sse.js (sole EventSource owner) and state/store.js (pure
  DOM-agnostic store) join the release tree and stay credential-free.
- Task 17: required JS modules and the static-only smoke entry.
- Task 19/20/21: packaging safety and route documentation grow here.

There is no JS test framework in this repo; tests assert against the file
layout and source text like ``tests/test_frontend_xss.py``.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = REPO_ROOT / 'frontend'
PACKAGE_SCRIPT = REPO_ROOT / 'deploy' / 'package-frontend-release.sh'


class ReleaseLayoutTests(unittest.TestCase):
    """Task 12 subset: shell files exist, are non-secret and build-tool-free."""

    def test_frontend_shell_files_exist(self):
        required = ('index.html', 'config.js', 'routes.js', 'styles/app.css')
        for relative in required:
            self.assertTrue((FRONTEND_DIR / relative).is_file(), relative)

    def test_frontend_api_contract_files_exist(self):
        required = ('api/client.js', 'api/contracts.js')
        for relative in required:
            self.assertTrue((FRONTEND_DIR / relative).is_file(), relative)

    def test_frontend_has_required_modules(self):
        # Task 17: static-shell smoke depends on exactly this module set being
        # servable by a plain static server.
        required = ('index.html', 'config.js', 'api/client.js', 'api/contracts.js',
                    'realtime/sse.js', 'state/store.js', 'views/fleet.js',
                    'views/machine.js', 'views/task.js', 'views/session.js')
        for relative in required:
            self.assertTrue((FRONTEND_DIR / relative).is_file(), relative)

    def test_frontend_view_files_exist(self):
        required = ('views/fleet.js', 'views/machine.js', 'views/task.js',
                    'views/session.js')
        for relative in required:
            self.assertTrue((FRONTEND_DIR / relative).is_file(), relative)

    def test_frontend_release_contains_no_generated_artifacts(self):
        forbidden = ('node_modules', 'package.json', 'package-lock.json',
                     'yarn.lock', 'pnpm-lock.yaml', 'vite.config', 'webpack')
        for path in FRONTEND_DIR.rglob('*'):
            if path.is_file():
                for token in forbidden:
                    self.assertNotIn(token, path.name, f'{path}')
        # The shell tree must stay flat + styles/ + api/ + views/ (no build dirs).
        present = {p.relative_to(FRONTEND_DIR).as_posix()
                   for p in FRONTEND_DIR.rglob('*') if p.is_file()}
        allowed = {'index.html', 'config.js', 'routes.js', 'styles/app.css',
                   'api/client.js', 'api/contracts.js',
                   'realtime/sse.js', 'state/store.js',
                   'views/fleet.js', 'views/machine.js', 'views/task.js',
                   'views/session.js'}
        unexpected = present - allowed
        self.assertEqual(unexpected, set(),
                         f'unexpected frontend files: {sorted(unexpected)}')

    def test_frontend_css_is_a_migration_of_existing_visual_language(self):
        app_css = (FRONTEND_DIR / 'styles' / 'app.css').read_text()
        legacy = (REPO_ROOT / 'hub' / 'static' / 'style.css').read_text()
        # Shared design tokens and responsive breakpoints must carry over.
        for token in ('--bg: #0f172a', '--ok: #22c55e', '--bad: #ef4444',
                      '--warn: #f59e0b', '--link: #60a5fa'):
            self.assertIn(token, app_css, f'missing design token {token}')
        for breakpoint in ('max-width: 1439px', 'max-width: 1023px', 'max-width: 767px'):
            self.assertIn(breakpoint, app_css, f'missing breakpoint {breakpoint}')
        # The migration must not shrink the information-architecture selector set.
        self.assertGreaterEqual(len(app_css), len(legacy) - 50,
                                'app.css must be a faithful migration, not a subset')

    def test_frontend_release_contains_no_credentials(self):
        secret_hints = ('X-Agent-Fleet-Token', 'X-Runner-Credential',
                        'private_key', 'BEGIN PRIVATE KEY', 'SELECT ')
        for relative in ('index.html', 'config.js', 'routes.js',
                         'styles/app.css', 'api/client.js', 'api/contracts.js',
                         'realtime/sse.js', 'state/store.js',
                         'views/fleet.js', 'views/machine.js',
                         'views/task.js', 'views/session.js'):
            source = (FRONTEND_DIR / relative).read_text()
            for token in secret_hints:
                self.assertNotIn(token, source, f'{relative} leaked {token}')


class PackageSafetyTests(unittest.TestCase):
    """Task 19: independent frontend release packaging must fail closed.

    ``deploy/package-frontend-release.sh <output_dir> [version]`` copies only the
    static ``frontend/`` release files, writes a deterministic non-secret
    ``manifest.json`` (release version + file list only), and refuses to emit
    anything if a copied path matches a secret/state/database redline. It must
    use no build chain (npm/git) and no external network/production endpoints.
    """

    def test_package_script_source_rejects_secret_and_state_paths(self):
        source = Path("deploy/package-frontend-release.sh").read_text()
        for forbidden in ("credentials/", "state/", "runner-credential", "ingest-token"):
            self.assertIn(forbidden, source)

    def test_package_script_source_has_no_build_or_network_chain(self):
        # The packaging contract forbids a Node/npm build chain, git clean, and
        # any external network or production endpoint. Assert absence in source.
        source = (REPO_ROOT / 'deploy' / 'package-frontend-release.sh').read_text()
        for token in ('npm ', 'npm\nexec', 'webpack', 'git clean', 'vite', 'curl ',
                      'wget ', 'https://', 'agent.mangoqwq.com'):
            self.assertNotIn(token, source, f'打包脚本含禁止指令 {token}')

    def test_package_script_requires_explicit_output_dir_and_version(self):
        # Missing output_dir or version must fail (usage error, non-zero).
        for args in ((), ('/tmp/x',)):
            with self.subTest(args=args):
                proc = subprocess.run(
                    ['bash', str(PACKAGE_SCRIPT), *args],
                    capture_output=True, text=True)
                self.assertNotEqual(proc.returncode, 0)
        # Illegal version characters must be rejected (JSON-injection guard).
        proc = subprocess.run(
            ['bash', str(PACKAGE_SCRIPT), '/tmp/x', 'v1";evil'],
            capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)

    def _run_package(self, src_override, args):
        env = dict(os.environ)
        if src_override is not None:
            env['FRONTEND_SRC'] = str(src_override)
        return subprocess.run(['bash', str(PACKAGE_SCRIPT), *args],
                              capture_output=True, text=True, env=env)

    def test_package_copies_only_frontend_release_files_and_writes_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / 'pkg'
            proc = self._run_package(None, [str(out), '2026-08-24.1'])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            copied = {p.relative_to(out).as_posix()
                      for p in out.rglob('*') if p.is_file()}
            # Required modules present; manifest is the only non-frontend file.
            for rel in ('index.html', 'config.js', 'api/client.js',
                        'api/contracts.js', 'realtime/sse.js', 'state/store.js'):
                self.assertIn(rel, copied, rel)
            manifest = json.loads((out / 'manifest.json').read_text())
            self.assertEqual(manifest['version'], '2026-08-24.1')
            # file list = required frontend modules, in stable order, no extras.
            self.assertIn('index.html', manifest['files'])
            self.assertIn('state/store.js', manifest['files'])
            self.assertEqual(sorted(manifest['files']), manifest['files'])
            # manifest only contains version + file list.
            self.assertEqual(set(manifest.keys()), {'version', 'files'})
            # source frontend dir is not copied into the package.
            self.assertNotIn(str(FRONTEND_DIR), copied)

    def test_package_refuses_forbidden_secret_and_state_paths(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / 'frontend'
            (src / 'views').mkdir(parents=True)
            (src / 'credentials').mkdir()
            (src / 'state').mkdir()
            (src / 'index.html').write_text('<html></html>')
            for rel in ('views/fleet.js', 'credentials/runner-credential.json',
                        'state/current.json', 'secret.pem', 'config.js'):
                (src / rel).write_text('x')
            out = Path(td) / 'pkg'
            proc = self._run_package(src, [str(out), 'v1'])
            self.assertNotEqual(proc.returncode, 0, 'forbidden paths must fail closed')
            # fail-closed: nothing is emitted when packaging is refused.
            self.assertFalse(out.exists(), 'forbidden packaging must not create output')

    def test_package_manifest_is_deterministic(self):
        with tempfile.TemporaryDirectory() as td:
            out1 = Path(td) / 'pkg1'
            out2 = Path(td) / 'pkg2'
            p1 = self._run_package(None, [str(out1), '1.0.0'])
            p2 = self._run_package(None, [str(out2), '1.0.0'])
            self.assertEqual(p1.returncode, 0, p1.stderr)
            self.assertEqual(p2.returncode, 0, p2.stderr)
            self.assertEqual((out1 / 'manifest.json').read_bytes(),
                             (out2 / 'manifest.json').read_bytes())


class RoutingDocumentationTests(unittest.TestCase):
    """Task 20: same-origin frontend/backend Nginx routing example and docs.

    The example config in ``deploy/nginx-frontend-backend.example.conf`` has no
    real certificates/keys/tokens (placeholder upstream names only), routes the
    static frontend (``/``, ``/assets/*``) and backend (``/api/*``) as
    same-origin releases without CORS, keeps ``/api/stream`` unbuffered with a
    long read timeout, and keeps probe/runner paths on the backend while
    documenting that Access bypass stays an edge policy.
    """

    def _read(self, path):
        return Path(path).read_text()

    def _block_after(self, source, marker):
        start = source.index(marker)
        open_brace = source.index('{', start)
        depth = 0
        for i in range(open_brace, len(source)):
            if source[i] == '{':
                depth += 1
            elif source[i] == '}':
                depth -= 1
                if depth == 0:
                    return source[start:i + 1]
        raise AssertionError(f'unclosed block starting at {marker!r}')

    def test_sse_location_disables_buffering(self):
        # The brief-mandated routing documentation test.
        source = Path("deploy/nginx-frontend-backend.example.conf").read_text()
        self.assertIn("location /api/stream", source)
        self.assertIn("proxy_buffering off", source)
        self.assertNotIn("Access-Control-Allow-Origin *", source)

    def test_static_frontend_and_api_backend_are_same_origin(self):
        source = self._read('deploy/nginx-frontend-backend.example.conf')
        # Static frontend release: / and /assets/*.
        self.assertIn('upstream fleet_frontend', source)
        self.assertIn('upstream fleet_backend', source)
        self.assertIn('location /assets/', source)
        self.assertIn('location /', source)
        # Backend: /api (与 legacy /api/* 兼容), probe/runner 路径。
        self.assertIn('location /api/', source)
        for path in ('/api/ingest', '/api/scan', '/api/commands/'):
            self.assertIn(f'location {path}', source)
        self.assertIn('proxy_set_header Host $host', source)
        self.assertIn('proxy_set_header X-Forwarded-Proto $scheme', source)

    def test_sse_location_keeps_buffering_off_and_long_read_timeout(self):
        source = Path("deploy/nginx-frontend-backend.example.conf").read_text()
        block = self._block_after(source, 'location /api/stream')
        self.assertIn('proxy_buffering off', block)
        self.assertNotIn('proxy_buffering on', block)
        self.assertIn('proxy_read_timeout', block)
        long_enough = any(token in block for token in
                          ('proxy_read_timeout 3600s', 'proxy_read_timeout 1800s',
                           'proxy_read_timeout 600s'))
        self.assertTrue(long_enough, 'SSE read timeout must be long (>=10min)')
        self.assertIn('proxy_http_version 1.1', block)
        self.assertIn('proxy_set_header Connection', block)

    def test_example_config_has_no_wildcard_cors(self):
        source = Path("deploy/nginx-frontend-backend.example.conf").read_text()
        self.assertNotIn('Access-Control-Allow-Origin', source)
        self.assertNotIn('access-control-allow-origin', source.lower())

    def test_example_config_has_no_secrets(self):
        source = Path("deploy/nginx-frontend-backend.example.conf").read_text()
        for token in ('private key', 'ingest-token', 'runner-credential',
                      'token=', 'begin private key'):
            self.assertNotIn(token, source.lower())

    def test_nginx_expose_docs_defines_route_matrix(self):
        source = self._read('deploy/nginx-expose.md')
        for marker in ('路由矩阵', 'fleet_frontend', 'fleet_backend',
                       '不需要 CORS', 'proxy_buffering off'):
            self.assertIn(marker, source)

    def test_cloudflare_access_docs_preserves_bypass_include_boundary(self):
        source = self._read('deploy/cloudflare-access.md')
        # Documented boundaries match the bolded table format.
        self.assertIn('**Include** 策略', source)
        self.assertIn('**Bypass** 策略', source)
        # Page/task/operator paths stay Access Include; machine paths Bypass.
        self.assertIn('/assets/*', source)
        self.assertIn('/task/*', source)
        self.assertIn('/api/stream', source)
        self.assertIn('/api/commands/*', source)
        # Access bypass is edge policy, not hub authorization bypass.
        self.assertIn('边缘策略，不是 hub 的授权旁路', source)
        self.assertIn('X-Runner-Credential', source)


class RuntimeStoreHygieneTests(unittest.TestCase):
    """Runtime session/adoption stores must stay out of git and release tarballs."""

    def test_runtime_var_storage_is_ignored(self):
        ignored = REPO_ROOT / "var" / "adoptions" / "meta.db"
        result = subprocess.run(
            ["git", "check-ignore", "--no-index",
             "var/adoptions/meta.db", "var/sessions/meta.db"],
            cwd=REPO_ROOT, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("var/adoptions/meta.db", result.stdout)
        self.assertFalse(ignored.exists() and not ignored.is_file())

    def test_frontend_state_source_is_not_ignored(self):
        tracked = subprocess.run(
            ["git", "ls-files", "frontend/state/store.js"],
            cwd=REPO_ROOT, text=True, capture_output=True, check=True,
        )
        self.assertEqual(tracked.stdout.strip(), "frontend/state/store.js")
        ignored = subprocess.run(
            ["git", "check-ignore", "frontend/state/store.js"],
            cwd=REPO_ROOT, text=True, capture_output=True,
        )
        self.assertNotEqual(ignored.returncode, 0, ignored.stdout)

    def test_tools_package_wins_over_hostile_site_packages(self):
        # Container regression: hermes' image ships a regular ``tools``
        # package in site-packages. A namespace-package release ``tools``
        # loses to ANY regular package regardless of sys.path order, so
        # ``import tools.session`` died with ModuleNotFoundError. The
        # release-root anchor ``tools/__init__.py`` makes the release a
        # regular package at sys.path[0], which wins.
        with tempfile.TemporaryDirectory() as td:
            release = Path(td) / "release"
            hostile = Path(td) / "site-packages"
            (release / "tools" / "session").mkdir(parents=True)
            (release / "tools" / "__init__.py").write_text("")
            (release / "tools" / "session" / "__init__.py").write_text("")
            (hostile / "tools").mkdir(parents=True)
            (hostile / "tools" / "__init__.py").write_text("")
            probe = (
                "import sys, tools, tools.session\n"
                "print(tools.__file__)\n"
            )
            proc = subprocess.run(
                [sys.executable, "-c", probe],
                capture_output=True, text=True, cwd=td,
                env={**os.environ,
                     "PYTHONPATH": os.pathsep.join([str(release), str(hostile)]),
                     "PYTHONDONTWRITEBYTECODE": "1"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(
                proc.stdout.strip().startswith(str(release / "tools")),
                f"tools resolved outside the release: {proc.stdout}")

    def test_full_release_archive_includes_runtime_imports_not_var(self):
        script = REPO_ROOT / "deploy" / "package-release.sh"
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "agent-fleet-release.tgz"
            proc = subprocess.run(
                ["bash", str(script), str(archive)],
                cwd=REPO_ROOT, capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            listing = subprocess.run(
                ["tar", "-tzf", str(archive)],
                capture_output=True, text=True, check=True,
            ).stdout.splitlines()
            # No provenance arg → no stamp anywhere in the archive, and no
            # staging leftovers in the working tree.
            self.assertNotIn(".RELEASE_ORIGIN.tmp", listing)
            self.assertFalse(
                (REPO_ROOT / ".RELEASE_ORIGIN.tmp").exists()
                or (REPO_ROOT / ".RELEASE_ORIGIN.tmp.staged").exists()
                or (REPO_ROOT / ".package-release.tmp.tar").exists())
        required = (
            "agent_profiles.py",
            "hub/web.py",
            "tools/__init__.py",
            "tools/result_files.py",
            "tools/session/__init__.py",
            "tools/supervisor/supervisor.py",
            # Regression (2026-09-01 acceptance): the archive path list once
            # omitted frontend/, so cp -a-based releases kept a stale
            # 2026-08-25 frontend (no session view, no Task 66 allowlist)
            # and 49 frontend-contract tests failed on the release tree.
            "frontend/index.html",
            "frontend/views/machine.js",
            "frontend/views/task.js",
            "frontend/views/session.js",
        )
        for name in required:
            self.assertTrue(
                any(entry == name or entry.endswith("/" + name) for entry in listing),
                f"missing {name}")
        for leaked in listing:
            self.assertFalse(leaked.startswith("var/") or "/var/" in leaked,
                             f"runtime store leaked: {leaked}")
            self.assertNotIn("__tmp", leaked)
            self.assertNotIn("credentials/", leaked)
            self.assertFalse(leaked.endswith(".pem") or leaked.endswith(".key"))
        # The hub's runtime ``state/`` observation tree must never ship;
        # ``frontend/state/`` (the DOM-agnostic store module) is tracked
        # frontend source and is the ONLY permitted ``/state/`` entry.
        runtime_states = [e for e in listing
                          if e.startswith("state/")
                          or ("/state/" in e and not e.startswith("frontend/"))]
        self.assertEqual(runtime_states, [],
                         f"runtime state leaked: {runtime_states}")
        frontend_states = [e for e in listing if e.startswith("frontend/state/")]
        self.assertEqual(
            frontend_states, ["frontend/state/", "frontend/state/store.js"],
            "only the tracked frontend store module may live under frontend/state/")

    def test_full_release_archive_with_origin_embeds_release_origin_stamp(self):
        script = REPO_ROOT / "deploy" / "package-release.sh"
        origin = "deadbeef|424242|https://github.com/dengyie/agent-fleet/actions/runs/424242"
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "agent-fleet-release.tgz"
            proc = subprocess.run(
                ["bash", str(script), str(archive), origin],
                cwd=REPO_ROOT, capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            stamp = subprocess.run(
                ["tar", "-xzOf", str(archive), "RELEASE_ORIGIN"],
                capture_output=True, text=True, check=True,
            ).stdout
            self.assertEqual(
                stamp,
                "commit: deadbeef\n"
                "run: 424242\n"
                "url: https://github.com/dengyie/agent-fleet/actions/runs/424242\n")
            # No staging leftovers in the working tree after packaging.
            self.assertFalse(
                (REPO_ROOT / ".RELEASE_ORIGIN.tmp").exists()
                or (REPO_ROOT / ".RELEASE_ORIGIN.tmp.staged").exists()
                or (REPO_ROOT / ".package-release.tmp.tar").exists())


if __name__ == '__main__':
    unittest.main()
