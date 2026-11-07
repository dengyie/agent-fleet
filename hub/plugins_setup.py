"""Initialize the Hub command key once: python -m hub.plugins_setup."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path


def _valid(value):
    try:
        return len(base64.b64decode(value.strip(), validate=True)) == 32
    except (TypeError, ValueError):
        return False


def initialize(root: Path):
    """Preserve existing keys; never rotate credentials during setup."""
    env = os.environ.get('AGENT_FLEET_PLATFORM_COMMAND_SIGNING_KEY')
    if env:
        if not _valid(env):
            raise ValueError('invalid_command_signing_key')
        return {'state': 'configured', 'source': 'environment'}
    path = Path(root) / 'credentials' / 'platform-command-signing.key'
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        if not _valid(path.read_text(encoding='utf-8')):
            raise ValueError('invalid_command_signing_key')
        return {'state': 'configured', 'source': 'file', 'path': str(path)}
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption
    raw = Ed25519PrivateKey.generate().private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return initialize(root)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        handle.write(base64.b64encode(raw).decode('ascii') + '\n')
        handle.flush()
        os.fsync(handle.fileno())
    return {'state': 'created', 'source': 'file', 'path': str(path), 'restart_required': True}


def main():
    from hub.auth import FLEET_HOME
    try:
        result = initialize(FLEET_HOME)
    except (OSError, ValueError):
        print(json.dumps({'state': 'failed', 'error': 'command_key_setup_failed'}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
