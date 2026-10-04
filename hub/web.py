#!/usr/bin/env python3
"""hub/web.py - agent-fleet Web 总览服务 and compatibility entrypoint.

The application assembly lives in :mod:`hub.bootstrap`; this module keeps the
legacy CLI, helper functions, and ``make_app`` signature stable for callers.
"""
import json
import os
import time
from pathlib import Path

# Direct ``python hub/web.py`` execution has no package import context. Keep
# this compatibility shim local to the legacy entrypoint; package imports and
# all application modules remain free of path mutation.
if __package__ in (None, ""):
    import importlib.util
    import sys
    import types

    FLEET_HOME = Path(__file__).resolve().parent.parent
    package = types.ModuleType("hub")
    package.__path__ = [str(Path(__file__).resolve().parent)]
    sys.modules.setdefault("hub", package)

    # Direct execution has ``hub/`` as sys.path[0], which would shadow the
    # stdlib ``http`` package with the new ``hub/http`` adapters (and break
    # werkzeug/flask imports). Swap the script dir for the repository root; the
    # ``hub`` package is already registered in sys.modules so nothing regresses.
    _script_dir = str(Path(__file__).resolve().parent)
    while _script_dir in sys.path:
        sys.path.remove(_script_dir)
    _root = str(FLEET_HOME)
    if _root not in sys.path:
        sys.path.insert(0, _root)

    # Register the one repository-root module imported by the legacy blueprints
    # by name; with FLEET_HOME on sys.path this just anchors it explicitly.
    schema_name = "report_schema"
    schema_path = FLEET_HOME / "report_schema.py"
    schema_spec = importlib.util.spec_from_file_location(schema_name, schema_path)
    if schema_spec is None or schema_spec.loader is None:
        raise ImportError(f"cannot load {schema_name} from {schema_path}")
    schema_module = importlib.util.module_from_spec(schema_spec)
    sys.modules.setdefault(schema_name, schema_module)
    schema_spec.loader.exec_module(schema_module)
else:
    FLEET_HOME = Path(__file__).resolve().parent.parent
STATE_DIR = FLEET_HOME / "state"
HOSTS_FILE = FLEET_HOME / "hosts.yaml"
INGEST_TOKEN_FILE = FLEET_HOME / "credentials" / "ingest-token"
INGEST_TOKEN_ENV = "AGENT_FLEET_INGEST_TOKEN"

try:
    from flask import Flask
    HAVE_FLASK = True
except ImportError:
    HAVE_FLASK = False

from hub.auth import MACHINE_RE, resolve_ingest_token


def load_hosts():
    """Load display metadata from the configured hosts file."""
    try:
        import yaml

        data = yaml.safe_load(HOSTS_FILE.read_text()) if HOSTS_FILE.exists() else {}
    except (OSError, ImportError, ValueError):
        data = {}
    hosts = []
    for host in (data or {}).get("hosts", []):
        if not isinstance(host, dict) or "name" not in host:
            continue
        hosts.append({
            "name": host["name"],
            "desc": host.get("desc", ""),
            "transport": host.get("transport", "ingest"),
        })
    return hosts


def collect_all():
    """Trigger ingest stale reconciliation; no machine commands are executed."""
    try:
        from hub import scan

        scan.scan_all()
    except Exception:
        pass
    return True


def start_reconciliation(interval_s=60):
    """Legacy daemon: periodic ingest stale-reconciliation.

    Compatibility wrapper around the push-only daemon runner in
    :mod:`hub.application.reconciliation_service`. Keeps the legacy
    ``interval_s``-only signature; the running app should use
    ``hub.bootstrap.start_background_jobs`` instead.
    """
    interval_s = max(10, int(interval_s))
    from hub.application.reconciliation_service import (
        start_reconciliation as _start_reconciliation,
    )
    return _start_reconciliation(collect_all, interval_s=interval_s)


def reconcile_leases_once(now=None):
    """单轮 lease/任务过期处理；返回重派的 task_id 列表。"""
    from hub import events as ev
    from hub import task_store

    requeued = task_store.expire_leases(now=now)
    for task_id in requeued:
        try:
            ev.emit("task_update", task_id=task_id, state="queued")
        except Exception:
            pass
    task_store.expire_tasks(now=now)
    return requeued


def start_lease_reconciler(interval_s=60):
    """Legacy daemon: periodic lease expiry / requeue.

    Compatibility wrapper over :mod:`hub.application.reconciliation_service`;
    running apps should use ``hub.bootstrap.start_background_jobs``.
    """
    interval_s = max(10, int(interval_s))
    from hub.application.reconciliation_service import (
        start_lease_reconciler as _start_lease_reconciler,
    )
    return _start_lease_reconciler(reconcile_leases_once, interval_s=interval_s)


#: Runtime feature gates for the additive Task 6/9 adoption + supervision
#: surfaces.  Each switch defaults OFF (identical surface to pre-Task-6/9),
#: matching the plan's "各阶段通过关闭 `adoption_repositories_enabled` / probe
#: adoption dispatch 或回滚该阶段提交恢复旧流量路径" rollout gate.  Values are
#: read from env (``AGENT_FLEET_*_ENABLED``) OR explicit ``make_app`` kwargs;
#: an explicit False wins, an absent value stays off — nothing flips on by
#: default.  When sessions/adoption are switched on, the stores live under
#: ``<FLEET_HOME>/var`` (persistent in the release dir); no key on the host
#: means raw capture is fail-closed while redacted/audit surfaces work.
_SESSION_ENABLE_ENV = "AGENT_FLEET_SESSION_REPOSITORIES_ENABLED"
_SUPERVISOR_ENABLE_ENV = "AGENT_FLEET_SUPERVISOR_ENABLED"
_ADOPTION_ENABLE_ENV = "AGENT_FLEET_ADOPTION_REPOSITORIES_ENABLED"
_APPEND_TURN_ENABLE_ENV = "AGENT_FLEET_APPEND_USER_TURN_ENABLED"
_APPLY_PROFILE_ENABLE_ENV = "AGENT_FLEET_APPLY_LOCAL_PROFILE_ENABLED"
_SERVICE_MONITORING_ENABLE_ENV = "AGENT_FLEET_SERVICE_MONITORING_ENABLED"
_SERVICE_ACTIONS_ENABLE_ENV = "AGENT_FLEET_SERVICE_ACTIONS_ENABLED"
_EXECUTION_WINDOWS_ENABLE_ENV = "AGENT_FLEET_EXECUTION_WINDOWS_ENABLED"
_KOMARI_ENABLE_ENV = "AGENT_FLEET_KOMARI_ENABLED"
_KOMARI_NETWORK_ENABLE_ENV = "AGENT_FLEET_KOMARI_NETWORK_ENABLED"
_KOMARI_SYNC_ENABLE_ENV = "AGENT_FLEET_KOMARI_SYNC_ENABLED"
_KOMARI_BASE_URL_ENV = "AGENT_FLEET_KOMARI_BASE_URL"
_KOMARI_NODES_PATH_ENV = "AGENT_FLEET_KOMARI_NODES_PATH"
_KOMARI_TOKEN_ENV = "AGENT_FLEET_KOMARI_TOKEN"
_KOMARI_NODE_MAPPING_ENV = "AGENT_FLEET_KOMARI_NODE_MAPPING"
_KOMARI_SYNC_INTERVAL_ENV = "AGENT_FLEET_KOMARI_SYNC_INTERVAL_S"
_HTTP_PROBE_ENABLE_ENV = "AGENT_FLEET_HTTP_PROBE_ENABLED"
_HTTP_PROBE_NETWORK_ENABLE_ENV = "AGENT_FLEET_HTTP_PROBE_NETWORK_ENABLED"
_HTTP_PROBE_SYNC_ENABLE_ENV = "AGENT_FLEET_HTTP_PROBE_SYNC_ENABLED"
_HTTP_PROBE_SYNC_INTERVAL_ENV = "AGENT_FLEET_HTTP_PROBE_SYNC_INTERVAL_S"
_HTTP_PROBE_ALLOWED_ORIGINS_ENV = "AGENT_FLEET_HTTP_PROBE_ALLOWED_ORIGINS"
_HTTP_PROBE_ALLOW_LOOPBACK_ENV = "AGENT_FLEET_HTTP_PROBE_ALLOW_LOOPBACK"
_INCIDENT_RECOVERY_REQUIRED_ENV = "AGENT_FLEET_INCIDENT_RECOVERY_REQUIRED"
_PLATFORM_WORKER_ENABLE_ENV = "AGENT_FLEET_PLATFORM_WORKER_ENABLED"
_PLATFORM_WORKER_SCHEDULER_ENABLE_ENV = "AGENT_FLEET_PLATFORM_WORKER_SCHEDULER_ENABLED"
_PLATFORM_WORKER_INTERVAL_ENV = "AGENT_FLEET_PLATFORM_WORKER_INTERVAL_S"
_PLATFORM_WORKER_LEASE_ENV = "AGENT_FLEET_PLATFORM_WORKER_LEASE_S"
_PLATFORM_WORKER_MAX_CONCURRENCY_ENV = "AGENT_FLEET_PLATFORM_WORKER_MAX_CONCURRENCY"
_PLATFORM_WORKER_MAX_WORKSPACE_CONCURRENCY_ENV = "AGENT_FLEET_PLATFORM_WORKER_MAX_WORKSPACE_CONCURRENCY"
_PLATFORM_PROVIDER_NETWORK_ENABLE_ENV = "AGENT_FLEET_PLATFORM_PROVIDER_NETWORK_ENABLED"
_PLATFORM_MEMORY_ENABLE_ENV = "AGENT_FLEET_PLATFORM_MEMORY_ENABLED"
_PLATFORM_MEMORY_CONTEXT_ENABLE_ENV = "AGENT_FLEET_PLATFORM_MEMORY_CONTEXT_ENABLED"
_PLATFORM_BROWSER_ENABLE_ENV = "AGENT_FLEET_PLATFORM_BROWSER_ENABLED"
_PLATFORM_BROWSER_NETWORK_ENABLE_ENV = "AGENT_FLEET_PLATFORM_BROWSER_NETWORK_ENABLED"
_PLATFORM_BROWSER_ORIGINS_ENV = "AGENT_FLEET_PLATFORM_BROWSER_ALLOWED_ORIGINS"

#: Optional per-release gate file, ``<FLEET_HOME>/fleet-gates.conf``, with
#: ``AGENT_FLEET_*_ENABLED=1`` lines.  It is ONLY consulted when the matching
#: env var is absent, so a production runner that can write the release dir
#: (but cannot add env vars to the guardian-spawned web) flips a gate by
#: dropping/editing this file before the web (re)starts.  An explicit
#: ``make_app`` kwarg still wins over both.  The path is resolved from the
#: module-level ``FLEET_HOME`` at call time so tests/the harness can keep the
#: whole run inside a temp sandbox by reassigning ``web.FLEET_HOME``.
_FEATURE_GATE_FILE = "fleet-gates.conf"


def _gate_conf_text(env: str) -> str:
    """Return the ``KEY=VALUE`` value from ``fleet-gates.conf`` for ``env``.

    Missing/unreadable files and unknown keys yield ``""`` (off).  Lines are
    ``#``/``;``-commentable and ``KEY=VALUE``-split on the first ``=``.
    """
    try:
        raw = (FLEET_HOME / _FEATURE_GATE_FILE).read_text()
    except OSError:
        return ""
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        key, sep, val = line.partition("=")
        if sep and key.strip() == env:
            return val.strip()
    return ""


def _feature_on(value: bool | None, *, env: str) -> bool:
    """Resolve the boolean feature switch from an explicit arg, env, or file.

    ``None`` (the default) falls back to the ``AGENT_FLEET_*_ENABLED``
    environment variable, then to the ``fleet-gates.conf`` line for the same
    key; either way ``1``/``true``/``yes``/``on`` (case-insensitive) are
    truthy and everything else is off.  An explicit bool always wins — the
    compatibility callers that pass no value keep the current default.
    """
    if value is not None:
        return bool(value)
    text = os.environ.get(env, "") or _gate_conf_text(env)
    return text.strip().lower() in ("1", "true", "yes", "on")


def _optional_setting(value, *, env: str):
    """Resolve an optional runtime setting without printing its value."""
    if value is not None:
        return value
    return os.environ.get(env) or None


def _bounded_float(value, *, default: float, lower: float, upper: float) -> float:
    try:
        parsed = float(default if value is None else value)
    except (TypeError, ValueError):
        parsed = default
    if not (parsed == parsed) or parsed in (float("inf"), float("-inf")):
        parsed = default
    return max(lower, min(upper, parsed))


def _bounded_int(value, *, default: int, lower: int, upper: int) -> int:
    try:
        parsed = int(default if value is None else value)
    except (TypeError, ValueError):
        parsed = default
    return max(lower, min(upper, parsed))


def _node_mapping(value):
    value = _optional_setting(value, env=_KOMARI_NODE_MAPPING_ENV)
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def make_app(ingest_token=None, require_token=True, dev_operator=None,
             runner_credentials=None, project_whitelist=None,
             serve_frontend=True, frontend_dir=None,
             session_repositories_enabled=None,
             supervisor_enabled=None,
             adoption_repositories_enabled=None,
             append_user_turn_enabled=None,
             apply_local_profile_enabled=None,
             platform_enabled=None, platform_db=None,
             service_monitoring_enabled=None, komari_enabled=None,
             komari_network_enabled=None,
             service_actions_enabled=None,
             execution_windows_enabled=None,
             komari_base_url=None, komari_nodes_path=None, komari_token=None,
             komari_node_mapping=None, komari_sync_enabled=None,
             komari_sync_interval_s=None, incident_recovery_required=None,
             http_probe_enabled=None, http_probe_network_enabled=None,
             http_probe_sync_enabled=None, http_probe_sync_interval_s=None,
             http_probe_allowed_origins=None, http_probe_allow_loopback=None,
             platform_worker_enabled=None, platform_worker_interval_s=None,
             platform_worker_lease_s=None, platform_worker_scheduler_enabled=None,
             platform_worker_max_concurrency=None, platform_sandbox_launcher=None,
             platform_worker_max_workspace_concurrency=None,
             platform_provider_network_enabled=None,
             platform_remote_execution_enabled=None,
             platform_browser_enabled=None, platform_browser_network_enabled=None,
             platform_browser_allowed_origins=None,
             platform_memory_enabled=None,
             platform_memory_context_enabled=None):
    """Compatibility wrapper around ``hub.bootstrap.create_app``.

    ``session_repositories_enabled`` / ``supervisor_enabled`` /
    ``adoption_repositories_enabled`` are the runtime feature gates, each
    falling back to ``AGENT_FLEET_*_ENABLED`` env (default False).  Passing
    an explicit bool overrides the env; omitting keeps today's surface.
    """
    from hub.bootstrap import create_app
    from hub import events, state, task_store
    from hub.auth import resolve_ingest_token as resolve_token
    from hub.config import FleetConfig

    resolved_token = resolve_token(ingest_token)
    if require_token and not resolved_token:
        raise RuntimeError(
            "AGENT_FLEET_INGEST_TOKEN or credentials/ingest-token is required"
        )
    session_on = _feature_on(
        session_repositories_enabled, env=_SESSION_ENABLE_ENV)
    supervisor_on = _feature_on(
        supervisor_enabled, env=_SUPERVISOR_ENABLE_ENV)
    adoption_on = _feature_on(
        adoption_repositories_enabled, env=_ADOPTION_ENABLE_ENV)
    append_turn_on = _feature_on(
        append_user_turn_enabled, env=_APPEND_TURN_ENABLE_ENV)
    apply_profile_on = _feature_on(
        apply_local_profile_enabled, env=_APPLY_PROFILE_ENABLE_ENV)
    platform_on = _feature_on(
        platform_enabled, env="AGENT_FLEET_PLATFORM_ENABLED")
    service_monitoring_on = _feature_on(
        service_monitoring_enabled, env=_SERVICE_MONITORING_ENABLE_ENV)
    service_actions_on = _feature_on(
        service_actions_enabled, env=_SERVICE_ACTIONS_ENABLE_ENV)
    execution_windows_on = _feature_on(
        execution_windows_enabled, env=_EXECUTION_WINDOWS_ENABLE_ENV)
    komari_on = _feature_on(komari_enabled, env=_KOMARI_ENABLE_ENV)
    komari_network_on = _feature_on(
        komari_network_enabled, env=_KOMARI_NETWORK_ENABLE_ENV)
    komari_sync_on = _feature_on(
        komari_sync_enabled, env=_KOMARI_SYNC_ENABLE_ENV)
    komari_base_url = _optional_setting(
        komari_base_url, env=_KOMARI_BASE_URL_ENV)
    komari_nodes_path = _optional_setting(
        komari_nodes_path, env=_KOMARI_NODES_PATH_ENV)
    komari_token = _optional_setting(komari_token, env=_KOMARI_TOKEN_ENV)
    komari_node_mapping = _node_mapping(komari_node_mapping)
    komari_sync_interval = _bounded_float(
        _optional_setting(komari_sync_interval_s, env=_KOMARI_SYNC_INTERVAL_ENV),
        default=60.0, lower=5.0, upper=3600.0)
    http_probe_on = _feature_on(http_probe_enabled, env=_HTTP_PROBE_ENABLE_ENV)
    http_probe_network_on = _feature_on(
        http_probe_network_enabled, env=_HTTP_PROBE_NETWORK_ENABLE_ENV)
    http_probe_sync_on = _feature_on(
        http_probe_sync_enabled, env=_HTTP_PROBE_SYNC_ENABLE_ENV)
    http_probe_sync_interval = _bounded_float(
        _optional_setting(http_probe_sync_interval_s, env=_HTTP_PROBE_SYNC_INTERVAL_ENV),
        default=60.0, lower=5.0, upper=3600.0)
    http_probe_allowed = _optional_setting(
        http_probe_allowed_origins, env=_HTTP_PROBE_ALLOWED_ORIGINS_ENV)
    if isinstance(http_probe_allowed, str):
        http_probe_allowed = [item.strip() for item in http_probe_allowed.split(",") if item.strip()]
    elif not isinstance(http_probe_allowed, (list, tuple, set)):
        http_probe_allowed = []
    http_probe_loopback = _feature_on(
        http_probe_allow_loopback, env=_HTTP_PROBE_ALLOW_LOOPBACK_ENV)
    incident_recovery = _bounded_float(
        _optional_setting(incident_recovery_required, env=_INCIDENT_RECOVERY_REQUIRED_ENV),
        default=2.0, lower=1.0, upper=10.0)
    platform_worker_on = _feature_on(
        platform_worker_enabled, env=_PLATFORM_WORKER_ENABLE_ENV)
    platform_worker_scheduler_on = _feature_on(
        platform_worker_scheduler_enabled, env=_PLATFORM_WORKER_SCHEDULER_ENABLE_ENV)
    platform_worker_interval = _bounded_float(
        _optional_setting(platform_worker_interval_s, env=_PLATFORM_WORKER_INTERVAL_ENV),
        default=1.0, lower=0.1, upper=60.0)
    platform_worker_lease = _bounded_float(
        _optional_setting(platform_worker_lease_s, env=_PLATFORM_WORKER_LEASE_ENV),
        default=60.0, lower=5.0, upper=3600.0)
    sandbox_launcher = _optional_setting(
        platform_sandbox_launcher, env="AGENT_FLEET_PLATFORM_SANDBOX_LAUNCHER")
    if sandbox_launcher is None:
        sandbox_launcher = ()
    elif isinstance(sandbox_launcher, str):
        try:
            sandbox_launcher = json.loads(sandbox_launcher)
        except ValueError:
            raise ValueError("AGENT_FLEET_PLATFORM_SANDBOX_LAUNCHER must be a JSON argv list") from None
    platform_worker_max_concurrency = _bounded_int(
        _optional_setting(platform_worker_max_concurrency, env=_PLATFORM_WORKER_MAX_CONCURRENCY_ENV),
        default=1, lower=1, upper=64)
    platform_worker_max_workspace_concurrency = _bounded_int(
        _optional_setting(platform_worker_max_workspace_concurrency,
                          env=_PLATFORM_WORKER_MAX_WORKSPACE_CONCURRENCY_ENV),
        default=1, lower=1, upper=64)
    platform_provider_network_on = _feature_on(
        platform_provider_network_enabled, env=_PLATFORM_PROVIDER_NETWORK_ENABLE_ENV)
    platform_remote_execution_on = _feature_on(
        platform_remote_execution_enabled,
        env="AGENT_FLEET_PLATFORM_REMOTE_EXECUTION_ENABLED")
    platform_browser_on = _feature_on(
        platform_browser_enabled, env=_PLATFORM_BROWSER_ENABLE_ENV)
    platform_browser_network_on = _feature_on(
        platform_browser_network_enabled, env=_PLATFORM_BROWSER_NETWORK_ENABLE_ENV)
    platform_browser_origins = _optional_setting(
        platform_browser_allowed_origins, env=_PLATFORM_BROWSER_ORIGINS_ENV)
    if isinstance(platform_browser_origins, str):
        platform_browser_origins = [item.strip() for item in platform_browser_origins.split(",") if item.strip()]
    elif not isinstance(platform_browser_origins, (list, tuple, set)):
        platform_browser_origins = []
    platform_memory_on = _feature_on(
        platform_memory_enabled, env=_PLATFORM_MEMORY_ENABLE_ENV)
    platform_memory_context_on = _feature_on(
        platform_memory_context_enabled,
        env=_PLATFORM_MEMORY_CONTEXT_ENABLE_ENV)
    # Adoption audits to the session transcript store. ``from_root`` only
    # derives that path when the session gate is on; ``create_app`` then
    # raises if adoption is on without it. Do not auto-enable session
    # (unexpected durable stores). Fail the adoption gate closed so
    # observe still boots. Formal rollout is Session → Supervisor →
    # Adoption.
    if adoption_on and not session_on:
        adoption_on = False
    config = FleetConfig.from_root(
        FLEET_HOME,
        state_dir=state.STATE_DIR,
        hosts_file=HOSTS_FILE,
        event_log=events.EVENT_LOG,
        task_db=task_store.DB_PATH,
        ingest_token=resolved_token,
        dev_operator=dev_operator,
        runner_credentials=runner_credentials,
        project_whitelist=project_whitelist,
        serve_frontend=serve_frontend,
        frontend_dir=frontend_dir,
        session_repositories_enabled=session_on,
        supervisor_enabled=supervisor_on,
        adoption_repositories_enabled=adoption_on,
        append_user_turn_enabled=append_turn_on,
        apply_local_profile_enabled=apply_profile_on,
        platform_enabled=platform_on,
        platform_db=platform_db,
        service_monitoring_enabled=service_monitoring_on,
        service_actions_enabled=service_actions_on,
        execution_windows_enabled=execution_windows_on,
        komari_enabled=komari_on,
        komari_network_enabled=komari_network_on,
        komari_base_url=komari_base_url,
        komari_nodes_path=komari_nodes_path,
        komari_token=komari_token,
        komari_node_mapping=komari_node_mapping,
        komari_sync_enabled=komari_sync_on,
        komari_sync_interval_s=komari_sync_interval,
        http_probe_enabled=http_probe_on,
        http_probe_network_enabled=http_probe_network_on,
        http_probe_sync_enabled=http_probe_sync_on,
        http_probe_sync_interval_s=http_probe_sync_interval,
        http_probe_allowed_origins=http_probe_allowed,
        http_probe_allow_loopback=http_probe_loopback,
        incident_recovery_required=int(incident_recovery),
        platform_worker_enabled=platform_worker_on,
        platform_worker_scheduler_enabled=platform_worker_scheduler_on,
        platform_worker_max_concurrency=platform_worker_max_concurrency,
        platform_worker_max_workspace_concurrency=platform_worker_max_workspace_concurrency,
        platform_worker_interval_s=platform_worker_interval,
        platform_worker_lease_s=platform_worker_lease,
        platform_sandbox_launcher=sandbox_launcher,
        platform_provider_network_enabled=platform_provider_network_on,
        platform_remote_execution_enabled=platform_remote_execution_on,
        platform_browser_enabled=platform_browser_on,
        platform_browser_network_enabled=platform_browser_network_on,
        platform_browser_allowed_origins=platform_browser_origins,
        platform_memory_enabled=platform_memory_on,
        platform_memory_context_enabled=platform_memory_context_on,
    )
    app = create_app(config)
    # Reflect the runtime gates on the app config so routes can assert them.
    app.config["SESSION_REPOSITORIES_ENABLED"] = bool(
        config.session_repositories_enabled)
    app.config["ADOPTION_REPOSITORIES_ENABLED"] = bool(
        config.adoption_repositories_enabled)
    app.config["PLATFORM_ENABLED"] = bool(config.platform_enabled)
    app.config["PLATFORM_BROWSER_ENABLED"] = bool(
        getattr(config, "platform_browser_enabled", False))
    app.config["PLATFORM_BROWSER_NETWORK_ENABLED"] = bool(
        getattr(config, "platform_browser_network_enabled", False))
    app.config["PLATFORM_MEMORY_ENABLED"] = bool(
        getattr(config, "platform_memory_enabled", False))
    app.config["PLATFORM_MEMORY_CONTEXT_ENABLED"] = bool(
        getattr(config, "platform_memory_context_enabled", False))
    app.config["SERVICE_ACTIONS_ENABLED"] = bool(
        getattr(config, "service_actions_enabled", False))
    app.config["EXECUTION_WINDOWS_ENABLED"] = bool(
        getattr(config, "execution_windows_enabled", False))
    app.config["HTTP_PROBE_ENABLED"] = bool(
        getattr(config, "http_probe_enabled", False))
    app.config["HTTP_PROBE_NETWORK_ENABLED"] = bool(
        getattr(config, "http_probe_network_enabled", False))
    app.config["HTTP_PROBE_SYNC_ENABLED"] = bool(
        getattr(config, "http_probe_sync_enabled", False))
    return app


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--reconcile-interval", type=int, default=60)
    parser.add_argument(
        "--ingest-token",
        default=None,
        help="v2 自报告端点令牌（也可用环境变量 AGENT_FLEET_INGEST_TOKEN 或 credentials/ingest-token 文件）",
    )
    parser.add_argument("--dev-operator", default=None,
                        help="开发模式 operator 兜底身份（生产勿用）")
    parser.add_argument(
        "--serve-frontend", action=argparse.BooleanOptionalAction, default=True,
        help="serve the independent frontend release from frontend-dir",
    )
    parser.add_argument(
        "--frontend-dir", default=None,
        help="frontend release directory used with --serve-frontend",
    )
    args = parser.parse_args()

    if not HAVE_FLASK:
        print("需要 flask: pip install flask")
        raise SystemExit(1)

    from hub.bootstrap import start_background_jobs

    app = make_app(
        ingest_token=args.ingest_token,
        require_token=True,
        dev_operator=args.dev_operator,
        serve_frontend=args.serve_frontend,
        frontend_dir=args.frontend_dir,
    )
    # 仅真实进程入口显式启动后台生命周期作业；make_app 本身不派生线程。
    start_background_jobs(app, reconcile_interval_s=args.reconcile_interval)
    app.run(host=args.host, port=args.port, debug=False)
