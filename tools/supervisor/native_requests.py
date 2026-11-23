"""Typed answers to a current owned ACP worker; no arbitrary stdin or paths."""
from pathlib import Path

from fleet_core.control import validated_control_payload
from tools.supervisor.group_ops import _handle_pid
from tools.supervisor.handles import _AttachedHandle


class NativeRequestMixin:
    def answer_native_request(self, session_id, request_id, selected):
        self.get(session_id)
        entry = self._entries[session_id]
        with entry.control_lock:
            manifest = entry.manifest
            if (manifest.agent != 'agy' or manifest.state != 'running'
                    or not session_id.startswith('runner_')):
                return 'interaction_expired'
            try:
                clean = validated_control_payload('answer_native_request',
                    {'request_id': request_id, 'selected': selected})
            except ValueError:
                return 'invalid_payload'
            # A captured/adopted process cannot gain a provider mailbox.
            if isinstance(entry.handle, _AttachedHandle) and not hasattr(entry.handle, 'start_identity'):
                return 'unsupported_action'
            code = self._guard_adopted(session_id)
            if code:
                return code
            # A separate control-client instance restores private continuation
            # options. It may only address the supervisor-owned mailbox tree.
            if not entry.resume_options:
                self._continuations.restore(entry)
            options = entry.resume_options
            roots = [options[i + 1] for i, value in enumerate(options[:-1])
                     if value == '--control-dir']
            expected = (self._manifest_dir / 'acp' / session_id).resolve()
            if len(roots) != 1 or not Path(roots[0]).is_absolute() or Path(roots[0]).resolve() != expected:
                return 'interaction_unavailable'
            from tools.providers.acp_mailbox import AcpMailbox
            handle, code = self._live_handle(session_id, entry)
            if handle is None or code:
                return 'interaction_expired'
            owner = AcpMailbox.live_owner(expected)
            if (not owner or owner.get('session_id') != session_id
                    or owner.get('pid') != _handle_pid(handle)):
                return 'interaction_unavailable'
            return AcpMailbox.answer(expected, clean['request_id'], clean['selected'])
