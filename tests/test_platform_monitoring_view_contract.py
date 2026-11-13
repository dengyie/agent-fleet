from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"


def test_monitoring_api_wrappers_use_bounded_platform_boundary():
    source = (FRONTEND / "assets/api/platform.js").read_text(encoding="utf-8")
    client_source = (FRONTEND / "assets/api/client.js").read_text(encoding="utf-8")
    assert "export async function getPlatformServices" in source
    assert "export async function getPlatformService" in source
    assert "export async function getPlatformIncidents" in source
    assert "platformRequest(apiPath('platform', 'v1', 'services')" in source
    assert "platformRequest(apiPath('platform', 'v1', 'services', segment(serviceId)))" in source
    assert "Math.min(100" in source and "Math.min(50" in source
    assert "state === 'open' || state === 'closed'" in source
    assert "path.indexOf(base + '/') === 0" in client_source
    assert "path = path.slice(base.length) || '/'" in client_source


def test_monitoring_view_and_shell_route_keep_read_only_health_surface():
    view = FRONTEND / "assets/views/monitoring.js"
    assert view.is_file()
    source = view.read_text(encoding="utf-8")
    shell = (FRONTEND / "index.html").read_text(encoding="utf-8") + (FRONTEND / "assets/app.js").read_text()
    assert "export function mountMonitoring" in source
    assert "getPlatformServices" in source
    assert "getPlatformService" in source
    assert "getPlatformIncidents" in source
    assert "innerHTML" not in source
    assert "restart" in source.lower()
    assert "komari" not in source.lower()
    assert "data-nav=\"monitoring\"" in shell
    assert "route-view" in shell
    assert "'monitoring'" in shell
    assert "mountMonitoring" in shell


def test_monitoring_view_is_bounded_and_does_not_render_sensitive_fields():
    source = (FRONTEND / "assets/views/monitoring.js").read_text(encoding="utf-8")
    assert "slice(0, 100)" in source
    assert "slice(0, 5)" in source
    assert "slice(0, 50)" in source
    assert "textContent" in source
    assert "argv" not in source
    assert "credential" not in source
    assert "result" not in source
