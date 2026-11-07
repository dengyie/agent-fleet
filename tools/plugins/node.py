"""Run official plugins as the intended VM OS user: python -m tools.plugins.node."""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
from pathlib import Path
import signal
import threading
import time
from urllib.parse import urlsplit

from fleet_core.locking import LockBusy, OwnedLock
from fleet_core.plugins import OFFICIAL_PLUGINS
from tools.platform.node_runtime import NodeRuntime, NodeRuntimeConfig


class PluginNodeRuntime(NodeRuntime):
    def __init__(self, config, lock, **kwargs):
        self.ownership_lock = lock
        super().__init__(config, **kwargs)

    def close_all(self):
        try:
            super().close_all()
        finally:
            self.ownership_lock.release()


def valid_origin(value):
    url = urlsplit(value)
    if (not url.hostname or (url.scheme != 'https' and not (
            url.scheme == 'http' and url.hostname in {'localhost', '127.0.0.1', '::1'})) or url.username or url.password
            or url.query or url.fragment or url.path not in {'', '/'}):
        raise ValueError('hub_https_origin_required')
    return value.rstrip('/')


def install_enrollment(path, state_directory):
    raw = Path(path).expanduser().read_bytes()
    if len(raw) > 65536:
        raise ValueError('invalid_enrollment')
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) != {'node_id', 'hub_url', 'credential', 'public_key', 'plugins'}:
        raise ValueError('invalid_enrollment')
    state = Path(state_directory).expanduser()
    if not state.is_absolute() or not isinstance(data['plugins'], dict) or not data['plugins']:
        raise ValueError('invalid_enrollment')
    for plugin_id in data['plugins']:
        OFFICIAL_PLUGINS.get(plugin_id)
    public = base64.b64decode(data['public_key'], validate=True)
    NodeRuntimeConfig(node_id=data['node_id'], credential=data['credential'], public_key=public,
                      hub_url=valid_origin(data['hub_url']), workspace_root=state / 'unused-workspace',
                      journal_path=state / 'commands.db')
    paths = {name: state / name for name in ('node-credential', 'command-public-key', 'node.json')}
    if any(path.exists() for path in paths.values()):
        raise ValueError('enrollment_files_exist')
    config = {'node_id': data['node_id'], 'hub_url': valid_origin(data['hub_url']),
              'credential_file': str(paths['node-credential']), 'public_key_file': str(paths['command-public-key']),
              'state_directory': str(state), 'plugins': data['plugins']}
    values = {'node-credential': data['credential'], 'command-public-key': data['public_key'],
              'node.json': json.dumps(config, ensure_ascii=False, indent=2)}
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, value in values.items():
        fd = os.open(str(paths[name]), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(value + '\n')
    return paths['node.json']


def load_runtime(path, *, transport=None):
    raw = Path(path).expanduser().read_bytes()
    if len(raw) > 65536:
        raise ValueError('invalid_node_config')
    data = json.loads(raw)
    required = {'node_id', 'hub_url', 'credential_file', 'public_key_file', 'state_directory', 'plugins'}
    if not isinstance(data, dict) or set(data) - (required | {'poll_interval_s'}) or not required <= set(data):
        raise ValueError('invalid_node_config')
    valid_origin(data['hub_url'])
    interval = data.get('poll_interval_s', 2)
    if type(interval) not in (int, float) or not math.isfinite(interval) or not 1 <= interval <= 60:
        raise ValueError('poll_interval_invalid')
    state = Path(data['state_directory']).expanduser()
    if not state.is_absolute() or not isinstance(data['plugins'], dict) or not data['plugins']:
        raise ValueError('invalid_node_config')
    credential = Path(data['credential_file']).expanduser().read_text(encoding='utf-8').strip()
    public = base64.b64decode(Path(data['public_key_file']).expanduser().read_text(encoding='utf-8').strip(), validate=True)
    config = NodeRuntimeConfig(
        node_id=data['node_id'], credential=credential, hub_url=data['hub_url'], public_key=public,
        workspace_root=state / 'unused-workspace', journal_path=state / 'commands.db', plugins=data['plugins'])
    lock = OwnedLock(state / 'node.lock', purpose='official-plugin-node').acquire()
    try:
        return PluginNodeRuntime(config, lock, transport=transport), interval
    except Exception:
        lock.release()
        raise


def announce(runtime):
    try:
        status, _ = runtime.client._post('/api/platform/v1/nodes/plugins',
                                         {'plugins': runtime.manifest().get('plugins', [])})
        return status == 200
    except Exception:
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--config')
    mode.add_argument('--enroll')
    parser.add_argument('--state-directory')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--manifest', action='store_true')
    args = parser.parse_args(argv)
    if args.enroll:
        if not args.state_directory:
            parser.error('--enroll requires --state-directory')
        try:
            config_path = install_enrollment(args.enroll, args.state_directory)
        except (OSError, ValueError, TypeError, KeyError):
            print(json.dumps({'ok': False, 'error': 'invalid_enrollment_or_existing_files'}))
            return 2
        print(json.dumps({'ok': True, 'config_file': str(config_path)}))
        return 0
    try:
        runtime, interval = load_runtime(args.config)
    except LockBusy:
        print(json.dumps({'ok': False, 'error': 'plugin_node_already_running'}))
        return 75
    except (OSError, ValueError, TypeError, KeyError):
        print(json.dumps({'ok': False, 'error': 'invalid_node_config'}))
        return 2
    stopped = threading.Event()
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, lambda *_: stopped.set())
    try:
        if args.manifest:
            print(json.dumps(runtime.manifest(), ensure_ascii=False))
            return 0
        last_report = -float('inf')
        while not stopped.is_set():
            if time.monotonic() - last_report >= 30:
                if announce(runtime):
                    runtime.client.heartbeat()
                    last_report = time.monotonic()
            result = runtime.poll_once(limit=1)
            print(json.dumps(result), flush=True)
            if args.once:
                return 0 if result.get('ok') else 1
            stopped.wait(interval)
        return 0
    finally:
        runtime.close_all()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == '__main__':
    raise SystemExit(main())
