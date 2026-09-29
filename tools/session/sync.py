"""Incrementally sync native JSONL conversations using the runner credential.

Run independently of task execution: python -m tools.session.sync --config ...
--source codex=/root/.codex/sessions --source claude_code=/root/.claude/projects.
"""
import argparse
import hashlib
import json
import logging
import time
from pathlib import Path

from tools.runner_config import load_config
from tools.session.capture.native_jsonl import tailer
from tools.session.runner_capture import RunnerCapture, replay_pending
from tools.transport import Transport

log = logging.getLogger(__name__)


def post(cfg, path, body):
    if cfg.transport is None:
        cfg.transport = Transport()
    return cfg.transport.post_json(cfg.hub + path, body, {
        'Content-Type': 'application/json', 'User-Agent': 'agent-fleet-session-sync/1',
        'X-Runner-Credential': cfg.machine + ':' + cfg.credential}, timeout=15)


def sync_file(cfg, family, path, send=post):
    path = Path(path).resolve()
    identity = 'native_' + hashlib.sha256((family + '\0' + str(path)).encode()).hexdigest()[:32]
    capture = RunnerCapture(cfg, {'attempt_id': identity, 'agent_type': family, 'task_id': ''}, send)
    checkpoint = capture.root / (capture.session_id + '.cursor')
    reader = tailer(str(path), checkpoint_path=str(checkpoint), max_line_bytes=16 << 20)
    try:
        rows = reader.read()
        if reader.last_error:
            raise RuntimeError(reader.last_error)
        for row in rows:
            capture.line(json.dumps(row, ensure_ascii=False))
        reader.checkpoint_to(str(checkpoint))
        capture.flush()
        if capture.bridge.uploader.last_error:
            raise RuntimeError("conversation_upload_pending")
        return len(rows)
    finally:
        capture.bridge.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='同步 VPS 原生 JSONL 会话到 Hub')
    parser.add_argument('--config', required=True)
    parser.add_argument('--source', action='append', required=True, metavar='FAMILY=DIRECTORY')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--interval', type=float, default=3)
    args = parser.parse_args(argv)
    if args.interval <= 0:
        parser.error('--interval 必须大于 0')
    sources = []
    for source in args.source:
        family, sep, directory = source.partition('=')
        if not sep or not family or not Path(directory).expanduser().is_dir():
            parser.error('--source 必须指向存在的 FAMILY=DIRECTORY')
        sources.append((family, Path(directory).expanduser()))
    cfg = load_config(args.config)
    logging.basicConfig(level=logging.INFO)
    try:
        while True:
            replay_pending(cfg, post)
            failed = False
            for family, root in sources:
                for path in sorted(root.rglob('*.jsonl')):
                    try:
                        sync_file(cfg, family, path)
                    except Exception:
                        failed = True
                        log.exception('Native conversation sync failed: family=%s file=%s', family, path.name)
            if args.once:
                return 1 if failed else 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
