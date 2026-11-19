"""Exact native ZCode desktop bindings and durable, non-replayed receipts."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlencode

from tools.supervisor.model import ManagedSessionManifest, validate_opaque
from tools.zcode_desktop import ZCodeDesktopClient, BridgeError


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix('.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


class DesktopControl:
    def __init__(self, cfg, client=None):
        self.cfg = cfg
        self.client = client or ZCodeDesktopClient(timeout=10)
        self.root = Path(cfg.supervisor_manifest_dir) / 'desktop'

    def bindings(self):
        if not self.cfg.managed_enabled:
            return {}
        rows, cursors, cursor = [], set(), None
        try:
            while True:
                page = self.client.request('/sessions' + ('?' + urlencode({'cursor':cursor}) if cursor else ''))
                rows.extend(page.get('sessions', []))
                cursor = page.get('nextCursor')
                if not cursor or cursor in cursors:
                    break
                cursors.add(cursor)
        except BridgeError:
            return {}
        return {r['session']['sessionId']: r for r in rows
                if isinstance(r, dict) and isinstance(r.get('session'), dict)
                and r['session'].get('sourceServerId') == 'local'
                and 'sendToSession' in r.get('availableActions', [])}

    def register(self, session_id, native_id, attempt_id):
        validate_opaque(native_id, 'session_id')
        group = 'desktop_' + hashlib.sha256(session_id.encode()).hexdigest()[:24]
        manifest = ManagedSessionManifest(session_id=session_id, machine_id=self.cfg.machine,
            process_group_id=group, agent='zcode', attempt_id=attempt_id,
            state='running', capability_manifest={'desktop_bridge': True})
        _write(self.root / (session_id + '.json'), {'native_id':native_id})
        _write(Path(self.cfg.supervisor_manifest_dir)/(session_id+'.json'), manifest.as_dict())
        return group

    def send(self, session_id, command_id, text):
        try:
            binding = json.loads((self.root/(session_id+'.json')).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return 'rejected', 'native_session_unbound'
        pending = self.root/'pending'/(command_id+'.json')
        # Persist before dispatch; a crash/unknown response must only query the
        # same request. The extension also deduplicates the original command ID.
        _write(pending, {'command_id':command_id})
        try:
            result = self.client.send(binding['native_id'], command_id, text)
        except (BridgeError, KeyError):
            return 'accepted', 'delivery_unknown'
        if result.get('status') == 'rejected':
            _write(pending, {'command_id':command_id, 'terminal':True})
            return 'rejected', 'desktop_rejected'
        return 'accepted', 'desktop_queued'

    def receipts(self):
        for path in (self.root/'pending').glob('*.json'):
            try:
                pending = json.loads(path.read_text(encoding='utf-8'))
                if pending.get('terminal'):
                    continue
                cid = pending['command_id']
                result = self.client.receipt(cid)
                operations = (result.get('observation') or {}).get('operations', [])
                statuses = [row.get('status') for row in operations]
                if statuses and all(s == 'completed' for s in statuses):
                    yield cid, 'succeeded', 'desktop_completed'
                elif result.get('status') == 'rejected' or any(s in {'failed','cancelled','rejected'} for s in statuses):
                    yield cid, 'failed', 'desktop_failed'
                else:
                    continue
            except (BridgeError, OSError, ValueError, KeyError):
                continue

    def acknowledge(self, cid):
        _write(self.root/'pending'/(cid+'.json'), {'command_id':cid,'terminal':True})
