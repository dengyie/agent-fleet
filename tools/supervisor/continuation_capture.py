"""Capture signed runner follow-ups in the original session stream."""
from __future__ import annotations

import json
import logging
import threading

from tools.session.runner_capture import RunnerCapture
from tools.session.sync import post


class ContinuationCapture:
    def __init__(self, cfg, supervisor):
        self.cfg = cfg
        self.supervisor = supervisor
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.RLock()

    @property
    def active(self):
        with self._lock:
            return any(thread.is_alive() for thread in self._threads.values())

    def send(self, session_id, text):
        with self._lock:
            self._threads = {sid: t for sid, t in self._threads.items() if t.is_alive()}
            if session_id in self._threads:
                return 'session_busy'
            manifest = self.supervisor.get(session_id)
            if manifest.state != 'terminated' or manifest.reason != 'completed':
                return 'session_busy' if manifest.state == 'running' else 'unsupported_action'
            if not self.supervisor.continuation_ready(session_id):
                return 'native_session_unbound'
            record = self.cfg.cache_dir / 'conversations' / (session_id + '.json')
            try:
                task = json.loads(record.read_text(encoding='utf-8'))
                if task.get('pruned') or not task.get('resumable'):
                    return 'capture_unavailable'
                capture = RunnerCapture(self.cfg, task, post,
                    process_group_id=manifest.process_group_id, replay=True)
            except (OSError, ValueError, RuntimeError):
                return 'capture_unavailable'
            try:
                outcome = self.supervisor.append_user_turn(session_id, text)
                if outcome != 'appended':
                    capture.bridge.close()
                    return outcome
                # Start capture before returning the delivery receipt; all
                # subsequent turns share the same durable sequence checkpoint.
                capture.begin_turn(text)
                capture.start_upload()
                thread = threading.Thread(target=self._finish, args=(session_id, capture),
                    name='native-continuation-' + session_id[-12:], daemon=True)
                self._threads[session_id] = thread
                thread.start()
                return outcome
            except Exception:
                capture.stop_upload()
                capture.bridge.close()
                # A failed capture setup after spawn must not orphan a writer.
                self.supervisor.terminate_session(session_id, grace_s=1)
                raise

    def _finish(self, session_id, capture):
        try:
            result = self.supervisor.wait_session(session_id, timeout_s=float('inf'), on_line=capture.line)
            capture.finish(result.exit_code, resumable=self.supervisor.continuation_ready(session_id))
        except Exception:
            logging.getLogger(__name__).exception('Continuation failed: %s', session_id)
            self.supervisor.terminate_session(session_id, grace_s=1)
            capture.finish(-1, resumable=False)
        finally:
            capture.stop_upload()
            capture.bridge.close()
