"""Real filesystem/SQLite deployment transactions with injected host boundaries."""
import io
import json
from pathlib import Path
import sqlite3
import tarfile
from types import SimpleNamespace

import pytest
import yaml

from deploy import auto_release as release
from deploy import auto_deploy_ssh as ssh
from deploy.auto_deploy_host import HostHooks
from deploy import auto_deploy_host as host

OLD, NEW = '1' * 40, '2' * 40


class Hooks:
    def __init__(self, live):
        self.live, self.calls = live, []
        self.fail_new = False
        self.fail_old = False

    def preflight(self, backend):
        self.calls.append('preflight')

    def save(self, backup):
        self.calls.append('save')

    def maintenance(self):
        self.calls.append('maintenance')

    def stop(self):
        self.calls.append('stop')

    def idle(self):
        self.calls.append('idle')

    def resume(self):
        self.calls.append('resume')

    def start(self):
        self.calls.append('start')

    def verify(self, sha):
        self.calls.append('verify:' + sha)
        assert 'commit: ' + sha in (self.live / 'RELEASE_ORIGIN').read_text()
        if (sha == NEW and self.fail_new) or (sha == OLD and self.fail_old):
            # A failed new app may already have committed legitimate user data.
            with sqlite3.connect(self.live / 'var/data.db') as db:
                db.execute('INSERT INTO records VALUES (?)', ('after-start',))
            raise RuntimeError('health_failed')

    def publish(self, path):
        self.calls.append('publish:' + ('old' if path is None else path.name))

    def public_verify(self, sha):
        self.calls.append('public:' + sha)

    def fail_closed(self):
        self.calls.append('fail_closed')


def backend(root, sha, run):
    for name in release.REQUIRED:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('new code ' + sha)
    (root / 'RELEASE_ORIGIN').write_text(f'commit: {sha}\nrun: {run}\nurl: https://example.test\n')


@pytest.fixture
def deployment(tmp_path):
    live, stage = tmp_path / 'live', tmp_path / 'stage'
    backend(live, OLD, 90)
    backend(stage / 'backend', NEW, 100)
    (stage / 'release.json').write_text(json.dumps({'sha': NEW, 'run': '100'}))
    front = stage / 'frontend'
    front.mkdir()
    (front / 'index.html').write_text('new frontend')
    (front / 'manifest.json').write_text(json.dumps({'version': NEW, 'files': ['index.html']}))
    (stage / 'backend/fleet-gates.conf').write_text('untrusted defaults')
    for name in ('fleet-gates.conf', 'hosts.yaml', '.env', 'credentials/secret', 'state/live.json'):
        path = live / name
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_text('preserve:' + name)
    (live / 'var').mkdir()
    with sqlite3.connect(live / 'var/data.db') as db:
        db.execute('CREATE TABLE records(value TEXT)')
        db.execute("INSERT INTO records VALUES ('before')")
    config = {'live': str(live), 'state': str(tmp_path / 'state'),
              'frontend_root': str(tmp_path / 'www'), 'databases': ['var/data.db']}
    hooks = Hooks(live)
    return release.Deployment(config, hooks), stage, hooks


def assert_runtime_preserved(live):
    for name in ('fleet-gates.conf', 'hosts.yaml', '.env', 'credentials/secret', 'state/live.json'):
        assert (live / name).read_text() == 'preserve:' + name


def test_deployment_preserves_live_inode_runtime_and_creates_consistent_backup(deployment):
    manager, stage, hooks = deployment
    inode = manager.live.stat().st_ino
    result = manager.apply(stage, NEW, '100')
    assert result['status'] == 'deployed'
    assert manager.live.stat().st_ino == inode
    assert_runtime_preserved(manager.live)
    with sqlite3.connect(Path(result['backup']) / 'databases/var/data.db') as db:
        assert db.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
        assert db.execute('SELECT value FROM records').fetchall() == [('before',)]
    assert hooks.calls.index('stop') < hooks.calls.index('start') < hooks.calls.index('publish:' + NEW)
    assert not (manager.state / 'pending.json').exists()
    hooks.calls.clear()
    assert manager.apply(stage, NEW, '100')['status'] == 'already_deployed'
    assert 'stop' not in hooks.calls


def test_check_only_never_stops_or_changes_live(deployment):
    manager, stage, hooks = deployment
    before = release.tree_digest(manager.live)
    assert manager.apply(stage, NEW, '100', check_only=True)['status'] == 'checked'
    assert release.tree_digest(manager.live) == before
    assert hooks.calls == ['preflight']
    assert not (manager.state / 'pending.json').exists()


def test_failed_health_rolls_back_code_but_never_restores_old_database(deployment):
    manager, stage, hooks = deployment
    hooks.fail_new = True
    with pytest.raises(release.DeployError, match='rolled_back'):
        manager.apply(stage, NEW, '100')
    assert OLD in (manager.live / 'RELEASE_ORIGIN').read_text()
    assert_runtime_preserved(manager.live)
    with sqlite3.connect(manager.live / 'var/data.db') as db:
        assert db.execute('SELECT value FROM records').fetchall() == [('before',), ('after-start',)]
    assert hooks.calls[-1] == 'publish:old'
    assert not (manager.state / 'pending.json').exists()


def test_partial_copy_is_rolled_back(deployment, monkeypatch):
    manager, stage, hooks = deployment
    copy = release.copy_managed

    def fail_copy(src, dst, **kwargs):
        if src == stage / 'backend':
            (dst / 'hub/web.py').write_text('partial update')
            raise OSError('disk_full')
        return copy(src, dst, **kwargs)

    monkeypatch.setattr(release, 'copy_managed', fail_copy)
    with pytest.raises(release.DeployError, match='rolled_back'):
        manager.apply(stage, NEW, '100')
    assert OLD in (manager.live / 'hub/web.py').read_text()
    assert hooks.calls[-1] == 'publish:old'


def test_failed_rollback_blocks_later_deployments(deployment):
    manager, stage, hooks = deployment
    hooks.fail_new = hooks.fail_old = True
    with pytest.raises(release.DeployError, match='rollback_failed'):
        manager.apply(stage, NEW, '100')
    assert (manager.state / 'pending.json').exists()
    assert hooks.calls[-1] == 'fail_closed'
    with pytest.raises(release.DeployError, match='unfinished'):
        manager.apply(stage, NEW, '100')


@pytest.mark.parametrize('failure', ['result', 'pending'])
def test_commit_failure_restores_last_success_and_allows_retry(deployment, monkeypatch, failure):
    manager, stage, hooks = deployment
    previous = {'sha': OLD, 'run': '90'}
    last = manager.state / 'last-success.json'
    release.atomic_json(last, previous)
    write, unlink = release.atomic_json, Path.unlink
    failed = False

    def fail_write(path, value):
        nonlocal failed
        if failure == 'result' and path.name == 'result.json' and not failed:
            failed = True
            raise OSError('disk_full')
        return write(path, value)

    def fail_unlink(path, **kwargs):
        nonlocal failed
        if failure == 'pending' and path.name == 'pending.json' and not failed:
            failed = True
            raise OSError('metadata_unavailable')
        return unlink(path, **kwargs)

    monkeypatch.setattr(release, 'atomic_json', fail_write)
    monkeypatch.setattr(Path, 'unlink', fail_unlink)
    with pytest.raises(release.DeployError, match='rolled_back'):
        manager.apply(stage, NEW, '100')
    assert OLD in (manager.live / 'RELEASE_ORIGIN').read_text()
    assert json.loads(last.read_text()) == previous
    assert not (manager.state / 'pending.json').exists()
    assert manager.apply(stage, NEW, '100')['status'] == 'deployed'


def test_failed_rollback_metadata_still_closes_admissions(deployment, monkeypatch):
    manager, stage, hooks = deployment
    hooks.fail_new = hooks.fail_old = True
    write = release.atomic_json

    def fail_write(path, value):
        if value.get('phase') == 'rollback_failed':
            raise OSError('disk_full')
        return write(path, value)

    monkeypatch.setattr(release, 'atomic_json', fail_write)
    with pytest.raises((release.DeployError, OSError)):
        manager.apply(stage, NEW, '100')
    assert hooks.calls[-1] == 'fail_closed'
    assert (manager.state / 'pending.json').exists()


@pytest.mark.parametrize('tamper', ['frontend', 'origin', 'runtime', 'metadata', 'missing'])
def test_invalid_bundles_cannot_enter_host_hooks(deployment, tamper):
    manager, stage, hooks = deployment
    if tamper == 'frontend':
        (stage / 'frontend/index.html').unlink()
    elif tamper == 'origin':
        (stage / 'backend/RELEASE_ORIGIN').write_text(f'commit: {OLD}\nrun: 100\n')
    elif tamper == 'runtime':
        (stage / 'backend/credentials').mkdir()
    elif tamper == 'metadata':
        (stage / 'release.json').write_text(json.dumps({'sha': OLD, 'run': '100'}))
    else:
        (stage / 'backend/hub/web.py').unlink()
    with pytest.raises(release.DeployError):
        manager.apply(stage, NEW, '100')
    assert hooks.calls == []
    assert OLD in (manager.live / 'RELEASE_ORIGIN').read_text()


@pytest.mark.parametrize('name,kind', [('../outside', 'file'), ('/outside', 'file'),
                                     ('hub/link', 'symlink'), ('hub/hard', 'hardlink'),
                                     ('credentials/password', 'file')])
def test_archive_rejects_traversal_links_and_runtime_paths(tmp_path, name, kind):
    archive = tmp_path / 'bad.tar'
    with tarfile.open(archive, 'w') as tar:
        item = tarfile.TarInfo(name)
        if kind == 'symlink':
            item.type, item.linkname = tarfile.SYMTYPE, '/outside'
        elif kind == 'hardlink':
            item.type, item.linkname = tarfile.LNKTYPE, 'hub/web.py'
        tar.addfile(item, io.BytesIO(b''))
    with pytest.raises(release.DeployError):
        release.safe_extract(archive, tmp_path / 'target')
    assert not (tmp_path / 'target').exists()


def test_stale_run_cannot_replace_newer_release(deployment):
    manager, stage, hooks = deployment
    release.atomic_json(manager.state / 'last-success.json', {'sha': OLD, 'run': '101'})
    with pytest.raises(release.DeployError, match='stale'):
        manager.apply(stage, NEW, '100')
    assert hooks.calls == []


def test_task_arriving_before_maintenance_is_not_terminated(deployment):
    manager, stage, hooks = deployment

    def became_busy():
        raise RuntimeError('active_task')

    hooks.idle = became_busy
    with pytest.raises(release.DeployError, match='rolled_back'):
        manager.apply(stage, NEW, '100')
    assert 'stop' not in hooks.calls and 'start' not in hooks.calls
    assert hooks.calls[-1] == 'resume'
    assert OLD in (manager.live / 'RELEASE_ORIGIN').read_text()


@pytest.mark.parametrize('command', [
    'bash', 'deploy; id', '--job /tmp/job.json',
    f'deploy {NEW} 100 ' + 'a' * 64 + ' 9999999999',
    f'deploy {NEW} 0 ' + 'a' * 64 + ' 10',
    f'deploy {NEW} 100 ' + 'a' * 64 + ' 10\nwhoami',
])
def test_ssh_receiver_rejects_shell_and_unbounded_inputs(command):
    with pytest.raises(release.DeployError, match='invalid_ssh_command'):
        ssh.parse_command(command)


def test_upload_checks_length_and_digest(tmp_path):
    import hashlib
    body = b'bounded upload'
    digest = hashlib.sha256(body).hexdigest()
    assert ssh.parse_command(f'check {NEW} 100 {digest} {len(body)}') == ('check', NEW, '100', digest, len(body))
    ssh.receive(io.BytesIO(body), tmp_path / 'good', len(body), digest)
    assert (tmp_path / 'good').read_bytes() == body
    with pytest.raises(release.DeployError, match='digest'):
        ssh.receive(io.BytesIO(body), tmp_path / 'wrong', len(body), '0' * 64)
    with pytest.raises(release.DeployError, match='truncated'):
        ssh.receive(io.BytesIO(body[:-1]), tmp_path / 'short', len(body), digest)


@pytest.mark.parametrize('failure', ['upload', 'missing_result', 'failed_result'])
def test_receiver_failures_never_report_success_or_leave_partial_upload(tmp_path, monkeypatch, capsys, failure):
    import hashlib
    body = b'bundle'
    config = SimpleNamespace(stat=lambda: SimpleNamespace(st_uid=0, st_mode=0o600),
                             read_text=lambda: json.dumps({'state': str(tmp_path)}))
    monkeypatch.setattr(ssh, 'CONFIG', config)
    monkeypatch.setattr(ssh.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(ssh.sys, 'argv', ['receiver', f'check {NEW} 100 {hashlib.sha256(body).hexdigest()} {len(body)}'])
    monkeypatch.setattr(ssh.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(b'' if failure == 'upload' else body)))

    def finish(argv, **kwargs):
        if failure == 'failed_result':
            release.atomic_json(Path(argv[-1]).parent / 'result.json', {'status': 'failed'})
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(ssh.subprocess, 'run', finish)
    if failure == 'upload':
        with pytest.raises(release.DeployError, match='truncated'):
            ssh.main()
        assert list((tmp_path / 'incoming').iterdir()) == []
    else:
        assert ssh.main() != 0
        result = json.loads(capsys.readouterr().out)
        assert result['status'] in ('failed', 'unconfirmed')


def test_bundle_roundtrip_keeps_same_run_frontend_and_backend(deployment, tmp_path):
    _, stage, _ = deployment
    archive = tmp_path / 'backend.tgz'
    with tarfile.open(archive, 'w:gz') as tar:
        for path in sorted((stage / 'backend').iterdir()):
            tar.add(path, arcname=path.name)
    bundle = tmp_path / 'bundle.tgz'
    release.build_bundle(archive, stage / 'frontend', bundle, NEW, '100')
    decoded = tmp_path / 'decoded'
    release.safe_extract(bundle, decoded, bundle=True)
    release.validate_stage(decoded, NEW, '100')
    assert release.tree_digest(decoded) == release.tree_digest(stage)


def test_nginx_cutover_and_restore_keep_original_account_proxy_config(tmp_path):
    nginx = tmp_path / 'site.conf'
    original = 'server {\n    root /var/www/previous;\n    location /api/ { proxy_pass http://127.0.0.1:8790; }\n}\n'
    nginx.write_text(original)
    marker = tmp_path / 'maintenance'
    hooks = HostHooks({'nginx_config': str(nginx), 'maintenance_marker': str(marker)})
    calls = []
    hooks.run = lambda command, **kwargs: calls.append(command)
    hooks.maintenance()
    assert marker.exists() and 'return 503;' in nginx.read_text()
    hooks.publish(Path('/var/www') / NEW)
    assert 'root /var/www/' + NEW in nginx.read_text()
    assert 'proxy_pass http://127.0.0.1:8790;' in nginx.read_text()
    hooks.resume()
    assert nginx.read_text() == original and not marker.exists()
    assert calls.count(['nginx', '-t']) == 3


@pytest.mark.parametrize('first_response', ['maintenance', 'old_version'])
def test_public_probe_waits_for_nginx_reload_to_take_effect(tmp_path, first_response):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            status = 503 if len(requests) == 1 and first_response == 'maintenance' else 200
            version = OLD if len(requests) == 1 else NEW
            body = json.dumps({'version': version}).encode()
            self.send_response(status)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    nginx = tmp_path / 'nginx.conf'
    nginx.write_text('server {\n root /var/www/old;\n}\n')
    hooks = HostHooks({'nginx_config': str(nginx), 'maintenance_marker': str(tmp_path / 'marker'),
                       'public_origin': f'http://127.0.0.1:{server.server_port}'})
    try:
        hooks.public_verify(NEW)
        assert requests == ['/manifest.json', '/manifest.json']
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


@pytest.mark.parametrize('response', ['unavailable', 'wrong_version', 'late_success'])
def test_public_probe_has_one_deadline_and_rejects_late_success(tmp_path, monkeypatch, response):
    nginx = tmp_path / 'nginx.conf'
    nginx.write_text('server {\n root /var/www/old;\n}\n')
    hooks = HostHooks({'nginx_config': str(nginx), 'maintenance_marker': str(tmp_path / 'marker'),
                       'public_origin': 'https://example.test'})
    now, calls = [0.0], []
    monkeypatch.setattr(host.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(host.time, 'sleep', lambda seconds: now.__setitem__(0, now[0] + seconds))

    def request(argv, *, timeout):
        calls.append(timeout)
        assert 0 < timeout <= .1 - now[0]
        assert float(argv[argv.index('--max-time') + 1]) == timeout
        now[0] += .2 if response == 'late_success' else .02
        if response == 'unavailable':
            raise release.DeployError('host_command_failed:curl')
        return json.dumps({'version': NEW if response == 'late_success' else OLD}).encode()

    hooks.run = request
    with pytest.raises(release.DeployError, match='public_frontend_not_ready'):
        hooks.public_verify(NEW, timeout=.1)
    assert calls and len(calls) <= 2


def test_ci_deploy_is_main_only_after_tests_and_same_run_packaging():
    workflow = yaml.safe_load((Path(__file__).resolve().parents[1] / '.github/workflows/ci.yml').read_text())
    job = workflow['jobs']['deploy']
    assert set(job['needs']) == {'test', 'package'}
    assert "github.ref == 'refs/heads/main'" in job['if']
    assert "github.event_name == 'push'" in job['if']
    assert job['environment'] == 'production'
    assert job['concurrency']['cancel-in-progress'] is False
    downloads = [step['with'] for step in job['steps'] if step.get('uses', '').startswith('actions/download-artifact@')]
    assert {item['name'] for item in downloads} == {'agent-fleet-release', 'agent-fleet-frontend'}
    assert all('run-id' not in item and 'repository' not in item for item in downloads)
    steps = '\n'.join(step.get('run', '') for step in job['steps'])
    assert 'StrictHostKeyChecking=yes' in steps
    assert 'git/ref/heads/main' in steps
    assert 'systemctl' not in steps and 'docker ' not in steps  # Receiver owns privileged operations.
    for name in ('test', 'package'):
        assert 'secrets.' not in json.dumps(workflow['jobs'][name])
