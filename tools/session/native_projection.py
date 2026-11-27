"""Stable digests and bounded visible tool fields shared by native stores."""
import hashlib
import json

from fleet_core.session.redact import redact_secrets_text


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def tool_field(text):
    # Redact the complete value before splitting: a secret can cross an edge.
    text = redact_secrets_text(text)[0]
    if len(text) <= 8192:
        return text, []
    chunks = [{'kind': 'source_record', 'payload': {'text': text[i:i + 4000],
               'is_complete': i + 4000 >= len(text)}} for i in range(0, len(text), 4000)]
    marker = '\n[truncated; full tool field retained in source archive]'
    return text[:8192 - len(marker)] + marker, chunks
