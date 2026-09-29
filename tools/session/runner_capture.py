"""Durable conversation capture for regular runner tasks.

The heartbeat log is only a preview. This channel retains source records and
normalized conversation events, and replays unacknowledged data on later polls.
"""
import hashlib
import json
import logging
import threading
from pathlib import Path

from tools.session.bridge import SessionBridge
from tools.supervisor.control_client_cli import _load_or_create_spool_key

log = logging.getLogger(__name__)


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
        self.error = None
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

    def line(self, line):
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
            except Exception as exc:
                self.error = type(exc).__name__
                log.exception('Conversation capture failed: session=%s', self.session_id)
                raise

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
            self.bridge.ingest_one({'kind': 'process_exit', 'payload': {'exit_code': exit_code}})
            self.bridge.ingest_one({'kind': 'session_close', 'payload': {'reason': 'completed' if exit_code == 0 else 'failed'}})
            self.flush()
            self.bridge.close()


def replay_pending(cfg, post):
    root = Path(cfg.cache_dir) / 'conversations'
    if not root.exists():
        return
    for record in root.glob(session_prefix(cfg.machine) + '*.json'):
        capture = None
        try:
            capture = RunnerCapture(cfg, json.loads(record.read_text()), post)
            capture.flush()
        except Exception:
            log.exception('Conversation replay failed: %s', record.name)
        finally:
            if capture is not None:
                capture.bridge.close()
