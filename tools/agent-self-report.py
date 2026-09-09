#!/usr/bin/env python3
"""Push-only local probe.

Run this on every agent machine. It executes local collectors and sends only
metadata to the HK hub; no reverse connection or central machine access is used.
"""

import argparse
import json
import os
import platform
import sys
import urllib.request
from pathlib import Path


def _bootstrap_direct_imports():
    """Expose repository namespace packages for direct script execution."""
    if __package__ not in (None, ""):
        return
    import importlib.util
    import types

    root = Path(__file__).resolve().parent.parent
    # ``agent_profiles`` 必须先于 report_schema 注册：report_schema 顶层
    # ``from agent_profiles import ...``（注册表派生），先加载 schema 会因
    # ambient 路径解析不到而 ModuleNotFoundError。
    if "agent_profiles" not in sys.modules:
        profiles_path = root / "agent_profiles.py"
        spec = importlib.util.spec_from_file_location(
            "agent_profiles", profiles_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["agent_profiles"] = module
        spec.loader.exec_module(module)
    if "report_schema" not in sys.modules:
        schema_path = root / "report_schema.py"
        spec = importlib.util.spec_from_file_location("report_schema", schema_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["report_schema"] = module
        spec.loader.exec_module(module)
    # ``tools.probe_collectors`` imports the repository-root
    # ``agent_profiles`` module by name. Without registration a direct run
    # (probe cron) resolves it against the ambient interpreter paths and
    # fails with ModuleNotFoundError — same class of bug as the
    # release-root ``tools`` anchor (commit a737839).
    tools_package = types.ModuleType("tools")
    tools_package.__path__ = [str(root / "tools")]
    sys.modules.setdefault("tools", tools_package)
    if "connectors" not in sys.modules:
        path = root / "connectors"
        spec = importlib.util.spec_from_file_location(
            "connectors", path / "__init__.py",
            submodule_search_locations=[str(path)],
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["connectors"] = module
        spec.loader.exec_module(module)


_bootstrap_direct_imports()

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


def resolve_token(cli_token, token_file):
    if cli_token:
        return cli_token
    env_token = os.environ.get("AGENT_FLEET_INGEST_TOKEN")
    if env_token:
        return env_token
    try:
        return Path(token_file).expanduser().read_text().strip()
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
        status, body = send_payload(args.endpoint, token, payload)
        print(f"[self-report] {machine} -> {status} {body}")
        return 0
    except Exception as exc:
        print(f"[self-report] {machine} report failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
