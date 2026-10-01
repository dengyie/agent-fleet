"""Static presentation is independent of API state and template rendering."""
from pathlib import Path
import pytest
from hub.bootstrap import create_app
from hub.config import FleetConfig

ROOT = Path(__file__).resolve().parents[1]

@pytest.mark.parametrize('route', ['/', '/index.html', '/login', '/assistant', '/monitoring', '/machine/example', '/task/example', '/session/example', '/conversation/example'])
def test_pages_are_the_exact_static_release(tmp_path, route):
    app = create_app(FleetConfig.from_root(tmp_path, frontend_dir=ROOT / 'frontend', serve_frontend=True))
    response = app.test_client().get(route)
    assert response.status_code == 200
    assert response.data == (ROOT / 'frontend/index.html').read_bytes()
    assert 'no-cache' in response.headers['Cache-Control']
    response.close()

def test_api_only_app_needs_no_frontend(tmp_path):
    app = create_app(FleetConfig.from_root(tmp_path, serve_frontend=False))
    client = app.test_client()
    assert client.get('/api/status').status_code == 200
    for route in ['/', '/login', '/assistant', '/config.js', '/assets/app.js', '/static/app.js']:
        assert client.get(route).status_code == 404


def test_static_and_api_namespaces_do_not_overlap(tmp_path):
    app = create_app(FleetConfig.from_root(tmp_path, frontend_dir=ROOT / 'frontend'))
    client = app.test_client()
    response = client.get('/assets/api/client.js')
    assert response.status_code == 200
    assert 'javascript' in response.content_type
    response.close()
    for route in ['/api/client.js', '/api/missing']:
        response = client.get(route)
        assert response.status_code == 404
        assert response.is_json
    for route in ['/assets/../config.js', '/assets/%2e%2e/config.js', '/assets/missing.js']:
        assert client.get(route).status_code == 404


def test_asset_symlink_cannot_escape_release(tmp_path):
    (tmp_path / 'assets').mkdir()
    outside = tmp_path / 'private.js'
    outside.write_text('private fixture')
    (tmp_path / 'assets/leak.js').symlink_to(outside)
    app = create_app(FleetConfig.from_root(tmp_path / 'state-root', frontend_dir=tmp_path))
    assert app.test_client().get('/assets/leak.js').status_code == 404
