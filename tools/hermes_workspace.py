"""Snapshot Hermes routing into a task-private home with the authorized cwd.

Some Hermes releases reapply terminal.cwd from user config after --in. A task
must not execute in that unrelated directory. The original home is never edited.
The scoped home (including auth) remains node-private and survives follow-ups.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile

import yaml


def _write(path, data):
    fd, name = tempfile.mkstemp(prefix='.snapshot-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def workspace_environment(environment, workdir):
    env = dict(environment)
    cwd = str(Path(workdir).resolve(strict=True))
    home = Path(env.get('HOME') or env.get('USERPROFILE') or Path.home())
    source = Path(env.get('HERMES_HOME') or home / '.hermes')
    key = hashlib.sha256(cwd.encode('utf-8')).hexdigest()
    target = home / '.cache/agent-fleet/hermes-workspaces' / key
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not (target / 'config.yaml').exists():
        config = source / 'config.yaml'
        if config.exists() and config.stat().st_size > 2 << 20:
            raise ValueError('hermes_config_overflow')
        value = yaml.safe_load(config.read_text(encoding='utf-8')) if config.exists() else {}
        if not isinstance(value, dict):
            raise ValueError('hermes_config_invalid')
        terminal = value.get('terminal') or {}
        if not isinstance(terminal, dict):
            raise ValueError('hermes_config_invalid')
        value['terminal'] = {**terminal, 'backend': 'local', 'env_type': 'local', 'cwd': cwd}
        for name in ('.env', 'auth.json'):
            path = source / name
            if path.exists():
                if path.stat().st_size > 2 << 20:
                    raise ValueError('hermes_credentials_overflow')
                _write(target / name, path.read_bytes())
        _write(target / 'config.yaml', yaml.safe_dump(value, allow_unicode=True).encode('utf-8'))
    env.update(HERMES_HOME=str(target), TERMINAL_CWD=cwd)
    return env
