"""Real account HTTP, SQLite, scheduler, provider factory and workspace fixtures."""
import http.client
import json
import time
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit

from werkzeug.serving import make_server
from hub.bootstrap import create_app, start_platform_run_scheduler
from hub.config import FleetConfig

PASSWORD = 'Full-flow-password-123!'
ROOT = Path(__file__).resolve().parents[2]


class Client:
    def __init__(self, origin):
        self.origin, self.cookie = origin, None

    def request(self, path, body=None, *, expected=200, headers=None):
        origin = urlsplit(self.origin)
        c = http.client.HTTPConnection(origin.hostname, origin.port, timeout=10)
        h = {'Origin': self.origin, 'Content-Type': 'application/json'}
        if self.cookie:
            h['Cookie'] = self.cookie
        h.update(headers or {})
        try:
            c.request('GET' if body is None else 'POST', path,
                      None if body is None else json.dumps(body), h)
            response = c.getresponse()
            raw = response.read()
            if response.getheader('Set-Cookie'):
                self.cookie = response.getheader('Set-Cookie').split(';', 1)[0]
            assert response.status == expected, (path, response.status, expected)
            if response.getheader('Content-Type', '').startswith('application/json'):
                return json.loads(raw)
            return raw
        finally:
            c.close()

    def login(self, email='mango', password=PASSWORD):
        return self.request('/api/accounts/login', {'email': email, 'password': password})['user']

    def submit(self, prompt='full flow', *, conversation=None, token='flow-turn', model=None):
        if conversation is None:
            conversation = self.request('/api/platform/v1/conversations',
                                        {'title': 'Disposable full-flow test', 'workspace_id': 'home'})['conversation']['conversation_id']
        payload = {'text': prompt, 'client_token': token}
        if model:
            payload['overrides'] = {'model_profile_id': model}
        return self.request('/api/platform/v1/conversations/' + conversation + '/turns', payload, expected=202)

    def terminal(self, run_id, *, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run = self.request('/api/platform/v1/runs/' + run_id)
            if run['state'] in ('succeeded', 'failed', 'unknown', 'cancelled'):
                return run
            time.sleep(.05)
        raise AssertionError('run did not reach a terminal state')


class FullFlowHub:
    def __init__(self, root, provider, *, browser=False):
        self.root, self.provider = root, provider
        self.mail = []
        self.mailbox = root / 'mail.json'
        self.clock = [time.time()]
        self.browser = browser
        self.shutdown_scheduler = None
        self.server = None

    def sender(self, *args):
        self.mail.append(args)
        self.mailbox.write_text(json.dumps(self.mail))
        self.clock[0] += 61

    def provision(self, owner):
        repo = self.app.extensions['fleet']['platform_repository']
        workspace = self.root / 'workspaces' / owner
        workspace.mkdir(parents=True, exist_ok=True)
        for model in ('full-flow', 'read-only', 'broken'):
            repo.upsert_model(owner, {'profile_id': model, 'provider': 'openai_compatible', 'model': model,
                'secret_ref': 'env://FLEET_FULL_FLOW_SECRET',
                'provider_config': {'endpoint': self.provider.url, 'max_retries': 0, 'timeout_s': 10 if self.browser else 1}})
        repo.upsert_workspace(owner, {'workspace_id': 'home', 'root_path': str(workspace)})
        if not repo.get_defaults(owner).get('model_profile_id'):
            repo.update_defaults(owner, {'model_profile_id': 'full-flow', 'workspace_id': 'home'}, 0)
        return workspace

    def start(self, *, scheduler=True, port=0):
        self.app = create_app(FleetConfig.from_root(self.root,
            frontend_dir=ROOT / 'frontend', platform_enabled=True, platform_worker_enabled=True,
            platform_worker_scheduler_enabled=True, platform_provider_network_enabled=True,
            service_monitoring_enabled=True,
            accounts={'enabled': True, 'origin': 'http://127.0.0.1', 'registration': 'invite', 'sender': self.sender}))
        accounts = self.app.extensions['accounts']
        accounts.clock = lambda: self.clock[0]
        if not accounts.users():
            accounts.bootstrap_admin('admin@example.test', PASSWORD, login_name='mango')
        self.owner = next(u['id'] for u in accounts.users() if u['email'] == 'admin@example.test')
        self.workspace = self.provision(self.owner)
        if self.browser:
            @self.app.post('/__test/release-provider')
            def release_provider():
                self.provider.release.set()
                return {'ok': True}
        self.server = make_server('127.0.0.1', port, self.app, threaded=True)
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        self.app.config['ACCOUNT_ORIGIN'] = self.origin
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        if scheduler:
            self.start_scheduler()
        return self

    def start_scheduler(self):
        self.shutdown_scheduler = start_platform_run_scheduler(self.app, interval_s=.1)

    def stop(self):
        self.provider.release.set()
        if self.shutdown_scheduler:
            self.shutdown_scheduler()
            self.shutdown_scheduler = None
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(5)
            assert not self.thread.is_alive()
            self.server = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.stop()
