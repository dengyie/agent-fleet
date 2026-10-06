"""TLS-only SMTP transport. Credentials come from runtime configuration."""
from email.message import EmailMessage
import smtplib
import ssl


class SmtpSender:
    def __init__(self, *, host, port=465, username='', password='', sender='', starttls=False):
        self.host, self.port = host, int(port)
        self.username, self.password, self.sender = username, password, sender
        self.starttls = starttls

    def __call__(self, email, purpose, code):
        message = EmailMessage()
        message['From'] = self.sender
        message['To'] = email
        message['Subject'] = 'Agent Fleet · ' + ('注册验证码' if purpose == 'register' else '密码重置验证码')
        message.set_content(f'你的验证码是：{code}\n\n10 分钟内有效，仅可使用一次。请勿向任何人透露。\n如果不是你本人操作，请忽略本邮件。')
        context = ssl.create_default_context()
        if self.starttls:
            client = smtplib.SMTP(self.host, self.port, timeout=10)
        else:
            client = smtplib.SMTP_SSL(self.host, self.port, timeout=10, context=context)
        with client:
            if self.starttls:
                client.starttls(context=context)
            if self.username:
                client.login(self.username, self.password)
            client.send_message(message)


class OneMailSender:
    """one-mail's address-scoped /api/send_mail API; no admin credential needed."""
    def __init__(self, *, origin, token, site_password='', from_name='Agent Fleet'):
        from urllib.parse import urlsplit
        parsed = urlsplit(origin)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise ValueError('one-mail requires an HTTPS origin without a path')
        if not token or any(c in token + site_password for c in '\r\n'):
            raise ValueError('one-mail requires a valid address JWT')
        self.host, self.port = parsed.hostname, parsed.port or 443
        self.token, self.site_password, self.from_name = token, site_password, from_name

    def __call__(self, email, purpose, code):
        import http.client
        import json
        import uuid
        payload = {'from_name': self.from_name, 'to_name': '', 'to_mail': email,
                   'subject': 'Agent Fleet · ' + ('注册验证码' if purpose == 'register' else '密码重置验证码'),
                   'is_html': False,
                   'content': f'你的验证码是：{code}\n\n10 分钟内有效，仅可使用一次。请勿向任何人透露。\n如果不是你本人操作，请忽略本邮件。'}
        headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.token,
                   'x-idempotency-key': uuid.uuid4().hex}
        if self.site_password:
            headers['x-custom-auth'] = self.site_password
        connection = http.client.HTTPSConnection(self.host, self.port, timeout=10, context=ssl.create_default_context())
        try:
            connection.request('POST', '/api/send_mail', body=json.dumps(payload).encode('utf-8'), headers=headers)
            response = connection.getresponse()
            # Never follow redirects carrying credentials, auto-retry an uncertain
            # delivery, log provider bodies, or accept an HTML login page as success.
            if response.status != 200:
                raise RuntimeError('one-mail did not confirm delivery')
            raw = response.read(8193)
            if len(raw) > 8192:
                raise RuntimeError('one-mail response exceeds limit')
            result = json.loads(raw)
            if not isinstance(result, dict) or result.get('status') != 'ok':
                raise RuntimeError('one-mail did not confirm delivery')
        finally:
            connection.close()
