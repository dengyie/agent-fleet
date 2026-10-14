import json
import pytest
from hub.integrations.muse.protocol import field, decode, Cipher, NoiseXX, FrameDecoder, ProtocolError
from hub.integrations.muse.client import normalize_login, MuseError


def test_protobuf_and_cipher_reject_corruption():
    payload = field(1, 123) + field(2, b'hello')
    assert decode(payload) == {1: [123], 2: [b'hello']}
    for bad in [b'\x80', b'\x12\x05x', b'\x00', b'\xff' * 12]:
        with pytest.raises(ProtocolError): decode(bad)
    a, b = Cipher(b'x' * 32), Cipher(b'x' * 32)
    assert b.decrypt(a.encrypt(b'hello')) == b'hello'
    damaged = bytearray(a.encrypt(b'next')); damaged[-1] ^= 1
    with pytest.raises(ProtocolError): b.decrypt(bytes(damaged))
    with pytest.raises(ProtocolError): b.decrypt(b'anything')


def test_frames_order_and_limits():
    d = FrameDecoder()
    assert d.feed(field(1, 5) + field(2, 1) + field(3, 2) + field(4, b'b')) is None
    assert d.feed(field(1, 5) + field(2, 0) + field(3, 2) + field(4, b'a')) == b'ab'
    with pytest.raises(ProtocolError): d.feed(field(3, 300))


def test_login_import_is_scoped_and_header_safe():
    valid = {'name': 'hatch_sess', 'value': 'example', 'domain': '.muse.ai'}
    assert normalize_login({'cookies': [valid]})[0]['name'] == 'hatch_sess'
    assert normalize_login([valid, {**valid, 'domain': '.example.com'}]) == normalize_login([valid])
    for item in [{**valid, 'value': 'x\r\nInjected: yes'}, {**valid, 'domain': 'evilmuse.ai'}, {**valid, 'expires': 1}]:
        with pytest.raises(MuseError): normalize_login([item])
    with pytest.raises(MuseError, match='有效期'):
        normalize_login([{**valid, 'expires': 10 ** 10000}])


def responder_handshake(first, challenge=None):
    """Independent responder using the published XX token ordering."""
    import hashlib, hmac
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    def pub(k): return k.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    def kdf(key, data):
        temp=hmac.digest(key,data,'sha256'); a=hmac.digest(temp,b'\x01','sha256')
        return a,hmac.digest(temp,a+b'\x02','sha256')
    ck=b'Noise_XX_25519_AESGCM_SHA256'.ljust(32,b'\0')
    hh=hashlib.sha256(ck).digest()
    hh=hashlib.sha256(hh+first[:32]).digest(); hh=hashlib.sha256(hh+first[32:]).digest()
    eph,static=X25519PrivateKey.generate(),X25519PrivateKey.generate()
    hh=hashlib.sha256(hh+pub(eph)).digest(); ck,key=kdf(ck,eph.exchange(X25519PublicKey.from_public_bytes(first[:32])))
    enc_static=AESGCM(key).encrypt(bytes(12),pub(static),hh);hh=hashlib.sha256(hh+enc_static).digest()
    ck,key=kdf(ck,static.exchange(X25519PublicKey.from_public_bytes(first[:32])))
    payload=challenge if challenge is not None else b'standard VM attestation'
    enc_payload=AESGCM(key).encrypt(bytes(12),payload,hh);hh=hashlib.sha256(hh+enc_payload).digest()
    def finish(third):
        nonlocal hh,ck,key
        remote=AESGCM(key).decrypt((1).to_bytes(12,'big'),third[:48],hh);hh=hashlib.sha256(hh+third[:48]).digest()
        ck,key=kdf(ck,eph.exchange(X25519PublicKey.from_public_bytes(remote)))
        assert AESGCM(key).decrypt(bytes(12),third[48:],hh)==b''
        return kdf(ck,b'')
    return pub(eph)+enc_static+enc_payload, finish


def test_noise_handshake_interoperates_and_confidential_challenge_rejected():
    noise=NoiseXX(); second,finish=responder_handshake(noise.first())
    third,send,receive=noise.finish(second)
    a,b=finish(third)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    assert AESGCM(a).decrypt(bytes(12),send.encrypt(b'request'),b'')==b'request'
    assert receive.decrypt(AESGCM(b).encrypt(bytes(12),b'response',b''))==b'response'
    with pytest.raises(ProtocolError): noise.first()
    noise=NoiseXX(); second,_=responder_handshake(noise.first(),field(1,b'attestation')+field(2,2)+field(3,b'x'*32)+field(4,b'y'*32))
    with pytest.raises(ProtocolError,match='confidential'):noise.finish(second)


def test_full_native_request_without_browser_handles_chunked_json_and_ack():
    from hub.integrations.muse.client import MuseClient
    from hub.integrations.muse.protocol import one
    class Socket:
        def __init__(self):self.phase=0;self.queue=[];self.response_cipher=None;self.closed=False
        def send_binary(self,data):
            if self.phase==0:
                second,self.finish=responder_handshake(data);self.queue.append(second)
            elif self.phase==1:
                a,b=self.finish(data);self.request_cipher=Cipher(a);self.response_cipher=Cipher(b)
            else:
                decoded=FrameDecoder().feed(self.request_cipher.decrypt(data))
                envelope=decode(decoded);frame=decode(one(envelope,2));request=decode(one(frame,2))
                assert one(request,1)==b'GET' and one(request,2)==b'/api/session/list'
                response=field(1,200)
                chunk=field(1,b'{"ok":true,"result":{"sessions":[]}}')+field(2,1)
                for n,payload in [(3,response),(4,chunk)]:
                    service=field(1,field(1,1)+field(n,payload))
                    self.queue.append(self.response_cipher.encrypt(field(1,n)+field(3,1)+field(4,service)))
            self.phase+=1
        def recv(self):return self.queue.pop(0)
        def settimeout(self,timeout):pass
        def close(self,**kwargs):self.closed=True
    socket=Socket()
    client=MuseClient([{'name':'hatch_sess','value':'secret','domain':'muse.ai'}],connector=lambda *a,**k:socket)
    client.credentials=lambda:('vm',{'token':'secret'})
    assert client.request('GET','/api/session/list')=={'sessions':[]}
    assert socket.closed
    assert client._unwrap({'type':'res','status':'ok','payload':{'message_id':'m'}},200)=={'message_id':'m'}
    with pytest.raises(MuseError):client._unwrap({'ok':False,'error':'secret'},409)


def test_transport_exception_never_exposes_credential_url():
    from hub.integrations.muse.client import MuseClient
    def failed(*a,**k):raise OSError('wss://host?auth_token=TOPSECRET')
    client=MuseClient('hatch_sess=TOPSECRET',connector=failed)
    client.credentials=lambda:('vm',{'token':'TOPSECRET'})
    with pytest.raises(MuseError) as error:client.request('GET','/api/session/list')
    assert 'TOPSECRET' not in str(error.value)


def test_auth_redirects_are_blocked_and_fetch_metadata_is_sent(monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from hub.integrations.muse import client as module
    calls = []
    class Auth(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            calls.append((self.path, dict(self.headers)))
            self.send_response(302)
            self.send_header('Location', '/redirected')
            self.end_headers()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Auth)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    origin = 'http://127.0.0.1:' + str(server.server_port)
    monkeypatch.setattr(module, 'ORIGIN', origin)
    try:
        with pytest.raises(MuseError): module.MuseClient('hatch_sess=fixture')._http('/api/session')
        assert len(calls) == 1
        headers = {k.lower():v for k,v in calls[0][1].items()}
        assert headers['cookie'] == 'hatch_sess=fixture'
        assert headers['sec-fetch-site'] == 'same-origin' and headers['origin'] == origin
    finally:
        server.shutdown(); server.server_close(); thread.join(5)


def test_auth_rejects_non_object_json_without_leaking_into_route_500(monkeypatch):
    from hub.integrations.muse import client as module

    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, limit): return b'[]'

    monkeypatch.setattr(module.urllib.request, 'build_opener', lambda *args: type(
        'Opener', (), {'open': lambda self, request, timeout: Response()})())
    with pytest.raises(MuseError, match='格式'):
        module.MuseClient('hatch_sess=fixture')._http('/api/session')


@pytest.mark.parametrize('session', [
    {'status': 'assigned', 'endpoint_url': 4, 'vm_id': 'vm', 'vm_name': 'name'},
    {'status': 'assigned', 'endpoint_url': 'wss://[invalid', 'vm_id': 'vm', 'vm_name': 'name'},
    {'status': 'assigned', 'endpoint_url': 'wss://vm.metaaivm.com', 'vm_id': [], 'vm_name': 'name'},
])
def test_credentials_reject_malformed_auth_session_as_protocol_error(monkeypatch, session):
    from hub.integrations.muse.client import MuseClient
    client = MuseClient('hatch_sess=fixture')
    monkeypatch.setattr(client, '_http', lambda *args, **kwargs: session)
    with pytest.raises(MuseError) as error:
        client.credentials()
    assert error.value.code == 'muse_protocol'


def test_gateway_uses_fixed_destination_without_cookies_or_redirects():
    from urllib.parse import urlparse
    from hub.integrations.muse.client import MuseClient
    connections = []
    def connect(url, **kwargs):
        connections.append((url, kwargs))
        raise OSError('fixture stop')
    client = MuseClient('hatch_sess=fixture', connector=connect)
    client.credentials = lambda: ('vm', {'token':'fixture-token'})
    with pytest.raises(MuseError): client.request('GET','/api/session/list')
    assert len(connections) == 1
    url, kwargs = connections[0]
    assert urlparse(url).hostname == 'hatch.metaaivm.com'
    assert kwargs['redirect_limit'] == 0
    assert 'cookie' not in kwargs and 'header' not in kwargs
