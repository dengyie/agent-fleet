#!/usr/bin/env python3
"""hub/web.py - agent-fleet Web 总览服务 and compatibility entrypoint.

The application assembly lives in :mod:`hub.bootstrap`; this module keeps the
legacy CLI, helper functions, and ``make_app`` signature stable for callers.
"""
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


def make_app(ingest_token=None, require_token=True, dev_operator=None,
             runner_credentials=None, project_whitelist=None,
             frontend_cutover=False, frontend_dir=None,
             session_repositories_enabled=None,
             supervisor_enabled=None,
             adoption_repositories_enabled=None,
             append_user_turn_enabled=None,
             apply_local_profile_enabled=None):
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
        frontend_cutover=frontend_cutover,
        frontend_dir=frontend_dir,
        session_repositories_enabled=session_on,
        supervisor_enabled=supervisor_on,
        adoption_repositories_enabled=adoption_on,
        append_user_turn_enabled=append_turn_on,
        apply_local_profile_enabled=apply_profile_on,
    )
    app = create_app(config)
    # Reflect the runtime gates on the app config so routes can assert them.
    app.config["SESSION_REPOSITORIES_ENABLED"] = bool(
        config.session_repositories_enabled)
    app.config["ADOPTION_REPOSITORIES_ENABLED"] = bool(
        config.adoption_repositories_enabled)
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
        "--frontend-cutover", action="store_true",
        help="serve the independent frontend release from frontend-dir",
    )
    parser.add_argument(
        "--frontend-dir", default=None,
        help="frontend release directory used with --frontend-cutover",
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
        frontend_cutover=args.frontend_cutover,
        frontend_dir=args.frontend_dir,
    )
    # 仅真实进程入口显式启动后台生命周期作业；make_app 本身不派生线程。
    start_background_jobs(app, reconcile_interval_s=args.reconcile_interval)
    app.run(host=args.host, port=args.port, debug=False)
