"""Immutable model credentials; persisted Run snapshots keep their original binding."""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

from hub.platform.providers.openai_compatible import EnvironmentSecretBroker


class ModelSecrets:
    def __init__(self, root: Path):
        self.root = Path(root)

    def store(self, value: str) -> str:
        if not isinstance(value, str) or not value.strip() or len(value.encode('utf-8')) > 8192 or any(c in value for c in '\r\n\x00'):
            raise ValueError('invalid_api_key')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        identifier = uuid.uuid4().hex
        descriptor = os.open(self.root / identifier, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        # Persist the directory entry before a database snapshot may reference it.
        if hasattr(os, 'O_DIRECTORY'):
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return 'managed://' + identifier

    def resolve(self, reference: str) -> str | None:
        if isinstance(reference, str) and re.fullmatch(r'managed://[a-f0-9]{32}', reference):
            try:
                return (self.root / reference[10:]).read_text(encoding='utf-8')
            except OSError:
                return None
        return EnvironmentSecretBroker().resolve(reference)
