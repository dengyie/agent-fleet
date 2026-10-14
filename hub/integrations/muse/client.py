"""Fixed-origin HTTPS authentication and native Muse encrypted gateway client."""
import json
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .protocol import MAX_BODY, FrameDecoder, NoiseXX, ProtocolError, decode, one, request_frames

ORIGIN = 'https://muse.ai'
GATEWAY = 'wss://hatch.metaaivm.com/v1/noise'


class MuseError(Exception):
    def __init__(self, code, detail, status=502):
        super().__init__(detail)
        self.code, self.detail, self.status = code, detail, status


def normalize_login(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            # A Cookie header is also portable between browsers. Do not allow
            # arbitrary request headers or a caller-provided auth destination.
            value = [{'name': part.strip().split('=', 1)[0],
                      'value': part.strip().split('=', 1)[1], 'domain': 'muse.ai'}
                     for part in value.split(';') if '=' in part]
    if isinstance(value, dict):
        value = value.get('cookies')
    if not isinstance(value, list) or len(value) > 200:
        raise MuseError('invalid_login', '请导入 Muse 的 Cookie JSON 或登录态文件。', 400)
    result = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        domain = item.get('domain', '')
        if not isinstance(domain, str) or domain.lstrip('.') != 'muse.ai':
            continue
        name, secret = item.get('name'), item.get('value')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_!#$%&*+.^`|~-]{1,128}', name):
            raise MuseError('invalid_login', 'Cookie 名称不合法。', 400)
        if not isinstance(secret, str) or len(secret) > 16384 or any(ord(c) < 33 or ord(c) > 126 or c == ';' for c in secret):
            raise MuseError('invalid_login', 'Cookie 内容不合法。', 400)
        expires = item.get('expires', item.get('expirationDate', -1))
        if type(expires) not in (int, float):
            raise MuseError('invalid_login', 'Cookie 有效期不合法。', 400)
        try:
            expires = float(expires)
        except (OverflowError, ValueError):
            raise MuseError('invalid_login', 'Cookie 有效期不合法。', 400) from None
        if not math.isfinite(expires):
            raise MuseError('invalid_login', 'Cookie 有效期不合法。', 400)
        if expires > 0 and expires <= time.time():
            continue
        result[name] = {'name': name, 'value': secret, 'domain': 'muse.ai', 'expires': expires}
    if not result.get('hatch_sess', {}).get('value'):
        raise MuseError('invalid_login', '缺少有效的 Muse 登录 Cookie（hatch_sess）。', 400)
    return list(result.values())


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class MuseClient:
    def __init__(self, cookies, *, timeout=20, connector=None):
        self.cookies = normalize_login(cookies)
        self.timeout = timeout
        self.connector = connector
        self._credentials = None
        self._lock = threading.Lock()

    def _http(self, path, body=None):
        headers = {'Cookie': '; '.join(c['name'] + '=' + c['value'] for c in self.cookies),
                   'Origin': ORIGIN, 'Referer': ORIGIN + '/', 'Sec-Fetch-Site': 'same-origin',
                   'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-Dest': 'empty', 'Content-Type': 'application/json'}
        req = urllib.request.Request(ORIGIN + path, headers=headers,
                                     data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=self.timeout) as response:
                payload = response.read(1024 * 1024 + 1)
                if len(payload) > 1024 * 1024:
                    raise MuseError('muse_protocol', 'Muse 鉴权响应过大。')
                value = json.loads(payload)
                if not isinstance(value, dict):
                    raise MuseError('muse_protocol', 'Muse 鉴权响应格式不合法。')
                return value
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                # 401 belongs to Fleet operator authentication. Keep this as a
                # domain error so a Muse expiry cannot log the user out of Fleet.
                raise MuseError('muse_login_expired', 'Muse 登录态已过期或需要在官网完成验证，请重新导入。', 409) from None
            raise MuseError('muse_auth_unavailable', 'Muse 鉴权暂时不可用。') from None
        except (OSError, ValueError):
            raise MuseError('muse_auth_unavailable', '无法连接 Muse 鉴权服务。') from None

    def credentials(self):
        with self._lock:
            if self._credentials and self._credentials[0] > time.monotonic():
                return self._credentials[1:]
            session = self._http('/api/session?vm_state=live')
            if session.get('status') != 'assigned':
                raise MuseError('muse_not_ready', 'Muse 尚未分配运行环境，请先在官网完成初始化。', 409)
            endpoint, vm_id = session.get('endpoint_url'), session.get('vm_id')
            if (not isinstance(endpoint, str) or len(endpoint) > 2048 or
                    not isinstance(vm_id, str) or not isinstance(session.get('vm_name'), str)):
                raise MuseError('muse_protocol', 'Muse 返回了无法识别的运行环境。')
            try:
                target = urllib.parse.urlparse(endpoint)
                hostname = target.hostname
            except ValueError:
                raise MuseError('muse_protocol', 'Muse 返回了无法识别的运行环境。') from None
            if (target.scheme != 'wss' or not target.hostname or
                    not hostname.endswith('.metaaivm.com') or target.username or target.password or
                    not re.fullmatch(r'[A-Za-z0-9._-]{1,255}', vm_id)):
                raise MuseError('muse_protocol', 'Muse 返回了无法识别的运行环境。')
            token = self._http('/api/hatch/token', {'vmAddress': endpoint, 'vmName': session.get('vm_name')})
            if (not isinstance(token, dict) or not isinstance(token.get('token'), str) or
                    not token['token'] or len(token['token']) > 8192 or
                    ('notary_token' in token and not isinstance(token['notary_token'], str))):
                raise MuseError('muse_protocol', 'Muse 没有返回网关凭据。')
            if str(token.get('notary_token', '')).startswith('delegation.'):
                raise MuseError('muse_confidential_vm', '此 Muse 机密环境还需要恢复密钥，当前登录态导入不支持。', 409)
            self._credentials = (time.monotonic() + 240, vm_id, token)
            return vm_id, token

    def request(self, method, path, params=None, *, service=0, subscription=False):
        """Execute one bounded operation. Never automatically replay a write."""
        if method not in ('GET', 'POST', 'PATCH') or not path.startswith('/') or '://' in path:
            raise ValueError('invalid gateway operation')
        params = params or {}
        body = b''
        if method == 'GET' and params:
            path += '?' + urllib.parse.urlencode({k: str(v).lower() if isinstance(v, bool) else v for k, v in params.items() if v is not None})
        elif method != 'GET':
            body = json.dumps(params, ensure_ascii=False, separators=(',', ':')).encode()
        vm_id, token = self.credentials()
        query = {'vm_id': vm_id, 'auth_token': token['token'], 'app_id': 'hatch-web', 'request_id': str(uuid.uuid4())}
        if token.get('notary_token'):
            query['notary_token'] = token['notary_token']
        ws = None
        try:
            if self.connector is None:
                import websocket
                connect = websocket.create_connection
            else:
                connect = self.connector
            ws = connect(GATEWAY + '?' + urllib.parse.urlencode(query), origin=ORIGIN,
                         timeout=self.timeout, redirect_limit=0)
            handshake = NoiseXX()
            ws.send_binary(handshake.first())
            third, send, receive = handshake.finish(ws.recv())
            ws.send_binary(third)
            for frame in request_frames(method, path, body, service, send):
                ws.send_binary(frame)
            return self._response(ws, receive, subscription)
        except MuseError:
            raise
        except ProtocolError as exc:
            if str(exc) == 'confidential_vm_requires_recovery_key':
                raise MuseError('muse_confidential_vm', '此 Muse 机密环境需要恢复密钥，当前版本不支持。', 409) from None
            raise MuseError('muse_protocol', 'Muse 加密协议或响应格式不兼容。') from None
        except ImportError:
            raise MuseError('muse_dependency', '请安装 requirements.txt 中的 websocket-client。', 503) from None
        except Exception:
            # Library exceptions can contain the credential-bearing WS URL.
            self._credentials = None
            raise MuseError('muse_transport', 'Muse 连接中断；发送结果不确定时请先检查会话，避免重复发送。') from None
        finally:
            if ws is not None:
                try:
                    ws.close(timeout=1)
                except Exception:
                    pass

    def _response(self, ws, receive, subscription):
        decoder, content, status = FrameDecoder(), bytearray(), None
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            ws.settimeout(max(.1, deadline - time.monotonic()))
            encrypted = ws.recv()
            if not isinstance(encrypted, bytes) or len(encrypted) > 65535:
                raise ProtocolError('invalid websocket frame')
            assembled = decoder.feed(receive.decrypt(encrypted))
            if assembled is None:
                continue
            frame = decode(one(decode(assembled), 1, b''))
            if one(frame, 1) != 1:
                raise ProtocolError('unexpected stream')
            if 5 in frame:
                raise MuseError('muse_stream_reset', 'Muse 已中断该请求。')
            if 3 in frame:
                response = decode(one(frame, 3))
                status = one(response, 1, 0)
                for raw in response.get(2, []):
                    header = decode(raw)
                    if one(header, 1, b'').lower() == b'content-encoding' and one(header, 2) not in (b'identity', b''):
                        raise ProtocolError('unexpected compression')
                content.extend(one(response, 3, b''))
                ended = one(response, 4, 0)
            elif 4 in frame and status is not None:
                chunk = decode(one(frame, 4))
                content.extend(one(chunk, 1, b''))
                ended = one(chunk, 2, 0)
            else:
                raise ProtocolError('unexpected response')
            if len(content) > MAX_BODY:
                raise ProtocolError('response too large')
            if subscription and 200 <= status < 300 and content.strip():
                first = bytes(content).split(b'\n', 1)[0]
                try:
                    value = json.loads(first)
                except ValueError:
                    if ended or b'\n' in content:
                        raise ProtocolError('invalid stream acknowledgement') from None
                else:
                    return self._unwrap(value, status)
            if ended:
                try:
                    value = json.loads(content or b'{}')
                except ValueError:
                    raise ProtocolError('invalid json') from None
                return self._unwrap(value, status)
        raise MuseError('muse_timeout', 'Muse 响应超时。')

    @staticmethod
    def _unwrap(value, status):
        if not isinstance(value, dict):
            raise ProtocolError('invalid response envelope')
        if not 200 <= status < 300 or value.get('ok') is False or value.get('status') == 'err' or 'error' in value:
            raise MuseError('muse_rejected', 'Muse 拒绝了此次操作，请检查会话状态或在官网查看详情。', 409)
        if value.get('ok') is True:
            return value.get('result', value)
        if value.get('type') == 'res' and value.get('status') == 'ok':
            return value.get('payload', {})
        return value
