"""Capture signed runner follow-ups in the original session stream."""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from tools.session.runner_capture import RunnerCapture
from tools.session.sync import post


class ContinuationCapture:
    def __init__(self, cfg, supervisor):
        self.cfg = cfg
        self.supervisor = supervisor
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.RLock()
        from tools.supervisor.continuation_result_upload import ContinuationResultUpload
        self.results = ContinuationResultUpload(cfg, post)
        self._replay_worker = None
        self._active_commands = set()

    @property
    def active(self):
        with self._lock:
            return any(thread.is_alive() for thread in self._threads.values())

    def replay_results(self):
        # Called from the existing control-client poll loop. Network retries
        # run independently so a pending upload cannot delay control admission.
        if self._replay_worker is None or not self._replay_worker.is_alive():
            self._replay_worker = threading.Thread(target=self._replay_results,
                name='continuation-result-upload', daemon=True)
            self._replay_worker.start()

    def _replay_results(self):
        try:
            self._recover_results()
            self.results.replay()
        except Exception:
            logging.getLogger(__name__).warning('Continuation result recovery pending')

    def _draft(self, command_id, session_id, task, baseline, manifest, *, phase):
        return {'version': 1, 'command_id': command_id, 'session_id': session_id,
            'task_id': task['task_id'], 'attempt_id': task['attempt_id'], 'project': task.get('project'),
            'process_group_id': manifest.process_group_id, 'phase': phase, 'started_at': time.time(),
            'before_tree': baseline[2] if isinstance(baseline, tuple) else None,
            'collection_reason': baseline.get('reason', 'result_unavailable') if isinstance(baseline, dict)
                else '' if baseline is not None else 'result_unavailable'}

    def _recover_results(self, *, session_id=None):
        """Resolve saved drafts without launching, waiting for or signalling Agents."""
        for draft in self.results.drafts(session_id=session_id):
            with self._lock:
                cid = draft.get('command_id')
                if cid in self._active_commands:
                    continue
                if self.results.retained(cid):
                    self.results.finish_draft(cid)
                    continue
                identifiers = ('command_id', 'session_id', 'task_id', 'attempt_id', 'process_group_id')
                if (draft.get('version') != 1 or any(not isinstance(draft.get(key), str)
                        or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', draft[key]) is None for key in identifiers)
                        or draft.get('phase') not in {'prepared', 'dispatched'}
                        or type(draft.get('started_at')) not in (int, float)
                        or not 0 <= draft['started_at'] <= 2**53
                        or not math.isfinite(draft['started_at'])):
                    logging.getLogger(__name__).warning('Continuation result draft invalid')
                    self.results.reject_draft(cid)
                    continue
                baseline = {'reason': 'result_unavailable'}
                task = {'task_id': draft['task_id'], 'attempt_id': draft['attempt_id'], 'project': draft.get('project')}
                try:
                    manifest = self.supervisor.get(draft['session_id'])
                    # "prepared" may have crashed before spawn; an unchanged
                    # startup terminal manifest cannot prove turn completion.
                    if (draft['phase'] == 'dispatched' and manifest.state == 'terminated'
                            and manifest.attempt_id == draft['attempt_id']
                            and manifest.process_group_id == draft['process_group_id']):
                        project = self._bound_project(draft['session_id'], task)
                        tree = draft.get('before_tree')
                        if project is not None and isinstance(tree, str) and re.fullmatch(r'[0-9a-f]{40}(?:[0-9a-f]{24})?', tree):
                            baseline = (Path(project), draft['task_id'], tree)
                        else:
                            baseline = {'reason': draft.get('collection_reason') or 'result_unavailable'}
                except Exception:
                    # Unknown incarnation/liveness has an explicit unknown
                    # result and no workspace collection; never resume again.
                    baseline = None
                # A terminal manifest does not retain the exit status. Do not
                # turn "completed" into a fabricated successful run. A failed
                # durable write leaves the draft intact for the next pass.
                self._store_result(draft['session_id'], task, cid, baseline,
                    time.monotonic() - min(365 * 86400, max(0, time.time() - draft['started_at'])), None,
                    request_replay=False)
                self.results.finish_draft(cid)

    def send(self, session_id, text, command_id=None):
        with self._lock:
            # Finalize orphaned earlier results before a later admitted turn
            # can change the same workspace and invalidate its baseline.
            self._recover_results(session_id=session_id)
            self._threads = {sid: t for sid, t in self._threads.items() if t.is_alive()}
            if session_id in self._threads:
                return 'session_busy'
            manifest = self.supervisor.get(session_id)
            if manifest.state != 'terminated' or manifest.reason != 'completed':
                return 'session_busy' if manifest.state == 'running' else 'unsupported_action'
            if not self.supervisor.continuation_ready(session_id):
                return 'native_session_unbound'
            record = self.cfg.cache_dir / 'conversations' / (session_id + '.json')
            spawned = False
            try:
                task = json.loads(record.read_text(encoding='utf-8'))
                if task.get('pruned') or not task.get('resumable'):
                    return 'capture_unavailable'
                capture = RunnerCapture(self.cfg, task, post,
                    process_group_id=manifest.process_group_id, replay=True)
            except (OSError, ValueError, RuntimeError):
                return 'capture_unavailable'
            try:
                baseline = self._baseline(session_id, task) if command_id else None
                draft = None
                if command_id:
                    self._active_commands.add(command_id)
                    draft = self._draft(command_id, session_id, task, baseline, manifest, phase='prepared')
                    self.results.save_draft(draft)
                outcome = self.supervisor.append_user_turn(session_id, text)
                if outcome != 'appended':
                    if command_id:
                        self.results.finish_draft(command_id)
                        self._active_commands.discard(command_id)
                    capture.bridge.close()
                    return outcome
                spawned = True
                if draft is not None:
                    draft['phase'] = 'dispatched'
                    self.results.save_draft(draft)
                # Start capture before returning the delivery receipt; all
                # subsequent turns share the same durable sequence checkpoint.
                capture.begin_turn(text)
                capture.start_upload()
                thread = threading.Thread(target=self._finish,
                    args=(session_id, capture, command_id, baseline, time.monotonic(), task),
                    name='native-continuation-' + session_id[-12:], daemon=True)
                self._threads[session_id] = thread
                thread.start()
                return outcome
            except Exception:
                self._active_commands.discard(command_id)
                capture.stop_upload()
                capture.bridge.close()
                # A failed capture setup after spawn must not orphan a writer.
                if spawned:
                    self.supervisor.terminate_session(session_id, grace_s=1)
                raise

    def _bound_project(self, session_id, task):
        project = self.cfg.projects.get(task.get('project'))
        if project is None:
            return None
        expected = Path(project).parent / (Path(project).name + '.fleet-worktrees') / task['task_id']
        entry = self.supervisor._entries.get(session_id)
        if not entry or not entry.cwd or Path(entry.cwd).resolve() != expected.resolve():
            return None
        return Path(project)

    def _baseline(self, session_id, task):
        """Require exact configured project + original retained native cwd."""
        from tools.continuation_results import snapshot_tree, ContinuationResultUnavailable
        project = self._bound_project(session_id, task)
        if project is None:
            return None
        try:
            return project, task['task_id'], snapshot_tree(project, task['task_id'])
        except ContinuationResultUnavailable as exc:
            return {'reason': exc.code}
        except Exception:
            return None

    def _store_result(self, session_id, task, command_id, baseline, started, result, *, request_replay=True):
        if not command_id:
            return
        from tools.continuation_results import compare_trees, snapshot_tree
        exit_code = result.exit_code if result is not None else -1
        value = {'command_id': command_id, 'session_id': session_id,
            'task_id': task['task_id'], 'execution_state':
                'unknown' if result is None else 'cancelled' if result.aborted else
                'succeeded' if exit_code == 0 else 'failed',
            'collection_state': 'unavailable', 'collection_reason': 'result_unavailable',
            'exit_code': exit_code, 'duration_s': min(365 * 86400, max(0, time.monotonic() - started)),
            'finished_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'diff_stat': '', 'diff_patch': '', 'test_summary': None,
            'diff_truncated': False, 'stat_truncated': False, 'redacted': False}
        if isinstance(baseline, dict):
            value['collection_reason'] = baseline['reason']
        elif baseline is not None:
            try:
                project, task_id, before = baseline
                collected = compare_trees(project, task_id, before, snapshot_tree(project, task_id))
                value.update({key: collected[key] for key in
                    ('diff_stat', 'diff_patch', 'test_summary', 'diff_truncated', 'stat_truncated', 'redacted')})
                value.update(collection_state='complete', collection_reason='')
                if collected['summary_unavailable']:
                    value.update(collection_state='partial', collection_reason='test_summary_unavailable')
            except Exception as exc:
                value['collection_reason'] = getattr(exc, 'code', 'result_unavailable')
        self.results.save(value)
        if request_replay:
            self.replay_results()

    def _finish(self, session_id, capture, command_id=None, baseline=None, started=None, task=None):
        result = None
        try:
            result = self.supervisor.wait_session(session_id, timeout_s=float('inf'), on_line=capture.line)
            capture.finish(result.exit_code, resumable=self.supervisor.continuation_ready(session_id))
        except Exception:
            logging.getLogger(__name__).exception('Continuation failed: %s', session_id)
            self.supervisor.terminate_session(session_id, grace_s=1)
            capture.finish(-1, resumable=False)
        finally:
            try:
                self._store_result(session_id, task, command_id, baseline,
                                   started or time.monotonic(), result)
                if command_id:
                    self.results.finish_draft(command_id)
            except Exception:
                logging.getLogger(__name__).warning('Continuation result not retained')
            capture.stop_upload()
            capture.bridge.close()
            with self._lock:
                self._active_commands.discard(command_id)
