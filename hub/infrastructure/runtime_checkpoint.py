"""Private native progress fenced by the existing Run lease."""
from __future__ import annotations

import json
import sqlite3

from hub.infrastructure.platform_db_base import PlatformRepositoryError

MAX_CHECKPOINT_BYTES = 8 * 1024 * 1024
SAFE_PHASES = frozenset({'ready', 'response', 'receipt', 'complete', 'remote_wait'})
PHASES = SAFE_PHASES | {'provider_pending', 'tool_pending'}


def decode_checkpoint(raw):
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if (not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1
                or data.get('phase') not in PHASES
                or type(data.get('step')) is not int or data['step'] < 1
                or not isinstance(data.get('transcript'), list)
                or not all(isinstance(item, dict) for item in data.get('transcript', []))
                or not isinstance(data.get('usage'), dict)):
            raise ValueError('invalid_checkpoint')
        for key in ('input_tokens', 'output_tokens', 'total_tokens', 'provider_requests'):
            if type(data['usage'].get(key)) is not int or data['usage'][key] < 0:
                raise ValueError('invalid_checkpoint')
        if 'limits' in data and (not isinstance(data['limits'], dict) or set(data['limits']) != {'max_steps', 'max_tokens'}
                or any(type(value) is not int or value < 0 for value in data['limits'].values())):
            raise ValueError('invalid_checkpoint')
        if data['phase'] in {'response', 'receipt', 'remote_wait'} and not isinstance(data.get('response'), dict):
            raise ValueError('invalid_checkpoint')
        if data['phase'] == 'receipt' and not isinstance(data.get('receipt'), dict):
            raise ValueError('invalid_checkpoint')
        if data['phase'] == 'complete' and not isinstance(data.get('result'), dict):
            raise ValueError('invalid_checkpoint')
        if data['phase'] == 'complete':
            result = data['result']
            if (result.get('state') not in {'succeeded', 'failed', 'unknown', 'cancelled'}
                    or not isinstance(result.get('text'), str)
                    or type(result.get('steps')) is not int
                    or type(result.get('estimated_tokens')) is not int
                    or result.get('usage') != data['usage']):
                raise ValueError('invalid_checkpoint')
        return data
    except (ValueError, TypeError):
        raise PlatformRepositoryError('invalid_runtime_checkpoint') from None


def resumable_checkpoint(raw):
    try:
        checkpoint = decode_checkpoint(raw)
        return checkpoint is not None and checkpoint['phase'] in SAFE_PHASES
    except PlatformRepositoryError:
        return False


class RuntimeCheckpointMixin:
    def load_runtime_checkpoint(self, owner_id, run_id, *, lease_id, worker_id, now):
        conn = self._connect()
        try:
            row = conn.execute(
                'SELECT runtime_checkpoint FROM runs WHERE owner_id=? AND run_id=? '
                'AND lease_id=? AND lease_owner=? AND lease_expires_at>?',
                (owner_id, run_id, lease_id, worker_id, float(now))).fetchone()
            if row is None:
                raise PlatformRepositoryError('lease_mismatch')
            return decode_checkpoint(row['runtime_checkpoint'])
        except sqlite3.Error as exc:
            raise PlatformRepositoryError('platform_store') from exc
        finally:
            conn.close()

    def save_runtime_checkpoint(self, owner_id, run_id, checkpoint, *, lease_id, worker_id, now):
        try:
            raw = json.dumps(checkpoint, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
            if len(raw.encode('utf-8')) > MAX_CHECKPOINT_BYTES:
                raise PlatformRepositoryError('runtime_checkpoint_too_large')
            decode_checkpoint(raw)
        except (ValueError, TypeError):
            raise PlatformRepositoryError('invalid_runtime_checkpoint') from None
        conn = self._connect()
        try:
            cur = conn.execute(
                'UPDATE runs SET runtime_checkpoint=?,runtime_wait_command_id=? WHERE owner_id=? AND run_id=? '
                "AND state IN ('running','cancelling') AND lease_id=? AND lease_owner=? AND lease_expires_at>?",
                (raw, f"{run_id}:step:{checkpoint['step']}" if checkpoint['phase'] == 'remote_wait' else None, owner_id, run_id, lease_id, worker_id, float(now)))
            if cur.rowcount != 1:
                raise PlatformRepositoryError('lease_mismatch')
        except sqlite3.Error as exc:
            raise PlatformRepositoryError('platform_store') from exc
        finally:
            conn.close()

    def suspend_native_run(self, owner_id, run_id, *, lease_id, worker_id, now):
        conn = self._connect()
        try:
            cur = conn.execute(
                "UPDATE runs SET state=CASE WHEN cancel_requested=1 THEN 'cancelling' ELSE 'waiting_node' END,"
                'lease_id=NULL,lease_owner=NULL,lease_expires_at=NULL,runtime_resume_at=?,updated_at=? '
                "WHERE owner_id=? AND run_id=? AND state IN ('running','cancelling') "
                'AND lease_id=? AND lease_owner=? AND lease_expires_at>?',
                (float(now) + 0.1, float(now), owner_id, run_id, lease_id, worker_id, float(now)))
            if cur.rowcount != 1:
                raise PlatformRepositoryError('lease_mismatch')
            return self.get_run(owner_id, run_id)
        finally:
            conn.close()
