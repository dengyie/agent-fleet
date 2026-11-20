"""Node-private, durable native continuation identity (never a public manifest).

A stream-proven native id and its original working directory must survive the
runner process. The control client can then resume an idle session without
inventing a new conversation or inheriting Hub credentials.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from tools.supervisor.model import validate_opaque


class ContinuationStore:
    def __init__(self, manifest_dir: Path):
        self.root = Path(manifest_dir) / 'continuations'

    def path(self, session_id: str) -> Path:
        return self.root / (validate_opaque(session_id, 'session_id') + '.json')

    def save(self, entry) -> bool:
        m = entry.manifest
        if not entry.resume_token or not entry.cwd or not entry.exe_path:
            return False
        value = {'session_id': m.session_id, 'machine_id': m.machine_id,
                 'agent': m.agent, 'cwd': entry.cwd, 'exe_path': entry.exe_path,
                 'resume_token': entry.resume_token, 'env': entry.env or {},
                 'resume_options': entry.resume_options,
                 'model_config': entry.model_config,
                 'restore_model': entry.restore_model,
                 'native_file_path': entry.native_file_path}
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix='.continuation-', dir=self.root)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(value, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path(m.session_id))
        finally:
            if os.path.exists(name):
                os.unlink(name)
        return True

    def restore(self, entry) -> bool:
        m = entry.manifest
        try:
            data = json.loads(self.path(m.session_id).read_text(encoding='utf-8'))
            if (data['session_id'] != m.session_id or data['machine_id'] != m.machine_id
                    or data['agent'] != m.agent or not isinstance(data['env'], dict)
                    or not all(isinstance(k, str) and isinstance(v, str) for k, v in data['env'].items())
                    or not all(isinstance(data[k], str) and data[k] for k in ('cwd', 'exe_path', 'resume_token'))
                    or not isinstance(data.get('resume_options', []), list)
                    or not all(isinstance(v, str) for v in data.get('resume_options', []))
                    or not Path(data['cwd']).is_dir()):
                return False
            model_config = data.get('model_config')
            restore_model = data.get('restore_model')
            if restore_model is not None and (not isinstance(restore_model, str) or not restore_model
                    or len(restore_model) > 256 or any(ord(c) < 32 for c in restore_model)):
                return False
            if model_config is not None:
                from fleet_core.session_models import validate_model_delivery
                model_config = validate_model_delivery(model_config, m.agent)
            for key in ('cwd', 'exe_path', 'resume_token', 'env', 'native_file_path'):
                setattr(entry, key, data.get(key))
            entry.resume_options = data.get('resume_options', [])
            entry.model_config = model_config
            entry.restore_model = restore_model
            return True
        except (OSError, ValueError, TypeError, KeyError):
            return False
