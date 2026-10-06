"""Real loopback SMTP/TLS handshake, authentication, delivery and rejection."""
import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
import ipaddress
import socketserver
import ssl
from threading import Thread

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from hub.accounts.mail import SmtpSender


@contextmanager
def smtp_server(tmp_path, *, starttls=False, reject=False):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / 'smtp-cert.pem', tmp_path / 'smtp-key.pem'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(cert_path, key_path)
    state = {'messages': [], 'auth': [], 'tls': [], 'deliveries': 0, 'cert': str(cert_path)}

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(5)
            secure = not starttls
            def send(line):
                self.wfile.write(line.encode() + b'\r\n'); self.wfile.flush()
            send('220 localhost test SMTP')
            while line := self.rfile.readline():
                verb = line.decode().split()[0].upper()
                if verb in ('EHLO', 'HELO'):
                    send('250-localhost'); send('250-AUTH PLAIN'); send('250 STARTTLS')
                elif verb == 'STARTTLS':
                    send('220 Ready to start TLS')
                    self.rfile.close(); self.wfile.close()
                    self.connection = context.wrap_socket(self.connection, server_side=True)
                    self.rfile = self.connection.makefile('rb'); self.wfile = self.connection.makefile('wb')
                    secure = True
                elif verb == 'AUTH':
                    state['tls'].append(secure)
                    auth = base64.b64decode(line.split()[2])
                    state['auth'].append(auth == b'\x00test-user\x00test-password')
                    send('235 Authentication successful' if state['auth'][-1] and secure else '535 Authentication failed')
                elif verb in ('MAIL', 'RCPT', 'RSET'):
                    send('250 OK')
                elif verb == 'DATA':
                    send('354 End with dot')
                    parts = []
                    while (data := self.rfile.readline()) not in (b'.\r\n', b''):
                        parts.append(data)
                    state['deliveries'] += 1
                    if reject:
                        send('451 Temporary delivery failure')
                    else:
                        state['messages'].append(BytesParser(policy=policy.default).parsebytes(b''.join(parts)))
                        send('250 Accepted')
                elif verb == 'QUIT':
                    send('221 Bye'); break
                else:
                    send('500 Unsupported')

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        def get_request(self):
            sock, address = super().get_request()
            sock.settimeout(5)
            if not starttls:
                try:
                    sock = context.wrap_socket(sock, server_side=True)
                except Exception:
                    sock.close(); raise
            return sock, address

    server = Server(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    state['port'] = server.server_address[1]
    try:
        yield state
    finally:
        server.shutdown(); server.server_close(); thread.join(5)
        assert not thread.is_alive()


@pytest.mark.parametrize('starttls', [False, True])
def test_smtp_real_tls_authentication_and_mime_delivery(tmp_path, monkeypatch, starttls):
    with smtp_server(tmp_path, starttls=starttls) as server:
        monkeypatch.setenv('SSL_CERT_FILE', server['cert'])
        sender = SmtpSender(host='127.0.0.1', port=server['port'], username='test-user',
                            password='test-password', sender='sender@example.test', starttls=starttls)
        sender('recipient@example.test', 'register', '123456')
        assert server['tls'] == [True] and server['auth'] == [True]
        assert server['deliveries'] == 1
        message = server['messages'][0]
        assert message['To'] == 'recipient@example.test'
        assert message['From'] == 'sender@example.test'
        assert '注册验证码' in message['Subject']
        assert '123456' in message.get_content()


def test_smtp_rejection_is_not_success_or_automatically_retried(tmp_path, monkeypatch):
    import smtplib
    with smtp_server(tmp_path, reject=True) as server:
        monkeypatch.setenv('SSL_CERT_FILE', server['cert'])
        sender = SmtpSender(host='127.0.0.1', port=server['port'], sender='sender@example.test')
        with pytest.raises(smtplib.SMTPDataError):
            sender('recipient@example.test', 'reset', '123456')
        assert server['deliveries'] == 1 and server['messages'] == []


def test_smtp_untrusted_certificate_is_rejected_before_delivery(tmp_path, monkeypatch):
    monkeypatch.delenv('SSL_CERT_FILE', raising=False)
    with smtp_server(tmp_path) as server:
        sender = SmtpSender(host='127.0.0.1', port=server['port'], sender='sender@example.test')
        with pytest.raises(ssl.SSLCertVerificationError):
            sender('recipient@example.test', 'reset', '123456')
        assert server['deliveries'] == 0
