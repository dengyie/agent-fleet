"""Native Muse sessions and bounded, auditable permission decisions."""
import hashlib
import re
import threading
import uuid
import base64
from contextlib import contextmanager
from contextlib import nullcontext
from urllib.parse import unquote

from hub.integrations.muse.client import MuseClient, MuseError, normalize_login


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,180}', value):
        raise MuseError('invalid_muse_id', '会话或请求标识不合法。', 400)
    return value


class MuseService:
    DEFINITELY_NOT_DISPATCHED = frozenset({
        'muse_not_ready', 'muse_auth_unavailable', 'muse_dependency',
        'muse_protocol', 'muse_confidential_vm',
    })
    def __init__(self, repository, client_factory=MuseClient, owner_enabled=None, owner_operation=None):
        self.repository, self.client_factory = repository, client_factory
        self.owner_enabled = owner_enabled or (lambda owner: True)
        self.owner_operation = owner_operation or (lambda owner: nullcontext(True))
        self.clients = {}
        self.client_cache_lock = threading.Lock()
        self.locks = [threading.RLock() for _ in range(64)]
        self.stop_event = threading.Event()
        self.worker = None
        self.worker_lock = threading.Lock()

    def _lock(self, owner, account_id=None):
        key = owner + ':' + (account_id or 'default')
        return self.locks[int(hashlib.sha256(key.encode()).hexdigest(), 16) % len(self.locks)]

    def _account(self, owner, account_id=None, expected_revision=None):
        account = self.repository.account(owner, account_id)
        if account is None:
            raise MuseError('muse_disconnected', '请先导入 Muse 登录态。', 409)
        if expected_revision is not None and str(account['revision']) != str(expected_revision):
            raise MuseError('muse_connection_changed', 'Muse 登录态已切换，请重新加载当前账号。', 409)
        return account

    def _client(self, owner, account_id=None, expected_revision=None):
        account = self._account(owner, account_id, expected_revision)
        key = (owner, account['account_id'])
        with self.client_cache_lock:
            current = self.clients.get(key)
            if current is None or current[0] != account['revision']:
                current = (account['revision'], self.client_factory(account['cookies']))
                self.clients[key] = current
        return current[1]

    @contextmanager
    def _lease(self, owner, account_id=None, expected_revision=None):
        with self.repository.account_lease(owner, account_id, expected_revision) as account:
            yield account, self._client(owner, account['account_id'], account['revision'])

    @contextmanager
    def _account_change(self, owner, account_id):
        with self.repository.account_change(owner, account_id):
            yield

    def status(self, owner, account_id=None):
        accounts = self.repository.list_accounts(owner)
        account = self.repository.account(owner, account_id)
        return {'connected': account is not None,
                'account_id': account['account_id'] if account else None,
                'label': account['label'] if account else None,
                'connection_revision': str(account['revision']) if account else None,
                'settings_revision': account['settings_revision'] if account else None,
                'settings': account['settings'] if account else {'auto_approve_sessions': [], 'auto_approve_all': False},
                'accounts': [{'account_id': value['account_id'], 'label': value['label'],
                              'connection_revision': str(value['revision']),
                              'settings_revision': value['settings_revision']}
                             for value in accounts],
                'transport': 'native', 'audit': self.repository.audit(owner, account['account_id']) if account else []}

    def import_login(self, owner, value, account_id=None, label=None):
        cookies = normalize_login(value)
        if account_id is None:
            account_id = 'default'
        identifier(account_id)
        with self._account_change(owner, account_id):
            client = self.client_factory(cookies)
            sessions = client.request('GET', '/api/session/list', {'include_peer_sidechats': True})
            if (not isinstance(sessions, dict) or not isinstance(sessions.get('sessions', []), list)):
                raise MuseError('muse_protocol', 'Muse 返回的会话列表格式不合法。')
            self.repository.import_login(owner, cookies, account_id, label or account_id)
            with self.client_cache_lock:
                self.clients.pop((owner, account_id), None)
        return {**self.status(owner, account_id), 'sessions': sessions.get('sessions', [])}

    def disconnect(self, owner, account_id=None, expected_revision=None):
        account = self._account(owner, account_id, expected_revision)
        with self._account_change(owner, account['account_id']):
            self.repository.disconnect(owner, account['account_id'], account['revision'])
            with self.client_cache_lock:
                self.clients.pop((owner, account['account_id']), None)
        return self.status(owner, account['account_id'])

    def sessions(self, owner, account_id=None, expected_revision=None):
        with self._lease(owner, account_id, expected_revision) as (_, client):
            return client.request('GET', '/api/session/list', {'include_peer_sidechats': True})

    def history(self, owner, session_id, before=None, account_id=None, expected_revision=None, after=None, continuation=None):
        params = {'limit': 60, 'session_id': identifier(session_id), 'transcript_mode': 'messages'}
        if before is not None:
            if not isinstance(before, int) or before < 1:
                raise MuseError('invalid_cursor', '历史游标不合法。', 400)
            params['before_seq'] = before
        if after is not None:
            if not isinstance(after, int) or after < 1:
                raise MuseError('invalid_cursor', '历史游标不合法。', 400)
            if continuation is not None:
                if not isinstance(continuation, int) or continuation < 1:
                    raise MuseError('invalid_cursor', '历史游标不合法。', 400)
                params['before_seq'] = continuation
        # Keep one account lease for all pages; account replacement waits for
        # the read, while other operations on the account remain concurrent.
        with self._lease(owner, account_id, expected_revision) as (_, client):
            result = client.request('GET', '/chat/history', params)
            if not isinstance(result, dict) or not isinstance(result.get('chat_events', []), list):
                raise MuseError('muse_protocol', 'Muse 返回的历史格式不合法。')
            events = result.get('chat_events', [])
            if after is None:
                result.setdefault('has_more_before', bool(result.get('has_more')))
                result.setdefault('has_more_after', False)
                return result
            collected = list(events)
            has_more = bool(result.get('has_more'))
            cursor = min((int(event['seq']) for event in collected if str(event.get('seq', '')).isdigit()), default=None)
            pages = 0
            while has_more and cursor is not None and cursor > after and pages < 10:
                page = client.request('GET', '/chat/history', {**params, 'before_seq': cursor})
                if not isinstance(page, dict) or not isinstance(page.get('chat_events', []), list):
                    raise MuseError('muse_protocol', 'Muse 返回的历史格式不合法。')
                older_events = page['chat_events']
                next_cursor = min((int(event['seq']) for event in older_events if str(event.get('seq', '')).isdigit()), default=None)
                if not older_events or next_cursor is None or next_cursor >= cursor:
                    has_more = False
                    break
                collected = older_events + collected
                cursor, has_more = next_cursor, bool(page.get('has_more'))
                pages += 1
            if cursor is not None and cursor > after and pages == 10 and has_more:
                result['chat_events'] = collected
                result['has_more_after'] = True
                result['continuation_before_seq'] = cursor
                result['has_more_before'] = bool(result.get('has_more'))
                return result
            result['chat_events'] = [event for event in collected
                                     if str(event.get('seq', '')).isdigit() and int(event['seq']) > after]
            result['has_more_after'] = bool(has_more and cursor is not None and cursor > after)
            result['continuation_before_seq'] = cursor if result['has_more_after'] else None
            result['has_more_before'] = bool(result.get('has_more'))
            return result

    def _mutate(self, owner, request_id, action, values, operation, account_id=None, expected_revision=None,
                required_auto_session=None, expected_settings_revision=None, required_auto_approve_all=False):
        request_id = identifier(request_id)
        previous = self.repository.claim(owner, request_id, action, values, account_id, expected_revision,
                                         required_auto_session, expected_settings_revision, required_auto_approve_all)
        if previous is not None:
            return previous
        try:
            result = operation()
        except MuseError as exc:
            # Definitive server rejection is different from uncertain delivery.
            state = ('rejected' if exc.code in ('muse_rejected', 'muse_disconnected', 'muse_login_expired') else
                     'retryable' if exc.code in self.DEFINITELY_NOT_DISPATCHED else 'unknown')
            return self.repository.finish(owner, request_id, state, {'error': exc.code, 'detail': exc.detail},
                                          account_id, expected_revision)
        except Exception:
            # Never leave a durable claim in dispatching after an unexpected
            # application failure. Delivery remains indeterminate and must not
            # be replayed automatically.
            return self.repository.finish(owner, request_id, 'unknown', {'error': 'muse_internal'},
                                          account_id, expected_revision)
        return self.repository.finish(owner, request_id, 'confirmed', result or {}, account_id, expected_revision)

    def send(self, owner, session_id, text, request_id, reply_to=None, account_id=None, expected_revision=None):
        identifier(session_id)
        if not isinstance(text, str) or not text.strip() or len(text) > 30000:
            raise MuseError('invalid_message', '消息长度需在 1 到 30000 字符之间。', 400)
        values = {'message': text, 'session_id': session_id,
                  'capabilities': ['chat_cancel', 'delta_stream']}
        if reply_to:
            values['reply_to_message_id'] = identifier(reply_to)
        with self._lease(owner, account_id, expected_revision) as (account, client):
            account_id = account['account_id']
            values['node_id'] = str(uuid.uuid5(uuid.NAMESPACE_URL, 'agent-fleet:muse:' + owner + ':' + account_id))
            return self._mutate(owner, request_id, 'send', values,
                                lambda: client.request('POST', '/chat/stream', values, subscription=True), account_id, account['revision'])

    def cancel(self, owner, session_id, request_id, account_id=None, expected_revision=None):
        values = {'session_id': identifier(session_id)}
        with self._lease(owner, account_id, expected_revision) as (account, client):
            account_id = account['account_id']
            return self._mutate(owner, request_id, 'cancel', values,
                                lambda: client.request('POST', '/chat/cancel', values), account_id, account['revision'])

    def approvals(self, owner, account_id=None, expected_revision=None):
        with self._lease(owner, account_id, expected_revision) as (_, client):
            return client.request('GET', '/approvals', service=1)

    def resource(self, owner, path, account_id=None, expected_revision=None):
        if not isinstance(path, str) or len(path) > 4096:
            raise MuseError('invalid_resource', '文件路径不合法。', 400)
        path = path.removeprefix('sandbox://').lstrip('/')
        parts = [unquote(part) for part in path.split('/')]
        if (len(parts) < 2 or parts[0] != 'workspace' or
                any(part in ('', '.', '..') or any(c in part for c in '/\\\0%?#') for part in parts)):
            raise MuseError('invalid_resource', '只能读取 Muse workspace 中的文件。', 400)
        path = '/'.join(parts)
        with self._lease(owner, account_id, expected_revision) as (_, leased_client):
            return self._read_resource(leased_client, path, parts)

    @staticmethod
    def _read_resource(client, path, parts):
        content = bytearray()
        while len(content) <= 8 * 1024 * 1024:
            chunk = client.request('POST', '/fs/read', {'path': path, 'offset': len(content), 'len': 1024 * 1024})
            if (not isinstance(chunk, dict) or type(chunk.get('len')) is not int or
                    type(chunk.get('eof')) is not bool):
                raise MuseError('muse_resource', 'Muse 文件数据不完整。')
            try:
                raw = base64.b64decode(chunk['data_base64'], validate=True)
            except (KeyError, ValueError, TypeError):
                raise MuseError('muse_resource', 'Muse 文件数据不完整。') from None
            if len(raw) != chunk.get('len') or len(raw) > 1024 * 1024:
                raise MuseError('muse_resource', 'Muse 文件长度不匹配。')
            content.extend(raw)
            if len(content) > 8 * 1024 * 1024:
                break
            if chunk.get('eof'):
                mime = 'application/octet-stream'
                if content.startswith(b'\x89PNG\r\n\x1a\n'): mime = 'image/png'
                elif content.startswith(b'\xff\xd8\xff'): mime = 'image/jpeg'
                elif content[:6] in (b'GIF87a', b'GIF89a'): mime = 'image/gif'
                elif content[:4] == b'RIFF' and content[8:12] == b'WEBP': mime = 'image/webp'
                return {'filename': parts[-1], 'mime': mime, 'data_base64': base64.b64encode(content).decode()}
            if not raw:
                raise MuseError('muse_resource', 'Muse 文件读取没有继续推进。')
        raise MuseError('muse_resource_large', '当前支持查看和下载 8 MB 以内的文件。', 413)

    def decide(self, owner, approval_id, decision, revision, *, automatic=False, account_id=None, expected_revision=None,
               required_auto_session=None, expected_settings_revision=None, required_auto_approve_all=False):
        identifier(approval_id)
        if decision not in ('allow_once', 'deny') or type(revision) is not int or revision < 0:
            raise MuseError('invalid_decision', '审批决定或版本不合法。', 400)
        # One decision per remote approval revision, including across processes.
        request_id = 'approval:' + approval_id + ':' + str(revision)
        values = {'decision': decision, 'expected_message_revision': revision,
                  'notification_channel': 'ui', 'reason': 'Agent Fleet 自动审批' if automatic else 'Agent Fleet 手动审批'}
        with self._lease(owner, account_id, expected_revision) as (account, client):
            account_id = account['account_id']
            def dispatch():
                return self._mutate(
                    owner, request_id, 'auto_approve' if automatic else 'approval', values,
                    lambda: client.request('POST', '/approvals/' + approval_id + '/decide', values, service=1),
                    account_id, account['revision'], required_auto_session, expected_settings_revision,
                    required_auto_approve_all)
            if automatic:
                with self.owner_operation(owner) as allowed:
                    if allowed:
                        return dispatch()
                return None
            return dispatch()

    def settings(self, owner, sessions=None, account_id=None, expected_revision=None, expected_connection_revision=None,
                session_id=None, enabled=None, auto_approve_all=None):
        if session_id is not None:
            if sessions is not None or auto_approve_all is not None:
                raise MuseError('invalid_settings', '每次只能更新一种自动审批设置。', 400)
            session_id = identifier(session_id)
            if type(enabled) is not bool:
                raise MuseError('invalid_settings', '自动审批开关不合法。', 400)
            with self._lock(owner, account_id), self._lease(owner, account_id, expected_connection_revision) as (account, _):
                available = {s['session_id'] for s in self.sessions(owner, account['account_id'], account['revision']).get('sessions', [])}
                if session_id not in available:
                    raise MuseError('invalid_settings', '只能为当前 Muse 账号已有的会话开启自动审批。', 400)
                self.repository.update_session_setting(owner, account['account_id'], session_id, enabled,
                                                       expected_revision, account['revision'])
            self.start()
            return self.status(owner, account['account_id'])
        if auto_approve_all is not None:
            if type(auto_approve_all) is not bool or sessions is not None:
                raise MuseError('invalid_settings', '账号级自动审批开关不合法。', 400)
            with self._lock(owner, account_id), self._lease(owner, account_id, expected_connection_revision) as (account, _):
                self.repository.settings(owner, {'auto_approve_all': auto_approve_all}, account['account_id'],
                                         expected_revision, account['revision'])
            self.start()
            return self.status(owner, account['account_id'])
        if not isinstance(sessions, list) or len(sessions) > 20:
            raise MuseError('invalid_settings', '最多为 20 个会话开启自动审批。', 400)
        sessions = list(dict.fromkeys(identifier(v) for v in sessions))
        with self._lock(owner, account_id), self._lease(owner, account_id, expected_connection_revision) as (account, _):
            account_id = account['account_id']
            available = {s['session_id'] for s in self.sessions(owner, account_id, account['revision']).get('sessions', [])}
            if not set(sessions) <= available:
                raise MuseError('invalid_settings', '只能为当前 Muse 账号已有的会话开启自动审批。', 400)
            self.repository.settings(owner, {'auto_approve_sessions': sessions}, account_id, expected_revision,
                                     account['revision'])
        self.start()
        return self.status(owner, account_id)

    def auto_approve_once(self, owner, account_id=None):
        if not self.owner_enabled(owner):
            return
        account = self.repository.account(owner, account_id)
        settings = account['settings'] if account else {}
        approve_all = settings.get('auto_approve_all') is True
        scopes = set(settings.get('auto_approve_sessions', []))
        if not approve_all and not scopes:
            return
        approvals = self.approvals(owner, account['account_id'], account['revision'])
        for item in approvals.get('pending_approvals', approvals.get('pending', [])):
            scope = item.get('scope') or {}
            trace = scope.get('runtime_request_trace') or item.get('runtime_request_trace') or {}
            session_id = (item.get('session_id') or scope.get('session_id') or
                          scope.get('thread_id') or item.get('thread_id') or trace.get('thread_id'))
            # Taskless/background requests and unscoped confirmations stay
            # manual. A selected chat cannot grant permissions to another.
            if item.get('status') not in (None, 'pending'):
                continue
            with self._lock(owner, account['account_id']):
                current = self.repository.account(owner, account['account_id'])
                if not current or current['revision'] != account['revision']:
                    return
                current_settings = current['settings']
                scopes = set(current_settings.get('auto_approve_sessions', []))
                approve_all = current_settings.get('auto_approve_all') is True
                if not approve_all and session_id not in scopes:
                    continue
                if not self.owner_enabled(owner):
                    return
                revision = item.get('message_revision', 0)
                try:
                    self.decide(owner, item.get('approval_id'), 'allow_once', revision, automatic=True,
                                account_id=account['account_id'], expected_revision=account['revision'],
                                required_auto_session=None if approve_all else session_id,
                                expected_settings_revision=current['settings_revision'],
                                required_auto_approve_all=approve_all)
                except MuseError as exc:
                    if exc.code != 'muse_approval_revoked':
                        raise

    def start(self):
        with self.worker_lock:
            if self.worker is not None:
                return
            def run():
                while not self.stop_event.wait(5):
                    try:
                        accounts = self.repository.enabled_accounts()
                    except Exception:
                        continue
                    for entry in accounts:
                        try:
                            self.auto_approve_once(entry['owner'], entry['account_id'])
                        except Exception:
                            # No raw upstream exceptions/credentials in logs.
                            continue
            self.worker = threading.Thread(target=run, name='muse-approvals', daemon=True)
            self.worker.start()

    def close(self):
        self.stop_event.set()
        with self.worker_lock:
            worker = self.worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=6)
        with self.worker_lock:
            if self.worker is worker:
                self.worker = None
