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
import tarfile
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = REPO_ROOT / 'frontend'
PACKAGE_SCRIPT = REPO_ROOT / 'deploy' / 'package-frontend-release.sh'


class ReleaseLayoutTests(unittest.TestCase):
    """Task 12 subset: shell files exist, are non-secret and build-tool-free."""

    def test_frontend_shell_files_exist(self):
        required = ('index.html', 'config.js', 'assets/routes.js', 'assets/styles/app.css')
        for relative in required:
            self.assertTrue((FRONTEND_DIR / relative).is_file(), relative)

    def test_frontend_api_contract_files_exist(self):
        required = ('assets/api/client.js', 'assets/api/contracts.js')
        for relative in required:
            self.assertTrue((FRONTEND_DIR / relative).is_file(), relative)

    def test_frontend_has_required_modules(self):
        # Task 17: static-shell smoke depends on exactly this module set being
        # servable by a plain static server.
        required = ('index.html', 'config.js', 'assets/api/client.js', 'assets/api/contracts.js',
                    'assets/api/platform.js', 'assets/api/accounts.js',
                    'assets/realtime/sse.js', 'assets/state/store.js', 'assets/views/fleet.js',
                    'assets/views/machine.js', 'assets/views/task.js', 'assets/views/session.js',
                    'assets/views/assistant.js', 'assets/views/monitoring.js', 'assets/views/account.js')
        for relative in required:
            self.assertTrue((FRONTEND_DIR / relative).is_file(), relative)

    def test_frontend_view_files_exist(self):
        required = ('assets/views/fleet.js', 'assets/views/machine.js', 'assets/views/task.js',
                    'assets/views/session.js', 'assets/views/assistant.js', 'assets/views/monitoring.js', 'assets/views/account.js')
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
        allowed = {'index.html', 'config.js', 'assets/routes.js', 'assets/styles/app.css',
                   'assets/api/client.js', 'assets/api/contracts.js', 'assets/api/platform.js', 'assets/api/accounts.js',
                   'assets/realtime/sse.js', 'assets/state/store.js',
                   'assets/views/fleet.js', 'assets/views/machine.js', 'assets/views/task.js',
                   'assets/views/session.js', 'assets/views/assistant.js', 'assets/views/monitoring.js', 'assets/views/account.js'}
        allowed.update({'assets/app.js', 'THIRD_PARTY.md'})
        for folder in ('assets/ui', 'assets/vendor', 'assets/shell', 'assets/styles', 'assets/views/assistant'):
            allowed.update(p.relative_to(FRONTEND_DIR).as_posix() for p in (FRONTEND_DIR / folder).rglob('*') if p.is_file())
        unexpected = present - allowed
        self.assertEqual(unexpected, set(),
                         f'unexpected frontend files: {sorted(unexpected)}')


    def test_frontend_release_contains_no_credentials(self):
        secret_hints = ('X-Agent-Fleet-Token', 'X-Runner-Credential',
                        'private_key', 'BEGIN PRIVATE KEY', 'SELECT ')
        for relative in ('index.html', 'config.js', 'assets/routes.js',
                         'assets/styles/app.css', 'assets/api/client.js', 'assets/api/contracts.js',
                         'assets/api/platform.js', 'assets/api/accounts.js',
                         'assets/realtime/sse.js', 'assets/state/store.js',
                         'assets/views/fleet.js', 'assets/views/machine.js',
                         'assets/views/task.js', 'assets/views/session.js',
                         'assets/views/assistant.js', 'assets/views/monitoring.js', 'assets/views/account.js'):
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

class DeploymentSafetyTests(unittest.TestCase):
    def test_e2e_smoke_copies_report_schema_dependency(self):
        source = (REPO_ROOT / "deploy" / "e2e-smoke.sh").read_text()
        self.assertIn(
            "cp -r hub tools connectors frontend agent_profiles.py report_schema.py session_schema.py platform_schema.py requirements.txt",
            source,
        )

    def test_container_install_has_legacy_migration_and_permission_preflight(self):
        source = (REPO_ROOT / "deploy" / "hk-container-install.sh").read_text()
        self.assertIn("allow_existing", source)
        self.assertIn("DEPLOY_PREFLIGHT_FAILED", source)
        self.assertIn("prepare_runtime_dir", source)
        self.assertIn("install -d -o", source)

    def test_container_install_preserves_runtime_credentials(self):
        source = (REPO_ROOT / "deploy" / "hk-container-install.sh").read_text()
        preserve_marker = 'fleet_preserve_runtime_tree "$live" "$release"'
        token_marker = 'install -m 600 "$token_source" "$release/credentials/ingest-token"'
        self.assertIn(preserve_marker, source)
        self.assertLess(source.index(preserve_marker), source.index(token_marker))
        self.assertIn('cleanup_token_source()', source)
        self.assertIn('source_real=$(readlink -f \"$token_source\"', source)
        self.assertIn('active_real=$(readlink -f \"$live/credentials/ingest-token\"', source)
        adopt = (REPO_ROOT / "deploy" / "hk-container-adopt-release.sh").read_text()
        self.assertIn(preserve_marker, adopt)

    def test_overlay_adopt_uses_rsync_free_runtime_preserving_helper(self):
        helper = (REPO_ROOT / "deploy" / "hk-overlay-sync.sh").read_text()
        adopt = (REPO_ROOT / "deploy" / "hk-container-adopt-release.sh").read_text()
        install = (REPO_ROOT / "deploy" / "hk-container-install.sh").read_text()
        self.assertIn('. "$here/hk-overlay-sync.sh"', adopt)
        self.assertIn('fleet_preserve_runtime_tree "$live" "$release"', adopt)
        self.assertIn('fleet_overlay_backup_tree "$live" "$backup"', adopt)
        self.assertIn('fleet_overlay_sync_tree "$release" "$live"', adopt)
        self.assertNotIn('overlay mode needs rsync', adopt)
        self.assertIn('Minimal-image fallback', helper)
        self.assertIn('switched=1', install)
        self.assertIn('switched=1', adopt)
        self.assertIn('trap rollback ERR', adopt)
        self.assertIn('old_stopped=0', install)
        self.assertIn('Do not start a', install)
        self.assertIn('deploy/hk-self-report-loop.sh', install)
        self.assertIn('hk-web-process-control.py', install)
        self.assertNotIn('kill -KILL "$pid"', install)
        self.assertNotIn('kill -TERM "$old_pid"', install)
        self.assertIn('probe_process_matches', install)
        self.assertIn('fleet_cp_tree_retry()', helper)
        self.assertIn('attempts < 5', helper)

    def test_overlay_helper_fallback_preserves_runtime_and_rolls_back(self):
        helper = REPO_ROOT / "deploy" / "hk-overlay-sync.sh"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tool_bin = root / "bin"
            tool_bin.mkdir()
            for name in ("cp", "mkdir", "rm"):
                tool_bin.joinpath(name).symlink_to(Path('/bin') / name)
            source = root / "source"
            target = root / "target"
            (source / "frontend").mkdir(parents=True)
            (target / "frontend").mkdir(parents=True)
            (target / "credentials").mkdir()
            (target / "state").mkdir()
            (target / "var").mkdir()
            (source / "frontend" / "index.html").write_text("new")
            (target / "frontend" / "index.html").write_text("old")
            (target / "credentials" / "operator-token").write_text("secret")
            (target / "state" / "meta.db").write_text("state")
            (target / "unknown.txt").write_text("keep")
            script = "\n".join((
                "set -eu",
                ". \"$0\"",
                "fleet_overlay_backup_tree \"$2/target\" \"$2/backup\"",
                "fleet_overlay_sync_tree \"$1\" \"$2/target\"",
                "test \"$(<\"$2/target/frontend/index.html\")\" = new",
                "test \"$(<\"$2/target/credentials/operator-token\")\" = secret",
                "test \"$(<\"$2/target/state/meta.db\")\" = state",
                "test \"$(<\"$2/target/unknown.txt\")\" = keep",
                "fleet_overlay_restore_tree \"$2/backup\" \"$2/target\"",
                "test \"$(<\"$2/target/frontend/index.html\")\" = old",
                "test \"$(<\"$2/target/credentials/operator-token\")\" = secret",
                "test \"$(<\"$2/target/unknown.txt\")\" = keep",
            ))
            env = dict(os.environ)
            env['PATH'] = str(tool_bin)
            proc = subprocess.run(
                ['/bin/bash', '-c', script, str(helper), str(source), str(root)],
                capture_output=True, text=True, env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_guardian_and_probe_allow_existing_only_for_existing_process(self):
        guardian = (REPO_ROOT / "hub" / "agent_fleet_guardian.py").read_text()
        loop = (REPO_ROOT / "deploy" / "hk-self-report-loop.sh").read_text()
        self.assertIn('"web",\n                "allow_existing"', guardian)
        self.assertNotIn("os.kill(old_pid", guardian)
        self.assertIn('mode=${3:-api_only}', loop)
        self.assertIn('allow_existing', loop)

    def test_probe_failure_counters_are_safe_under_set_e(self):
        loop = (REPO_ROOT / "deploy" / "hk-self-report-loop.sh").read_text()
        # ``((counter++))`` returns status 1 when counter is initially zero;
        # with set -e that exits before the first recovery attempt.
        self.assertNotIn('((failure_count++))', loop)
        self.assertNotIn('((restart_failure_count++))', loop)
        self.assertIn('failure_count=$((failure_count + 1))', loop)
        self.assertIn('restart_failure_count=$((restart_failure_count + 1))', loop)

    def test_probe_process_identity_is_bounded_and_verified_after_start(self):
        install = (REPO_ROOT / "deploy" / "hk-container-install.sh").read_text()
        start = install.index('probe_process_matches()')
        end = install.index('\n}\n', start) + 3
        matcher = install[start:end]
        self.assertIn('expected_cwd', matcher)
        self.assertIn('expected_uid', matcher)
        self.assertIn('/proc/$probe_pid/status', matcher)
        self.assertIn('readlink /proc/$q_probe_pid/cwd', matcher)
        self.assertIn('su -s /bin/bash -c', matcher)
        self.assertIn("awk '/^Uid:/", matcher)
        self.assertIn('stop_probe_loop "$probe_cwd"', install)
        self.assertIn('probe_process_matches "$probe_pid" "$target"', install)

    def test_probe_loop_is_singleton_and_pid_cleanup_is_owner_bound(self):
        loop = (REPO_ROOT / "deploy" / "hk-self-report-loop.sh").read_text()
        self.assertIn('lock_dir=${pid_file}.lock', loop)
        self.assertIn('lock_file=${lock_dir}/flock', loop)
        self.assertIn('exec {probe_lock_fd}>"$lock_file"', loop)
        self.assertIn('flock -n "$probe_lock_fd"', loop)
        self.assertIn('[[ "$(cat "$pid_file"', loop)
        self.assertIn("trap cleanup_probe_state EXIT", loop)
        self.assertIn("trap 'exit 0' INT TERM", loop)
        self.assertIn('interruptible_sleep()', loop)
        self.assertIn('interruptible_sleep "$health_check_interval"', loop)

    def test_adopt_release_matches_only_the_exact_hub_process(self):
        adopt = (REPO_ROOT / "deploy" / "hk-container-adopt-release.sh").read_text()
        start = adopt.index('pid=$(su')
        end = adopt.index('\" \"$FLEET_USER\")', start)
        matcher = adopt[start:end]
        self.assertIn('/proc/[0-9]*', matcher)
        self.assertIn("awk '/^Uid:/", matcher)
        self.assertIn('hub/web.py', matcher)
        self.assertIn('--no-serve-frontend', matcher)
        self.assertIn('candidate', matcher)

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
            for rel in ('index.html', 'config.js', 'assets/api/client.js',
                        'assets/api/contracts.js', 'assets/realtime/sse.js', 'assets/state/store.js'):
                self.assertIn(rel, copied, rel)
            manifest = json.loads((out / 'manifest.json').read_text())
            self.assertEqual(manifest['version'], '2026-08-24.1')
            # file list = required frontend modules, in stable order, no extras.
            self.assertIn('index.html', manifest['files'])
            self.assertIn('assets/state/store.js', manifest['files'])
            self.assertEqual(sorted(manifest['files']), manifest['files'])
            # manifest only contains version + file list.
            self.assertEqual(set(manifest.keys()), {'version', 'files'})
            # source frontend dir is not copied into the package.
            self.assertNotIn(str(FRONTEND_DIR), copied)

    def test_package_refuses_forbidden_secret_and_state_paths(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / 'frontend'
            (src / 'assets/views').mkdir(parents=True)
            (src / 'credentials').mkdir()
            (src / 'state').mkdir()
            (src / 'index.html').write_text('<html></html>')
            for rel in ('assets/views/fleet.js', 'credentials/runner-credential.json',
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

    def test_release_routing_smoke_matches_current_config(self):
        result = subprocess.run(
            ["bash", str(REPO_ROOT / "deploy/test-release-routing.sh")],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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
        self.assertIn("location = /api/stream", source)
        self.assertIn("proxy_buffering off", source)
        self.assertNotIn("Access-Control-Allow-Origin *", source)

    def test_static_frontend_and_api_backend_are_same_origin(self):
        source = self._read('deploy/nginx-frontend-backend.example.conf')
        # Static frontend release: / and /assets/*.
        self.assertIn('root /srv/agent-fleet/frontend/current', source)
        self.assertIn('upstream fleet_backend', source)
        self.assertIn('location /assets/', source)
        self.assertIn('location /', source)
        # Backend: /api (与 legacy /api/* 兼容), probe/runner 路径。
        self.assertIn('location /api/', source)
        self.assertIn('try_files $uri $uri/ /index.html', source)
        self.assertIn('proxy_set_header Host $host', source)
        self.assertIn('proxy_set_header X-Forwarded-Proto $scheme', source)

    def test_sse_location_keeps_buffering_off_and_long_read_timeout(self):
        source = Path("deploy/nginx-frontend-backend.example.conf").read_text()
        block = self._block_after(source, 'location = /api/stream')
        self.assertIn('proxy_buffering off', block)
        self.assertNotIn('proxy_buffering on', block)
        self.assertIn('proxy_read_timeout', block)
        long_enough = any(token in block for token in
                          ('proxy_read_timeout 3600s', 'proxy_read_timeout 1800s',
                           'proxy_read_timeout 600s'))
        self.assertTrue(long_enough, 'SSE read timeout must be long (>=10min)')
        self.assertIn('proxy_http_version 1.1', block)
        # A location-level proxy_set_header replaces the entire parent set.
        # Keep Connection alongside Host/forwarded identity headers at server scope.
        self.assertIn('proxy_set_header Connection ""', source.split('location /assets/')[0])
        self.assertIn('proxy_set_header Host $host', source.split('location /assets/')[0])
        self.assertNotIn('proxy_set_header', block)

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
            ["git", "ls-files", "frontend/assets/state/store.js"],
            cwd=REPO_ROOT, text=True, capture_output=True, check=True,
        )
        self.assertEqual(tracked.stdout.strip(), "frontend/assets/state/store.js")
        ignored = subprocess.run(
            ["git", "check-ignore", "frontend/assets/state/store.js"],
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
            "deploy/hk-web-process-control.py",
            "tools/__init__.py",
            "tools/result_files.py",
            "tools/session/__init__.py",
            "tools/supervisor/supervisor.py",
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
            self.assertFalse(
                leaked == "hosts.yaml" or leaked.endswith("/hosts.yaml"),
                "example hosts.yaml must not ship in the CI artifact")
        self.assertFalse(any(e.startswith('frontend/') for e in listing),
                         'backend release must not embed the independent UI')
        self.assertFalse(any(e.startswith('state/') or '/state/' in e for e in listing))


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
            with tarfile.open(archive) as packaged:
                self.assertFalse(any(Path(item.name).name.startswith('._') for item in packaged),
                                 'macOS resource forks must not enter the deployable archive')
            # No staging leftovers in the working tree after packaging.
            self.assertFalse(
                (REPO_ROOT / ".RELEASE_ORIGIN.tmp").exists()
                or (REPO_ROOT / ".RELEASE_ORIGIN.tmp.staged").exists()
                or (REPO_ROOT / ".package-release.tmp.tar").exists())


if __name__ == '__main__':
    unittest.main()
