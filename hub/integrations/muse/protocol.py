"""Bounded Muse Noise XX transport (observed web protocol, October 2026).

Wire format: AES-GCM(NoiseTransportFrame(ServiceRequest(ServiceFrame(HTTP)))).
This module implements the standard XX primitives with cryptography; it does
not load or execute Muse's frontend bundle. Confidential VM enrollment is not
supported: a recovery-key challenge is explicitly rejected.
"""
import hashlib
import hmac
import os
import time

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

MAX_BODY = 8 * 1024 * 1024


class ProtocolError(Exception):
    pass


def varint(value):
    value &= (1 << 64) - 1
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def field(number, value):
    if isinstance(value, str):
        value = value.encode('utf-8')
    if isinstance(value, bytes):
        return varint(number * 8 + 2) + varint(len(value)) + value
    return varint(number * 8) + varint(value)


def decode(data):
    if not isinstance(data, bytes) or len(data) > MAX_BODY:
        raise ProtocolError('invalid frame size')
    offset = 0
    result = {}

    def read_varint():
        nonlocal offset
        value = 0
        for shift in range(0, 70, 7):
            if offset >= len(data):
                raise ProtocolError('truncated varint')
            byte = data[offset]
            offset += 1
            if shift == 63 and byte > 1:
                raise ProtocolError('varint overflow')
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise ProtocolError('invalid varint')

    while offset < len(data):
        tag = read_varint()
        number, wire = tag >> 3, tag & 7
        if not number:
            raise ProtocolError('invalid field')
        if wire == 0:
            value = read_varint()
        elif wire in (1, 2, 5):
            size = read_varint() if wire == 2 else (8 if wire == 1 else 4)
            if size > len(data) - offset:
                raise ProtocolError('truncated field')
            value = data[offset:offset + size]
            offset += size
        else:
            raise ProtocolError('unsupported wire type')
        result.setdefault(number, []).append(value)
    return result


def one(fields, key, default=None):
    values = fields.get(key, [])
    if len(values) > 1:
        raise ProtocolError('duplicate field')
    return values[0] if values else default


def hkdf(chaining_key, material):
    temporary = hmac.digest(chaining_key, material, 'sha256')
    first = hmac.digest(temporary, b'\x01', 'sha256')
    return first, hmac.digest(temporary, first + b'\x02', 'sha256')


class Cipher:
    def __init__(self, key=None):
        self.key, self.nonce, self.dead = key, 0, False

    def _apply(self, data, associated, decrypt):
        if self.dead or self.nonce >= (1 << 53) - 1:
            raise ProtocolError('cipher unavailable')
        if self.key is None:
            return data
        nonce = self.nonce.to_bytes(12, 'big')
        self.nonce += 1
        try:
            cipher = AESGCM(self.key)
            return (cipher.decrypt if decrypt else cipher.encrypt)(nonce, data, associated)
        except Exception:
            self.dead = True
            raise ProtocolError('cipher authentication failed') from None

    def encrypt(self, data, associated=b''):
        return self._apply(data, associated, False)

    def decrypt(self, data, associated=b''):
        return self._apply(data, associated, True)


def public(key):
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


class NoiseXX:
    def __init__(self):
        self.hash = b'Noise_XX_25519_AESGCM_SHA256'.ljust(32, b'\0')
        self.chaining_key = self.hash
        self._mix_hash(b'')
        self.cipher, self.phase = Cipher(), 0
        self.ephemeral = X25519PrivateKey.generate()

    def _mix_hash(self, value):
        self.hash = hashlib.sha256(self.hash + value).digest()

    def _mix_key(self, value):
        self.chaining_key, key = hkdf(self.chaining_key, value)
        self.cipher = Cipher(key)

    def _crypt(self, value, decrypt=False):
        output = (self.cipher.decrypt if decrypt else self.cipher.encrypt)(value, self.hash)
        self._mix_hash(value if decrypt else output)
        return output

    def first(self):
        if self.phase != 0:
            raise ProtocolError('invalid handshake phase')
        self.phase = 1
        key = public(self.ephemeral)
        self._mix_hash(key)
        return key + self._crypt(field(1, os.urandom(32)))

    def finish(self, message):
        if self.phase != 1 or not isinstance(message, bytes) or not 96 <= len(message) <= 65535:
            raise ProtocolError('invalid handshake response')
        self.phase = 2
        try:
            remote_ephemeral = message[:32]
            self._mix_hash(remote_ephemeral)
            self._mix_key(self.ephemeral.exchange(X25519PublicKey.from_public_bytes(remote_ephemeral)))
            remote_static = self._crypt(message[32:80], True)
            self._mix_key(self.ephemeral.exchange(X25519PublicKey.from_public_bytes(remote_static)))
            attestation = self._crypt(message[80:], True)
            # Standard VMs have no recovery-key challenge. Never answer a
            # confidential VM challenge with an empty credential payload.
            try:
                challenge = decode(attestation)
            except ProtocolError:
                challenge = {}
            if one(challenge, 2) in (1, 2) or 5 in challenge:
                raise ProtocolError('confidential_vm_requires_recovery_key')
            static = X25519PrivateKey.generate()
            third = self._crypt(public(static))
            self._mix_key(static.exchange(X25519PublicKey.from_public_bytes(remote_ephemeral)))
            third += self._crypt(b'')
            send_key, receive_key = hkdf(self.chaining_key, b'')
            return third, Cipher(send_key), Cipher(receive_key)
        except ProtocolError:
            raise
        except Exception:
            raise ProtocolError('invalid handshake key') from None
        finally:
            self.ephemeral = None


class FrameDecoder:
    def __init__(self):
        self.pending = {}

    def feed(self, data):
        values = decode(data)
        identity, index, total, payload = (one(values, 1, 0), one(values, 2, 0),
                                          one(values, 3, 1), one(values, 4, b''))
        if not isinstance(total, int) or not isinstance(index, int) or not 1 <= total <= 256 or not 0 <= index < total:
            raise ProtocolError('invalid chunk index')
        if not isinstance(payload, bytes) or len(payload) > 65489:
            raise ProtocolError('invalid chunk size')
        now = time.monotonic()
        self.pending = {k: v for k, v in self.pending.items() if now - v[0] < 60}
        if identity not in self.pending:
            if len(self.pending) >= 16:
                raise ProtocolError('too many partial frames')
            self.pending[identity] = (now, total, {})
        _, expected, chunks = self.pending[identity]
        if expected != total or index in chunks:
            raise ProtocolError('inconsistent chunks')
        chunks[index] = payload
        if sum(sum(map(len, item[2].values())) for item in self.pending.values()) > MAX_BODY:
            raise ProtocolError('frame too large')
        if len(chunks) == total:
            del self.pending[identity]
            return b''.join(chunks[k] for k in range(total))
        return None


def request_frames(verb, path, body, service, cipher):
    headers = b''.join(field(3, field(1, k) + field(2, v)) for k, v in (
        ('x-app-id', 'hatch-web'), ('Content-Type', 'application/json'),
        ('Accept-Language', 'zh-CN'), ('Accept-Encoding', 'identity')))
    request = field(1, verb) + field(2, path) + headers + field(4, body) + field(5, 1)
    envelope = field(1, service) + field(2, field(1, 1) + field(2, request))
    if len(envelope) > MAX_BODY:
        raise ProtocolError('request too large')
    identity = int.from_bytes(os.urandom(8), 'big')
    total = max(1, (len(envelope) + 65488) // 65489)
    return [cipher.encrypt(field(1, identity) + field(2, i) + field(3, total) +
                           field(4, envelope[i * 65489:(i + 1) * 65489])) for i in range(total)]
