"""Host-specific Docker/Nginx hooks, configured outside the public repository."""
import json
import os
from pathlib import Path
import re
import subprocess
import time

try:
    from .auto_release import DeployError
except ImportError:  # Installed standalone helper.
    from auto_release import DeployError


class HostHooks:
    def __init__(self, config):
        self.config = config
        self.nginx = Path(config['nginx_config'])
        self.original = self.nginx.read_text()
        self.marker = Path(config['maintenance_marker'])
        if self.marker.exists():
            raise DeployError('existing_maintenance_barrier')
        if len(re.findall(r'^\s*root\s+[^;]+;', self.original, re.M)) != 1:
            raise DeployError('nginx_root_ambiguous')

    def run(self, argv, *, input=None, timeout=60):
        result = subprocess.run(argv, input=input, capture_output=True, timeout=timeout)
        if result.returncode:
            raise DeployError('host_command_failed:' + Path(argv[0]).name)
        return result.stdout

    def container(self, mode, **extra):
        data = {'mode': mode, 'live': self.config['container_live'],
                'port': self.config['port'], 'public_origin': self.config['public_origin'], **extra}
        if mode in ('preflight', 'verify'):
            path = Path(self.config['account_credentials'])
            if path.stat().st_mode & 0o077:
                raise DeployError('account_credentials_not_private')
            data['credentials'] = json.loads(path.read_text())
        # The helper is installed outside the release and contains no product imports.
        helper = Path(__file__).with_name('auto_deploy_container.py').read_bytes()
        self.run(['docker', 'exec', '-i', '-u', self.config['container_user'], self.config['container'],
                  'python3', '-c', helper.decode()], input=json.dumps(data).encode())

    def preflight(self, backend):
        self.container('preflight')
        path = self.run(['docker', 'exec', '-u', 'root', self.config['container'],
                         'mktemp', '-d', '/tmp/agent-fleet-ci.XXXXXXXX']).decode().strip()
        if not re.fullmatch(r'/tmp/agent-fleet-ci\.[A-Za-z0-9]{8}', path):
            raise DeployError('invalid_stage_directory')
        try:
            self.run(['docker', 'exec', '-u', 'root', self.config['container'], 'chmod', '755', path])
            self.run(['docker', 'cp', str(backend) + '/.', self.config['container'] + ':' + path])
            self.container('stage', stage=path)
        finally:
            self.run(['docker', 'exec', '-u', 'root', self.config['container'], 'rm', '-rf', path])

    def save(self, backup):
        (backup / 'nginx.conf').write_text(self.original)

    def write_nginx(self, text):
        previous = self.nginx.read_text()
        self.nginx.write_text(text)
        try:
            self.run(['nginx', '-t'])
            self.run(['systemctl', 'reload', 'nginx'])
        except BaseException:
            self.nginx.write_text(previous)
            self.run(['nginx', '-t'])
            self.run(['systemctl', 'reload', 'nginx'])
            raise

    def maintenance(self):
        self.marker.touch(exist_ok=True)
        self.write_nginx(re.sub(r'(^\s*root\s+[^;]+;)', r'\1\n    return 503;', self.original, count=1, flags=re.M))

    def stop(self):
        self.container('stop')

    def idle(self):
        self.container('idle')

    def resume(self):
        self.marker.unlink(missing_ok=True)
        self.publish(None)

    def start(self):
        self.marker.unlink(missing_ok=True)
        self.run(['docker', 'exec', '-u', self.config['container_user'], self.config['container'],
                  'bash', self.config['launcher']])

    def verify(self, sha):
        self.container('verify', sha=sha)

    def publish(self, frontend):
        text = self.original
        if frontend is not None:
            text = re.sub(r'(^\s*root\s+)[^;]+;', lambda match: match[1] + str(frontend) + ';', text, count=1, flags=re.M)
        self.write_nginx(text)

    def public_verify(self, sha, *, timeout=30):
        # Nginx reload returns before new workers take over. Requests can still
        # reach the maintenance response or previous root during that interval.
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            budget = min(5, remaining)
            manifest = None
            try:
                raw = self.run(['curl', '--fail', '--silent', '--show-error', '--max-time', str(budget),
                                self.config['public_origin'] + '/manifest.json'], timeout=budget)
                manifest = json.loads(raw)
            except (DeployError, subprocess.TimeoutExpired, json.JSONDecodeError):
                pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if isinstance(manifest, dict) and manifest.get('version') == sha:
                return
            time.sleep(min(.25, remaining))
        raise DeployError('public_frontend_not_ready')

    def fail_closed(self):
        self.maintenance()
