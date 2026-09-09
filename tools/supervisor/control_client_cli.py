#!/usr/bin/env python3
"""Machine-side ControlClient entrypoint (LaunchAgent / cron).

``tools.supervisor.control_client.ControlClient`` is a library — it has no
CLI. This wrapper is the process that:

- pulls ``POST /api/supervisor/poll`` with ``X-Supervisor-Credential``;
- validates + dispatches signed control (adopt / detach / pause / …);
- on successful adopt, opens one ``SessionBridge`` and tails the native
  transcript into the encrypted spool, then POSTs a JSON *list* to
  ``/api/session-events`` (Observe ingest domain).

The process must stay resident: live bridges live on the ControlClient
instance. A StartInterval cron that exits after one poll would drop them.

Usage:
  python3 tools/supervisor/control_client_cli.py --config ~/.config/agent-fleet/runner.yaml
  python3 tools/supervisor/control_client_cli.py --config ... --once
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def _bootstrap_direct_imports():
    """Repo-root sys.path + tools namespace for direct script execution."""
    if __package__ not in (None, ""):
        return
    import importlib.util
    import types

    root = Path(__file__).resolve().parent.parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    if "agent_profiles" not in sys.modules:
        profiles_path = root / "agent_profiles.py"
        spec = importlib.util.spec_from_file_location(
            "agent_profiles", profiles_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["agent_profiles"] = module
        spec.loader.exec_module(module)
    tools_package = types.ModuleType("tools")
    tools_package.__path__ = [str(root / "tools")]
    sys.modules.setdefault("tools", tools_package)


_bootstrap_direct_imports()

from tools import runner_config  # noqa: E402


DEFAULT_INTERVAL_S = 15
MIN_BACKOFF_S = 5
MAX_BACKOFF_S = 60
DEFAULT_INGEST_TOKEN = Path.home() / ".config" / "agent-fleet" / "ingest-token"
DEFAULT_SPOOL_KEY = Path.home() / ".config" / "agent-fleet" / "session-spool.key"
DEFAULT_SPOOL_ROOT = Path.home() / ".cache" / "agent-fleet" / "session-spool"
USER_AGENT = "agent-fleet-control-client/1.0"


class CliError(Exception):
    """Fail-closed configuration error; message is a bounded token, no secrets."""


def _load_or_create_spool_key(path: Path) -> bytes:
    path = Path(path).expanduser()
    if path.exists():
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise CliError("无法读取 session spool key") from exc
        if len(data) != 32:
            raise CliError("session spool key 必须是 32 字节")
        return data
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        key = os.urandom(32)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        raise CliError("无法写入 session spool key") from exc
    return key


def _read_token_file(path: Path, what: str) -> str:
    try:
        token = Path(path).expanduser().read_text().strip()
    except OSError as exc:
        raise CliError(f"无法读取 {what}") from exc
    if not token:
        raise CliError(f"{what} 为空")
    return token


def _make_session_events_post(hub_url: str, ingest_token: str):
    """Uploader transport: unwrap ``{\"events\": [...]}`` and POST a JSON list."""

    def post_json(payload):
        events = payload.get("events") if isinstance(payload, dict) else payload
        if not isinstance(events, list):
            raise TypeError("events list required")
        req = urllib.request.Request(
            hub_url.rstrip("/") + "/api/session-events",
            data=json.dumps(events).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Agent-Fleet-Token": ingest_token,
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode() or "{}")
            except Exception:
                body = {}
            if isinstance(body, dict):
                return body
            return {}
        except Exception:
            return {}

    return post_json


def _make_bridge_factory():
    """Build a SessionBridge from ControlClient's bounded adopt config."""

    def factory(cfg: dict):
        from tools.session.bridge import SessionBridge

        family = str(cfg.get("agent_family") or "claude")
        native_path = cfg.get("native_file_path") or None
        manifest = {
            "agent": family,
            "version": "0",
            "installed": True,
            "spawn": False,
            "resume": False,
            "native_transcript": bool(native_path),
            "structured_stream": False,
            "hooks": False,
            "pty": False,
            "supported_event_kinds": [],
            "quality_by_kind": {},
            "diagnostics": [],
        }
        bridge = SessionBridge.open(manifest, cfg)
        # ``native_file_path`` rides in the adopt config; the bridge stores it
        # at construction, so later pump ticks can call ingest_native() with no
        # path.
        return bridge

    return factory


def _pump_bridges(client) -> None:
    """Best-effort native tail + flush for every live adopted bridge."""
    for bridge in client.iter_bridges():
        try:
            ingest = getattr(bridge, "ingest_native", None)
            if callable(ingest):
                ingest()
        except Exception:
            pass
        try:
            flush = getattr(bridge, "flush", None)
            if callable(flush):
                flush()
        except Exception:
            pass


def _build_client(cfg, *, ingest_token: str, spool_key: bytes,
                  spool_root: Path):
    from tools.supervisor.control_client import ControlClient, NonceStore
    from tools.supervisor.supervisor import Supervisor

    if not cfg.supervisor_enabled or cfg.supervisor_manifest_dir is None:
        raise CliError("supervisor 未启用")
    if not cfg.supervisor_credential:
        raise CliError("缺少 supervisor credential")
    if not cfg.supervisor_public_key:
        raise CliError("缺少 supervisor public_key")

    supervisor = Supervisor(
        manifest_dir=cfg.supervisor_manifest_dir,
        machine_id=cfg.machine,
    )
    try:
        supervisor.recover()
    except Exception:
        pass

    spool_root = Path(spool_root).expanduser()
    spool_root.mkdir(parents=True, exist_ok=True)

    return ControlClient(
        hub_url=cfg.hub,
        machine_id=cfg.machine,
        credential=cfg.supervisor_credential,
        hub_public_key=cfg.supervisor_public_key,
        public_supervisor=supervisor,
        nonce_store=NonceStore(),
        bridge_factory=_make_bridge_factory(),
        spool_root=spool_root,
        spool_key=spool_key,
        post_json=_make_session_events_post(cfg.hub, ingest_token),
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="agent-fleet ControlClient（pull-only 控制面 + session 上报）")
    parser.add_argument("--config", default=str(runner_config.DEFAULT_CONFIG_PATH))
    parser.add_argument("--once", action="store_true", help="单轮 poll（cron / 拨测）")
    parser.add_argument("--interval", type=int, default=None,
                        help="常驻轮询间隔秒（默认 15）")
    parser.add_argument("--ingest-token-file",
                        default=str(DEFAULT_INGEST_TOKEN))
    parser.add_argument("--spool-key-file", default=str(DEFAULT_SPOOL_KEY))
    parser.add_argument("--spool-root", default=str(DEFAULT_SPOOL_ROOT))
    args = parser.parse_args(argv)

    try:
        cfg = runner_config.load_config(args.config)
        if not cfg.supervisor_enabled or cfg.supervisor_manifest_dir is None:
            raise CliError("supervisor 未启用")
        if not cfg.supervisor_credential:
            raise CliError("缺少 supervisor credential")
        if not cfg.supervisor_public_key:
            raise CliError("缺少 supervisor public_key")
        ingest_token = _read_token_file(
            Path(args.ingest_token_file), "ingest-token")
        spool_key = _load_or_create_spool_key(Path(args.spool_key_file))
        client = _build_client(
            cfg, ingest_token=ingest_token, spool_key=spool_key,
            spool_root=Path(args.spool_root))
    except (CliError, runner_config.ConfigError) as extra:
        print(f"[control-client] {extra}", file=sys.stderr)
        return 2

    interval = args.interval or DEFAULT_INTERVAL_S

    def _tick():
        result = client.poll_once()
        _pump_bridges(client)
        return result

    if args.once:
        result = _tick()
        ok = bool(result.get("ok"))
        n = int(result.get("commands") or 0)
        err = result.get("error") or ""
        if ok:
            print(f"[control-client] poll ok commands={n}")
        else:
            print(f"[control-client] poll failed {str(err)[:80]}",
                  file=sys.stderr)
        return 0 if ok else 1

    backoff = MIN_BACKOFF_S
    while True:
        try:
            result = _tick()
            if result.get("ok"):
                backoff = MIN_BACKOFF_S
            else:
                print(f"[control-client] poll failed {str(result.get('error') or '')[:80]}",
                      file=sys.stderr)
                backoff = min(max(backoff, MIN_BACKOFF_S) * 2, MAX_BACKOFF_S)
            time.sleep(interval if result.get("ok") else backoff)
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            print(f"[control-client] {type(exc).__name__}", file=sys.stderr)
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_S)


if __name__ == "__main__":
    raise SystemExit(main())
