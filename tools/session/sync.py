"""Incrementally sync native JSONL conversations using the runner credential.

Run independently of task execution: python -m tools.session.sync --config ...
--source codex=/root/.codex/sessions --source claude_code=/root/.claude/projects.
"""
import argparse
import hashlib
import json
import logging
import copy
import threading
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlencode

from tools.runner_config import load_config
from tools.session.capture.native_jsonl import tailer
from tools.session.runner_capture import RunnerCapture, replay_pending, session_prefix
from tools.transport import Transport
from agent_profiles import PROFILES

log = logging.getLogger(__name__)

#: Native JSONL lines above this size are skipped (logged), never synced.
MAX_LINE_BYTES = 16 << 20


def native_identity(family, path):
    return 'native_' + hashlib.sha256((family + '\0' + str(Path(path).resolve())).encode()).hexdigest()[:32]


def default_sources():
    """Existing native conversation stores on this node, including history."""
    return [(family, Path(profile.native_session_root).expanduser())
            for family, profile in PROFILES.items()
            if profile.native_session_root and Path(profile.native_session_root).expanduser().is_dir()]


class NativeArchiveSync:
    """Independent incremental archive worker; slow uploads never block control polling."""

    def __init__(self, cfg, *, interval=15, sources=default_sources, send=None):
        self.cfg = copy.copy(cfg)
        self.cfg.transport = copy.copy(cfg.transport) if cfg.transport is not None else None
        self.interval = interval
        self.sources = sources
        self.send = send or post
        self.stopping = threading.Event()
        self.thread = None
        self.completed = OrderedDict()
        self._archive_paths = None

    def start(self):
        self.thread = threading.Thread(target=self._run, name='native-session-sync', daemon=True)
        self.thread.start()

    def close(self):
        self.stopping.set()
        if self.thread is not None:
            # An in-flight upload has its own finite transport timeout. No new
            # file is started after stop; the daemon cannot pin process exit.
            self.thread.join(timeout=1)

    def scan_once(self):
        desktop = None
        bindings = {}
        if self.cfg.managed_enabled:
            from tools.supervisor.desktop_control import DesktopControl
            desktop = DesktopControl(self.cfg)
            bindings = desktop.bindings()
        sources = list(self.sources())
        # The desktop Host knows sessions even when no CLI rollout exists.
        # Bind and query those before scanning potentially large CLI archives.
        if desktop is not None:
            zroot = next((root for family, root in sources if family == 'zcode'),
                         Path(PROFILES['zcode'].native_session_root).expanduser())
            for native_id, row in sorted(bindings.items(),
                    key=lambda item: str(item[1].get('updatedAt') or ''), reverse=True):
                if self.stopping.is_set():
                    return
                path = zroot / ('model-io-' + native_id + '.jsonl')
                sid = session_prefix(self.cfg.machine) + native_identity('zcode', path)
                checkpoint = self.cfg.cache_dir / 'conversations' / (sid + '.desktop-cursor')
                if not path.exists() or checkpoint.exists():
                    try:
                        sync_desktop(self.cfg, desktop, row, path, self.send)
                    except Exception:
                        log.warning('Desktop session sync pending: session=%s', sid)
        # Keep traversal progress across bounded scan slices. A large or slow
        # history must not postpone the next desktop refresh until every file
        # on this machine has uploaded. One in-flight upload may finish first.
        if self._archive_paths is None:
            self._archive_paths = ((family, root, path)
                for family, root in sorted(sources, key=lambda source: source[0] != 'zcode')
                for path in root.rglob('*.jsonl'))
        deadline = time.monotonic() + self.interval
        visited = False
        while True:
            if visited and time.monotonic() >= deadline:
                return
            item = next(self._archive_paths, None)
            if item is None:
                self._archive_paths = None
                return
            family, root, path = item
            visited = True
            if self.stopping.is_set():
                return
            try:
                if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                    continue
                stat = path.stat()
                native_id = path.stem.removeprefix('model-io-')
                bound = family == 'zcode' and native_id in bindings
                fingerprint = (stat.st_ino, stat.st_size, stat.st_mtime_ns, bound)
                if self.completed.get((family, path)) == fingerprint:
                    continue
                identity = native_identity(family, path)
                if (self.cfg.cache_dir / 'conversations' / (session_prefix(self.cfg.machine) + identity + '.desktop-cursor')).exists():
                    continue  # one source owns this session's message stream
                group = desktop.register(session_prefix(self.cfg.machine)+identity,
                    native_id, identity) if bound else None
                sync_file(self.cfg, family, path, self.send, desktop_group=group)
                # Only skip future scans when the complete file was read
                # and acknowledged. Large files continue next pass; pending
                # uploads and partial lines are never marked complete.
                identity = native_identity(family, path)
                checkpoint = self.cfg.cache_dir / 'conversations' / (session_prefix(self.cfg.machine) + identity + '.cursor')
                if json.loads(checkpoint.read_text())['offset'] == stat.st_size:
                    self.completed[(family, path)] = fingerprint
                    self.completed.move_to_end((family, path))
                    if len(self.completed) > 4096:
                        self.completed.popitem(last=False)
            except Exception:
                log.warning('Native session sync failed: family=%s code=sync_failed', family)

    def _run(self):
        while not self.stopping.is_set():
            try:
                self.scan_once()
            except Exception:
                log.warning('Native session discovery failed: code=source_unavailable')
            self.stopping.wait(self.interval)


def post(cfg, path, body):
    if cfg.transport is None:
        cfg.transport = Transport()
    return cfg.transport.post_json(cfg.hub + path, body, {
        'Content-Type': 'application/json', 'User-Agent': 'agent-fleet-session-sync/1',
        'X-Runner-Credential': cfg.machine + ':' + cfg.credential}, timeout=15)


def sync_desktop(cfg, desktop, row, native_path, send=post):
    """Bounded official message pagination for sessions without a CLI rollout.

    Keep the last page cursor and its message IDs so new messages are picked up
    on subsequent scans without replaying prior pages. The original native ID
    determines the Fleet identity whether or not a rollout is later created.
    """
    native_id = row['session']['sessionId']
    identity = native_identity('zcode', native_path)
    sid = session_prefix(cfg.machine) + identity
    group = desktop.register(sid, native_id, identity)
    capture = RunnerCapture(cfg, {'attempt_id':identity,'agent_type':'zcode','task_id':''},
                            send, process_group_id=group)
    checkpoint = capture.root / (sid + '.desktop-cursor')
    try:
        try:
            state = json.loads(checkpoint.read_text(encoding='utf-8'))
        except FileNotFoundError:
            state = {'cursor':None, 'seen':[]}
            checkpoint.write_text(json.dumps(state), encoding='utf-8')
        capture.bridge.ingest_one({'kind':'session_metadata','payload':{'desktop_bridge':True}})
        cursors = {state['cursor']}
        while row.get('status') == 'idle':
            query = {'session_id':native_id}
            if state['cursor']:
                query['cursor'] = state['cursor']
            page = desktop.client.request('/messages?' + urlencode(query))
            for message in page.get('messages', []):
                mid, role, text = message.get('id'), message.get('role'), message.get('text')
                if not mid or mid in state['seen']:
                    continue
                if role in {'user','assistant'} and isinstance(text, str) and text:
                    capture.bridge.ingest_one({'kind':role+'_message',
                        'payload':{'text':text,'is_complete':True}})
                state['seen'].append(mid)
                checkpoint.write_text(json.dumps(state), encoding='utf-8')
            cursor = page.get('nextCursor')
            if cursor and cursor not in cursors:
                state = {'cursor':cursor, 'seen':[]}
                checkpoint.write_text(json.dumps(state), encoding='utf-8')
                cursors.add(cursor)
            else:
                break
        capture.flush()
    finally:
        capture.bridge.close()


def sync_file(cfg, family, path, send=post, *, desktop_group=None):
    path = Path(path).resolve()
    identity = native_identity(family, path)
    capture = RunnerCapture(cfg, {'attempt_id': identity, 'agent_type': family, 'task_id': ''},
                            send, process_group_id=desktop_group)
    checkpoint = capture.root / (capture.session_id + '.cursor')
    reader = tailer(str(path), checkpoint_path=str(checkpoint), max_line_bytes=MAX_LINE_BYTES)
    try:
        if family == 'zcode':
            capture.bridge.ingest_one({'kind':'session_metadata', 'payload':{'desktop_bridge':bool(desktop_group)}})
        start = reader.offset
        entries = reader.read_entries()
        if reader.last_error == 'nlj_oversized_line':
            # The tailer already consumed the oversized line's bytes and kept
            # the rows around it: checkpoint past it instead of raising
            # forever on the same offset (a permanent sync stall).
            log.warning('Native conversation line over %d bytes skipped: family=%s file=%s',
                        MAX_LINE_BYTES, family, path.name)
        elif reader.last_error:
            raise RuntimeError(reader.last_error)
        end = reader.offset
        # 逐行推进 checkpoint：配额/写入失败在中途抛出时，已入 spool 的前缀
        # 已落盘为 checkpoint，下次从失败行续传，不会以新 sequence 重复上送。
        reader.offset = start
        for row, row_end in entries:
            capture.line(json.dumps(row, ensure_ascii=False), strict=True)
            reader.offset = row_end
            reader.checkpoint_to(str(checkpoint))
        # 尾部被跳过的空行/坏行也一并确认
        reader.offset = end
        reader.checkpoint_to(str(checkpoint))
        capture.flush()
        status = capture.bridge.spool.status()
        if (capture.bridge.uploader.last_error
                or status['ack_sequence'] < status['next_sequence'] - 1):
            raise RuntimeError("conversation_upload_pending")
        return len(entries)
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
