"""Run a foreground Muse-only Fleet Node with explicit local opt-in."""
from __future__ import annotations

import argparse
import base64
import json
import math
from pathlib import Path
import time
from urllib.parse import urlsplit

from tools.platform.node_runtime import NodeRuntime, NodeRuntimeConfig
from .contract import WEB_AGENT_TOOLS


def load_runtime(path):
    data = json.loads(Path(path).expanduser().read_text(encoding='utf-8'))
    required = {'node_id', 'hub_url', 'credential_file', 'public_key_file',
                'state_directory', 'workspace_ids', 'allow_muse_tasks'}
    if not isinstance(data, dict) or set(data) - (required | {'poll_interval_s'}) or not required <= set(data):
        raise ValueError('invalid_node_config')
    if data['allow_muse_tasks'] is not True:
        raise ValueError('local_muse_opt_in_required')
    url = urlsplit(data['hub_url'])
    if (url.scheme != 'https' or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.path not in {'', '/'}):
        raise ValueError('hub_https_origin_required')
    workspaces = data['workspace_ids']
    if not isinstance(workspaces, list) or not workspaces or any(not isinstance(w, str) for w in workspaces):
        raise ValueError('workspace_allowlist_required')
    interval = data.get('poll_interval_s', 2)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval) or not 1 <= interval <= 60:
        raise ValueError('poll_interval_invalid')
    state = Path(data['state_directory']).expanduser()
    if not state.is_absolute():
        raise ValueError('absolute_state_path_required')
    credential = Path(data['credential_file']).expanduser().read_text(encoding='utf-8').strip()
    public = base64.b64decode(Path(data['public_key_file']).expanduser().read_text(encoding='utf-8').strip(), validate=True)
    config = NodeRuntimeConfig(node_id=data['node_id'], credential=credential,
        hub_url=data['hub_url'], public_key=public, capabilities=WEB_AGENT_TOOLS,
        workspace_root=state / 'unused-workspace', journal_path=state / 'commands.db',
        muse_bridge_enabled=True, muse_workspace_ids=tuple(workspaces),
        muse_bridge_state_path=state / 'muse.db')
    # Command receipts contain task replies; protect both journals before the
    # generic runtime creates its workspace or SQLite files.
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if state.stat().st_mode & 0o077:
        raise ValueError('private_state_directory_required')
    return NodeRuntime(config), interval


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--manifest', action='store_true')
    args = parser.parse_args(argv)
    try:
        runtime, interval = load_runtime(args.config)
    except (OSError, ValueError, TypeError, KeyError):
        print(json.dumps({'ok':False, 'error':'invalid_node_config'}))
        return 2
    if args.manifest:
        print(json.dumps(runtime.manifest()))
        return 0
    try:
        last_heartbeat = 0.0
        while True:
            if time.monotonic() - last_heartbeat >= 30:
                runtime.client.heartbeat()
                last_heartbeat = time.monotonic()
            result = runtime.poll_once(limit=1)
            print(json.dumps(result), flush=True)  # counts only; never transcript or credentials
            if args.once:
                return 0 if result.get('ok') else 1
            time.sleep(interval)
    except KeyboardInterrupt:
        return 130
    finally:
        runtime.close_all()


if __name__ == '__main__':
    raise SystemExit(main())
