"""Session capture source adapters (Task 7).

Each adapter turns a bounded verified source (native JSONL transcript, a
CLI structured stream, hooks, or PTY) into validated, redactable session
events.  Adapters never consult CLI names/versions to infer a capability: the
caller (``tools/session/bridge.py``) passes a capability manifest as the sole
source of truth for which source is allowed.

The package never imports Flask, never opens inbound connections, never
constructs arbitrary remote commands, and never forwards hook/process
environments.  All diagnostics are short stable codes.
"""

from . import hooks, native_jsonl, pty, structured_stream  # noqa: F401

__all__ = ["hooks", "native_jsonl", "pty", "structured_stream"]