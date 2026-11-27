"""Durable conversation capture for regular runner tasks.

The heartbeat log is only a preview. This channel retains source records and
normalized conversation events, and replays unacknowledged data on later polls.

会话采集是旁路：采集失败（配额满、磁盘写失败…）只降级标记并记录日志，
绝不中止健康的任务。任务结束且 Hub 已确认全部事件后，本地记录与加密 spool
被清理；长期无法上送的已结束会话按保留期清理，防止缓存无限增长。
"""
import hashlib
import copy
import json
import logging
import shutil
import threading
import time
from pathlib import Path

from tools.session.bridge import SessionBridge
from tools.session.spool import OWNER_WAIT_S, SpoolBusy
from tools.supervisor.control_client_cli import _load_or_create_spool_key

log = logging.getLogger(__name__)


#: 已结束但始终未被 Hub 确认的会话，本地最多保留这么久（秒）。
CLOSED_RETENTION_S = 7 * 86400
#: 未结束（runner 崩溃遗留）的任务会话：全部已确认后保留期。
ORPHAN_RETENTION_S = 2 * 86400
#: sync.py 的原生会话身份前缀：其 spool 序号与 Hub 去重绑定，永不清理。
NATIVE_PREFIX = 'native_'
# Historical imports may fill their shared 128 MiB allocation. Keep a further
# 64 MiB available to live managed task/continuation events on the same disk.
ARCHIVE_MACHINE_BYTES = 128 * 1024 * 1024
LIVE_RESERVE_BYTES = 64 * 1024 * 1024


def session_prefix(machine):
    return 'runner_' + hashlib.sha256(machine.encode()).hexdigest()[:16] + '_'


class RunnerCapture:
    """One task/native session's capture.

    The capture owns its spool root's writer lock for its lifetime (released
    by ``bridge.close()``).  ``replay=True`` (used by :func:`replay_pending`)
    never waits for it: a spool a live writer owns raises
    :class:`~tools.session.spool.SpoolBusy` instead of being opened.
    All sessions under ``cache_dir/conversations`` share one machine quota.
    """

    def __init__(self, cfg, task, post, process_group_id=None, *, replay=False, preserve_source=True):
        self.cfg = cfg
        self.root = Path(cfg.cache_dir) / 'conversations'
        self.root.mkdir(parents=True, exist_ok=True)
        key = _load_or_create_spool_key(self.root / 'spool.key')
        self.session_id = session_prefix(cfg.machine) + task['attempt_id']
        self.record = self.root / (self.session_id + '.json')
        self.lock = threading.RLock()
        # Serializes flushers; held across the POST, unlike ``lock``.
        self.flush_lock = threading.Lock()
        # 逐行采集失败只降级：记录在 ``degraded``/``failed_lines``，不中止任务。
        self.degraded = None
        self.failed_lines = 0
        self.stopping = threading.Event()
        self.worker = None
        if replay and process_group_id is None:
            process_group_id = self._replay_group(task)
        self.bridge = SessionBridge({'agent': task['agent_type'], 'structured_stream': True}, {
            'session_id': self.session_id, 'machine_id': cfg.machine,
            'attempt_id': task['attempt_id'], 'agent_family': task['agent_type'],
            'process_group_id': process_group_id, 'managed': bool(process_group_id),
            'spool_root': self.root / self.session_id, 'spool_key': key,
            'spool_quota_root': self.root,
            'spool_machine_bytes': ARCHIVE_MACHINE_BYTES + (
                0 if task['attempt_id'].startswith(NATIVE_PREFIX) else LIVE_RESERVE_BYTES),
            'spool_owner_lock': True, 'spool_owner_timeout': 0 if replay else OWNER_WAIT_S,
            'preserve_source': preserve_source,
            'post_json': lambda batch: self._post(post, batch),
        })
        try:
            if not self.record.exists():
                # No credential or prompt in this recovery index.
                self.record.write_text(json.dumps({k: task[k] for k in
                    ('attempt_id', 'agent_type', 'task_id', 'project') if k in task},
                    ensure_ascii=False), encoding='utf-8')
                self.bridge.start()
                metadata = {'task_id': task['task_id']}
                if task.get('agent_type') == 'agy' and task.get('provider_account_id'):
                    metadata['provider_account_id'] = task['provider_account_id']
                self.bridge.ingest_one({'kind': 'session_metadata', 'payload': metadata})
                if 'instruction' in task:
                    self.bridge.record_user_turn(task['instruction'])
            elif replay:
                # Upgrade retained idle conversations without replaying a prompt.
                # The spool writer lock above excludes running captures.
                from fleet_core.session_models import AGENT_PROTOCOLS
                data = json.loads(self.record.read_text(encoding='utf-8'))
                if data.get('closed') and data.get('resumable') and self._failed_native_completion():
                    # Repair older clients' identity-only claim without resuming
                    # or rewriting the original failed native session.
                    self.bridge.ingest_one({'kind': 'session_metadata', 'payload': {
                        'native_resume': False, 'model_takeover': False}})
                    data['resumable'] = False
                    self.record.write_text(json.dumps(data), encoding='utf-8')
                if (data.get('closed') and data.get('resumable') and not data.get('model_takeover_version')
                        and data.get('agent_type') in AGENT_PROTOCOLS):
                    self.bridge.ingest_one({'kind': 'session_metadata', 'payload': {'model_takeover': True}})
                    data['model_takeover_version'] = 1
                    self.record.write_text(json.dumps(data), encoding='utf-8')
        except Exception:
            self.bridge.close()
            raise

    def _replay_group(self, task):
        """Retain the original managed envelope, without adopting a process."""
        from tools.supervisor.model import ManagedSessionManifest
        root = self.cfg.supervisor_manifest_dir
        if root is None:
            return None
        try:
            manifest = ManagedSessionManifest.from_json(
                (Path(root) / (self.session_id + '.json')).read_text(encoding='utf-8'))
            if (manifest.session_id == self.session_id and manifest.machine_id == self.cfg.machine
                    and manifest.attempt_id == task['attempt_id'] and manifest.agent == task['agent_type']):
                return manifest.process_group_id
        except (OSError, ValueError, TypeError):
            pass
        return None

    def _failed_native_completion(self):
        from tools.supervisor.model import ManagedSessionManifest
        root = self.cfg.supervisor_manifest_dir
        if root is None:
            return False
        try:
            manifest = ManagedSessionManifest.from_json(
                (Path(root) / (self.session_id + '.json')).read_text(encoding='utf-8'))
            return (manifest.session_id == self.session_id and manifest.machine_id == self.cfg.machine
                    and manifest.state == 'terminated' and manifest.reason != 'completed')
        except (OSError, ValueError, TypeError):
            return False

    def _post(self, post, batch):
        status, body = post(self.cfg, '/api/runner-session-events', batch['events'])
        if status != 200:
            log.warning('Conversation upload pending: session=%s status=%s', self.session_id, status)
            return {}
        return body

    def line(self, line, *, strict=False):
        """Capture one output line; returns True when it was persisted.

        Default (task output path): a failure degrades — it is logged once,
        counted and marked on ``degraded`` — and never propagates, so a
        healthy task keeps running.  ``strict=True`` re-raises, for callers
        (native sync) that must not advance their source cursor past a line
        that was not persisted.
        """
        with self.lock:
            try:
                try:
                    value = json.loads(line)
                except ValueError:
                    self.bridge.record_source(line)
                else:
                    if isinstance(value, dict):
                        self.bridge.ingest_one(value)
                    else:
                        self.bridge.record_source(line)
                return True
            except Exception as exc:
                if strict:
                    raise
                self.failed_lines += 1
                if self.degraded is None:
                    self.degraded = type(exc).__name__
                    log.warning('Conversation capture degraded: session=%s error=%s',
                                self.session_id, self.degraded, exc_info=True)
                return False

    def start_upload(self):
        def upload_loop():
            while not self.stopping.wait(1):
                try:
                    self.flush(should_stop=self.stopping.is_set)
                except Exception:
                    log.exception('Conversation upload failed: %s', self.session_id)
        self.worker = threading.Thread(target=upload_loop, name='conversation-upload', daemon=True)
        self.worker.start()

    def stop_upload(self):
        self.stopping.set()
        if self.worker is not None:
            self.worker.join()

    def flush(self, *, should_stop=None):
        # ``line()`` only contends for ``lock`` while the spool is read or
        # acked; the HTTP POST runs outside it.
        with self.flush_lock:
            return self.bridge.flush(guard=self.lock, should_stop=should_stop)

    def begin_turn(self, text):
        with self.lock:
            data = json.loads(self.record.read_text())
            data.update(closed=False, resumable=True)
            self.record.write_text(json.dumps(data), encoding='utf-8')
            self.bridge.ingest_one({'kind': 'session_metadata', 'payload': {
                'native_resume': True, 'resumed': True}})
            self.bridge.record_user_turn(text)

    def refresh_metadata(self, *, native_path=None, family=None, force=False, strict=False):
        """Recover display facts without touching a content cursor or Agent.

        The bridge owns the spool writer lock. Publication precedes the atomic
        digest checkpoint, so a failed checkpoint/upload remains recoverable.
        This side channel never changes execution/resume capabilities.
        """
        from tools.session.native_metadata import read_jsonl_metadata, read_retained_metadata
        from tools.runner_source import atomic_private_json
        try:
            with self.lock:
                data = json.loads(self.record.read_text(encoding='utf-8'))
                state = data.get('native_metadata')
                if state is not None and (not isinstance(state, dict) or state.get('version') != 1
                        or any(not isinstance(state.get(key), str) or len(state[key]) != 64
                            or any(char not in '0123456789abcdef' for char in state[key])
                            for key in ('fingerprint', 'digest'))):
                    raise ValueError('native_metadata_checkpoint')
                if native_path is not None:
                    snapshot = read_jsonl_metadata(family or data['agent_type'], native_path)
                elif data['attempt_id'].startswith(NATIVE_PREFIX):
                    return False  # archive source discovery owns these paths
                else:
                    snapshot = read_retained_metadata(self.cfg, self.session_id, data['agent_type'])
                if snapshot is None:
                    return False
                fingerprint, payloads = snapshot
                digest = hashlib.sha256(json.dumps(payloads, ensure_ascii=False, sort_keys=True,
                    separators=(',', ':')).encode('utf-8')).hexdigest()
                if not force and state and state['fingerprint'] == fingerprint and state['digest'] == digest:
                    return False
                if force or not state or state['digest'] != digest:
                    for payload in payloads:
                        if not self.bridge.ingest_one({'kind': 'session_metadata',
                                'payload': payload}):
                            raise RuntimeError('native_metadata_pending')
                data['native_metadata'] = {'version': 1, 'fingerprint': fingerprint, 'digest': digest}
                atomic_private_json(self.record, data)
                return True
        except Exception:
            log.warning('Native metadata recovery pending: session=%s', self.session_id)
            if strict:
                raise RuntimeError('native_metadata_pending') from None
            return False

    def finish(self, exit_code, *, resumable=False):
        self.stop_upload()
        self.refresh_metadata()
        with self.lock:
            try:
                from fleet_core.session_models import AGENT_PROTOCOLS
                task = json.loads(self.record.read_text(encoding='utf-8'))
                self.bridge.ingest_one({'kind': 'session_metadata', 'payload': {
                    'native_resume': resumable,
                    'model_takeover': bool(resumable and task.get('agent_type') in AGENT_PROTOCOLS)}})
                self.bridge.ingest_one({'kind': 'process_exit', 'payload': {'exit_code': exit_code}})
                self.bridge.ingest_one({'kind': 'session_close', 'payload': {'reason': 'completed' if exit_code == 0 else 'failed', 'exit_code':exit_code}})
            except Exception:
                log.warning('Conversation close events not captured: session=%s',
                            self.session_id, exc_info=True)
            self._mark_closed(resumable=resumable)
            try:
                self.flush()
            except Exception:
                log.warning('Conversation final upload pending: session=%s',
                            self.session_id, exc_info=True)
            self.bridge.close()
            prune_if_done(self)

    def _mark_closed(self, *, resumable=False):
        try:
            data = json.loads(self.record.read_text())
            if isinstance(data, dict) and not data.get('closed'):
                data['closed'] = True
                data['resumable'] = bool(resumable)
                self.record.write_text(json.dumps(data))
        except (OSError, ValueError):
            pass

    def fully_acked(self):
        uploader = getattr(self.bridge, 'uploader', None)
        spool = getattr(uploader, 'spool', None)
        if spool is None:
            return False
        status = spool.status()
        return status.get('ack_sequence', 0) >= status.get('last_sequence', 0)


def _remove_session(root, record, session_id):
    try:
        record.unlink()
    except FileNotFoundError:
        pass
    shutil.rmtree(root / session_id, ignore_errors=True)


def prune_if_done(capture, *, now=None):
    """Delete a task session's spool once it is safe to do so.

    Safe = closed and fully acknowledged; or closed and older than
    ``CLOSED_RETENTION_S``; or an orphan (never closed) that is fully
    acknowledged and older than ``ORPHAN_RETENTION_S``.  Native sync sessions
    are never pruned (their spool sequence numbers are the hub dedup key).
    The record shrinks to a content-free tombstone (removed with the spool
    once past ``CLOSED_RETENTION_S``).  Returns True when pruned.
    """
    if capture.session_id[len(session_prefix(capture.cfg.machine)):].startswith(NATIVE_PREFIX):
        return False
    try:
        data = json.loads(capture.record.read_text())
        age = (time.time() if now is None else now) - capture.record.stat().st_mtime
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or data.get('pruned'):
        return False
    if data.get('resumable'):
        # Retain the sequence checkpoint for subsequent turns. Acknowledged
        # encrypted segments are already compacted by LocalSpool.ack().
        return False
    closed = bool(data.get('closed'))
    acked = capture.fully_acked()
    if not ((closed and (acked or age > CLOSED_RETENTION_S))
            or (acked and age > ORPHAN_RETENTION_S)):
        return False
    capture.bridge.close()
    if age > CLOSED_RETENTION_S:
        _remove_session(capture.root, capture.record, capture.session_id)
    else:
        # 删除 spool 正文，仅保留不含内容的小墓碑（attempt/task id），
        # 供本地按 attempt 定位会话 id；墓碑不再重放，超保留期后删除。
        shutil.rmtree(capture.root / capture.session_id, ignore_errors=True)
        tomb = {k: data[k] for k in ('attempt_id', 'agent_type', 'task_id', 'project') if k in data}
        tomb.update(closed=closed, pruned=True)
        capture.record.write_text(json.dumps(tomb))
    return True


def _expire_tombstone(record, data, now=None):
    """True when ``record`` is a pruned tombstone (removed once expired)."""
    if not (isinstance(data, dict) and data.get('pruned')):
        return False
    try:
        age = (time.time() if now is None else now) - record.stat().st_mtime
        if age > CLOSED_RETENTION_S:
            record.unlink()
    except OSError:
        pass
    return True


def replay_pending(cfg, post, *, should_stop=lambda: False):
    root = Path(cfg.cache_dir) / 'conversations'
    if not root.exists():
        return
    for record in root.glob(session_prefix(cfg.machine) + '*.json'):
        if should_stop():
            return
        capture = None
        try:
            data = json.loads(record.read_text())
            if _expire_tombstone(record, data):
                continue
            capture = RunnerCapture(cfg, data, post, replay=True)
            if (data.get('agent_type') == 'agy' and data.get('attempt_id', '').startswith(NATIVE_PREFIX)
                    and (capture.root / (capture.session_id + '.agy-cursor')).exists()):
                from tools.session.agy_archive import recover_receipt
                recover_receipt(capture)
            capture.refresh_metadata()
            capture.flush(should_stop=should_stop)
        except SpoolBusy:
            # A live runner/sync process owns this spool; it uploads itself.
            log.debug('Conversation replay skipped (live writer): %s', record.name)
            continue
        except Exception:
            log.exception('Conversation replay failed: %s', record.name)
        finally:
            if capture is not None:
                capture.bridge.close()
                try:
                    prune_if_done(capture)
                except Exception:
                    log.exception('Conversation prune failed: %s', record.name)


class ConversationReplay:
    """Recover retained uploads without delaying the runner's task polling."""

    def __init__(self, cfg, post, *, interval=15):
        self.cfg = copy.copy(cfg)
        self.cfg.transport = copy.copy(cfg.transport) if cfg.transport is not None else None
        self.post = post
        self.interval = interval
        self.stopping = threading.Event()
        self.thread = None

    def start(self):
        def replay():
            while not self.stopping.is_set():
                try:
                    replay_pending(self.cfg, self.post, should_stop=self.stopping.is_set)
                except Exception:
                    log.exception('Conversation replay scan failed')
                self.stopping.wait(self.interval)
        self.thread = threading.Thread(target=replay, name='conversation-replay', daemon=True)
        self.thread.start()

    def close(self):
        self.stopping.set()
        if self.thread is not None:
            self.thread.join(timeout=1)
