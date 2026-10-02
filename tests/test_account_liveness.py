import asyncio
from pathlib import Path
import subprocess
import threading

import pytest
from werkzeug.serving import make_server

from hub.agent_fleet_guardian import AgentFleetGuardian
from hub.bootstrap import create_app
from hub.config import FleetConfig


@pytest.mark.parametrize('accounts_enabled', [False, True])
def test_liveness_is_private_data_free_and_independent_of_login(tmp_path, accounts_enabled):
    app = create_app(FleetConfig.from_root(tmp_path, serve_frontend=False,
        accounts={'enabled': accounts_enabled, 'origin': 'https://fleet.example.test'}))
    client = app.test_client()
    response = client.get('/healthz')
    assert response.status_code == 200
    assert response.json == {'status': 'ok'}
    assert response.headers['Cache-Control'] == 'no-store'
    assert client.get('/api/operator/session').status_code == 401
    if accounts_enabled:
        assert client.get('/api/status').status_code == 401


def test_both_guardians_probe_real_account_hub_without_credentials(tmp_path):
    app = create_app(FleetConfig.from_root(tmp_path, serve_frontend=False,
        accounts={'enabled': True, 'origin': 'https://fleet.example.test'}))
    server = make_server('127.0.0.1', 0, app)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        guardian = AgentFleetGuardian(repo_root=str(tmp_path), web_port=server.server_port)
        assert asyncio.run(guardian.check_health()) == 200
        # Exercise the deployed shell guardian's actual probe against the same Hub.
        source = (Path(__file__).resolve().parents[1] / 'deploy/hk-self-report-loop.sh').read_text()
        start = source.index('health_check() {')
        probe = source[start:source.index('\n}', start) + 2]
        result = subprocess.run(['bash', '-c', probe + '\nweb_port=$1\nhealth_check',
            'probe', str(server.server_port)], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == '200'
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
