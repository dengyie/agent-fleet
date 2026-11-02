#!/usr/bin/env python3
"""Push-only local probe.

Run this on every agent machine. It executes local collectors and sends only
metadata to the HK hub; no reverse connection or central machine access is used.
"""

import argparse
import http.client
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


def _bootstrap_direct_imports():
    """Probe cron 直跑：把 repo root 挂到 sys.path[0]。

    ``tools.probe_collectors`` 依赖 repo root 上的 ``agent_profiles`` 与
    ``connectors`` 包；cron 环境无 WorkingDirectory 时常规 import 解析
    不到，root 置于 sys.path[0] 后全部可达（同 agent-runner 卡点 B）。
    """
    if __package__ not in (None, ""):
        return
    root = Path(__file__).resolve().parent.parent
    # Direct scripts put tools/ first, where platform/ shadows the stdlib.
    # Keep only the package root on sys.path, as Python -m already does.
    sys.path[:] = [entry for entry in sys.path if Path(entry).resolve() != root / 'tools']
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


_bootstrap_direct_imports()

import platform

from tools.probe_collectors import (
    DEFAULT_AGENT_TYPES,
    build_payload,
    collect_all,
    redacted_json,
)


def parse_agent_types(raw):
    if not raw or raw == "all":
        return list(DEFAULT_AGENT_TYPES)
    allowed = set(DEFAULT_AGENT_TYPES)
    selected = [item.strip() for item in raw.split(",") if item.strip()]
    unknown = sorted(set(selected) - allowed)
    if unknown:
        raise ValueError("unknown agent types: " + ", ".join(unknown))
    return selected


def send_payload(endpoint, token, payload):
    req = urllib.request.Request(
        endpoint.rstrip("/") + "/api/ingest",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Agent-Fleet-Token": token,
            "User-Agent": "agent-fleet-probe/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        return response.status, response.read().decode()


def _is_retryable_report_error(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (408, 429) or 500 <= exc.code <= 599
    if isinstance(exc, ssl.SSLError):
        return False
    network_errors = (
        TimeoutError,
        ConnectionError,
        socket.gaierror,
        http.client.BadStatusLine,
        http.client.IncompleteRead,
        http.client.LineTooLong,
        http.client.RemoteDisconnected,
    )
    if isinstance(exc, urllib.error.URLError):
        return isinstance(exc.reason, network_errors)
    return isinstance(exc, network_errors)


def send_payload_with_retry(endpoint, token, payload, attempts=3, backoff=5):
    # Use one id for every attempt so Hub can safely discard an ambiguous replay.
    payload = dict(payload)
    payload.setdefault("report_id", uuid.uuid4().hex)
    last_exc = None
    attempts = max(1, int(attempts))
    for attempt in range(1, attempts + 1):
        try:
            return send_payload(endpoint, token, payload)
        except Exception as exc:
            retryable = _is_retryable_report_error(exc)
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            if not retryable:
                raise
            last_exc = exc
            if attempt < attempts:
                print(
                    f"[self-report] attempt {attempt}/{attempts} failed: {exc}, "
                    f"retrying in {backoff}s",
                    file=sys.stderr,
                )
                time.sleep(backoff)
    raise last_exc


def resolve_token(cli_token, token_file):
    if cli_token:
        return cli_token
    env_token = os.environ.get("AGENT_FLEET_INGEST_TOKEN")
    if env_token:
        return env_token
    try:
        return Path(token_file).expanduser().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--endpoint",
        default="",
        help="hub base URL (required unless --dry-run), e.g. https://hub.example.com",
    )
    parser.add_argument("--machine", default="")
    parser.add_argument("--name", default="")
    parser.add_argument("--token", default="")
    parser.add_argument(
        "--token-file",
        default="~/.config/agent-fleet/ingest-token",
        help="local token file (default: ~/.config/agent-fleet/ingest-token)",
    )
    parser.add_argument("--agents", default="all", help="all or comma-separated agent types")
    parser.add_argument("--dry-run", action="store_true", help="print payload and do not send")
    args = parser.parse_args(argv)

    machine = args.name or args.machine or platform.node()
    if not machine:
        parser.error("machine name required")
    try:
        agent_types = parse_agent_types(args.agents)
    except ValueError as exc:
        parser.error(str(exc))
    payload = build_payload(machine, agent_types)

    if args.dry_run:
        print(redacted_json(payload))
        return 0
    if not args.endpoint:
        parser.error("--endpoint is required unless --dry-run")
    token = resolve_token(args.token, args.token_file)
    if not token:
        print("[self-report] --token is required", file=sys.stderr)
        return 2
    try:
        status, body = send_payload_with_retry(args.endpoint, token, payload)
        print(f"[self-report] {machine} -> {status} {body}")
        return 0
    except Exception as exc:
        print(f"[self-report] {machine} report failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
