"""Durable conversation capture for regular runner tasks.

The heartbeat log is only a preview. This channel retains source records and
normalized conversation events, and replays unacknowledged data on later polls.

会话采集是旁路：采集失败（配额满、磁盘写失败…）只降级标记并记录日志，
绝不中止健康的任务。任务结束且 Hub 已确认全部事件后，本地记录与加密 spool
被清理；长期无法上送的已结束会话按保留期清理，防止缓存无限增长。
"""
import hashlib
import json
import logging
import shutil
import threading
import time
from pathlib import Path

from tools.session.bridge import SessionBridge
from tools.supervisor.control_client_cli import _load_or_create_spool_key

log = logging.getLogger(__name__)


#: 已结束但始终未被 Hub 确认的会话，本地最多保留这么久（秒）。
CLOSED_RETENTION_S = 7 * 86400
#: 未结束（runner 崩溃遗留）的任务会话：全部已确认后保留期。
ORPHAN_RETENTION_S = 2 * 86400
#: sync.py 的原生会话身份前缀：其 spool 序号与 Hub 去重绑定，永不清理。
NATIVE_PREFIX = 'native_'


def session_prefix(machine):
    return 'runner_' + hashlib.sha256(machine.encode()).hexdigest()[:16] + '_'


class RunnerCapture:
    def __init__(self, cfg, task, post, process_group_id=None):
        self.cfg = cfg
        self.root = Path(cfg.cache_dir) / 'conversations'
        self.root.mkdir(parents=True, exist_ok=True)
        key = _load_or_create_spool_key(self.root / 'spool.key')
        self.session_id = session_prefix(cfg.machine) + task['attempt_id']
        self.record = self.root / (self.session_id + '.json')
        self.lock = threading.RLock()
        # 逐行采集失败只降级：记录在 ``degraded``/``failed_lines``，不中止任务。
        self.degraded = None
        self.failed_lines = 0
        self.stopping = threading.Event()
        self.worker = None
        self.bridge = SessionBridge({'agent': task['agent_type'], 'structured_stream': True}, {
            'session_id': self.session_id, 'machine_id': cfg.machine,
            'attempt_id': task['attempt_id'], 'agent_family': task['agent_type'],
            'process_group_id': process_group_id, 'managed': bool(process_group_id),
            'spool_root': self.root / self.session_id, 'spool_key': key,
            'preserve_source': True,
            'post_json': lambda batch: self._post(post, batch),
        })
        if not self.record.exists():
            # No credential or prompt in this recovery index.
            self.record.write_text(json.dumps({k: task[k] for k in ('attempt_id', 'agent_type', 'task_id')}))
            self.bridge.start()
            self.bridge.ingest_one({'kind': 'session_metadata', 'payload': {'task_id': task['task_id']}})
            if 'instruction' in task:
                self.bridge.ingest_one({'kind': 'user_message', 'payload': {'text': task['instruction']}})

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
                    self.flush()
                except Exception:
                    log.exception('Conversation upload failed: %s', self.session_id)
        self.worker = threading.Thread(target=upload_loop, name='conversation-upload', daemon=True)
        self.worker.start()

    def stop_upload(self):
        self.stopping.set()
        if self.worker is not None:
            self.worker.join()

    def flush(self):
        with self.lock:
            return self.bridge.flush()

    def finish(self, exit_code):
        self.stop_upload()
        with self.lock:
            try:
                self.bridge.ingest_one({'kind': 'process_exit', 'payload': {'exit_code': exit_code}})
                self.bridge.ingest_one({'kind': 'session_close', 'payload': {'reason': 'completed' if exit_code == 0 else 'failed'}})
            except Exception:
                log.warning('Conversation close events not captured: session=%s',
                            self.session_id, exc_info=True)
            self._mark_closed()
            try:
                self.flush()
            except Exception:
                log.warning('Conversation final upload pending: session=%s',
                            self.session_id, exc_info=True)
            self.bridge.close()
            prune_if_done(self)

    def _mark_closed(self):
        try:
            data = json.loads(self.record.read_text())
            if isinstance(data, dict) and not data.get('closed'):
                data['closed'] = True
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
        tomb = {k: data[k] for k in ('attempt_id', 'agent_type', 'task_id') if k in data}
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


def replay_pending(cfg, post):
    root = Path(cfg.cache_dir) / 'conversations'
    if not root.exists():
        return
    for record in root.glob(session_prefix(cfg.machine) + '*.json'):
        capture = None
        try:
            data = json.loads(record.read_text())
            if _expire_tombstone(record, data):
                continue
            capture = RunnerCapture(cfg, data, post)
            capture.flush()
        except Exception:
            log.exception('Conversation replay failed: %s', record.name)
        finally:
            if capture is not None:
                capture.bridge.close()
                try:
                    prune_if_done(capture)
                except Exception:
                    log.exception('Conversation prune failed: %s', record.name)
