"""Account HTTP endpoints and fail-closed role boundary for account mode."""
import os
import time
from urllib.parse import urlsplit

from flask import Blueprint, current_app, g, jsonify, request
from hub.accounts.store import AccountStore, email_address, login_identifier, fail
from hub.accounts.client_address import client_address, proxy_networks
from hub.accounts.mail import OneMailSender, SmtpSender
from hub.accounts.captcha import verify_hcaptcha, verify_turnstile
from hub.http.errors import ApplicationError, error_response
from hub.http._common import request_json
from hub.auth import is_machine_auth_view

bp = Blueprint('accounts', __name__, url_prefix='/api/accounts')
def cookie_name():
    # Host-bound production cookies cannot be planted by sibling subdomains.
    return '__Host-fleet_session' if current_app.config['ACCOUNT_COOKIE_SECURE'] else 'fleet_session'
PUBLIC = {'accounts.options', 'accounts.code', 'accounts.register', 'accounts.login', 'accounts.reset'}
# These handlers already enforce their own distinct machine credential domain.
MACHINE_BLUEPRINTS = {'commands', 'platform_nodes'}
# Only audited owner-scoped surfaces are available to ordinary accounts.
USER_BLUEPRINTS = {'accounts', 'operator', 'platform_conversations', 'platform_artifacts', 'platform_muse'}


def store():
    return current_app.extensions['accounts']


def body():
    data = request_json()
    if not isinstance(data, dict):
        fail('invalid_json', '请求体必须是 JSON 对象')
    return data


#: 流式响应的会话复核间隔（秒）；可用 app.config['ACCOUNT_STREAM_REVALIDATE_S'] 覆盖。
STREAM_REVALIDATE_S = 30.0
_IDENTITY_CACHE = '_account_identity_cache'


def account_identity(req):
    if any(req.headers.get(header) for header in (
        'X-Agent-Fleet-Token', 'X-Runner-Credential', 'X-Supervisor-Credential', 'X-Platform-Node-Credential')):
        return None
    token = req.cookies.get(cookie_name(), '')
    # 每个请求只查一次库：boundary 与 operator 解析共用 flask.g 缓存（按 token 键控）。
    cached = g.get(_IDENTITY_CACHE)
    if cached is not None and cached[0] == token:
        return cached[1]
    user = store().identity(token)
    setattr(g, _IDENTITY_CACHE, (token, user))
    return user


def cookie(response, token='', *, clear=False):
    response.set_cookie(cookie_name(), token, max_age=0 if clear else 604800,
                        secure=current_app.config['ACCOUNT_COOKIE_SECURE'], httponly=True,
                        samesite='Lax', path='/')
    return response


def init_accounts(app, config):
    settings = dict(config.extra.get('accounts') or {})
    enabled = settings.get('enabled', os.environ.get('AGENT_FLEET_ACCOUNTS_ENABLED') == '1')
    if not enabled:
        return
    origin = settings.get('origin') or os.environ.get('AGENT_FLEET_ACCOUNT_ORIGIN', '')
    parsed = urlsplit(origin)
    if not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username or (
            parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1'))):
        raise ValueError('account mode requires an HTTPS origin (HTTP allowed only on loopback)')
    registration = settings.get('registration') or os.environ.get('AGENT_FLEET_REGISTRATION', 'invite')
    if registration not in ('invite', 'open'):
        raise ValueError('registration must be invite or open')
    trusted_proxies = proxy_networks(settings.get('trusted_proxies',
        os.environ.get('AGENT_FLEET_ACCOUNT_TRUSTED_PROXIES', '')))
    sender = settings.get('sender')
    if sender is None and os.environ.get('AGENT_FLEET_ONEMAIL_ORIGIN'):
        sender = OneMailSender(origin=os.environ['AGENT_FLEET_ONEMAIL_ORIGIN'],
                               token=os.environ.get('AGENT_FLEET_ONEMAIL_TOKEN', ''),
                               site_password=os.environ.get('AGENT_FLEET_ONEMAIL_SITE_PASSWORD', ''))
    if sender is None and os.environ.get('AGENT_FLEET_SMTP_HOST'):
        sender = SmtpSender(host=os.environ['AGENT_FLEET_SMTP_HOST'],
                            port=os.environ.get('AGENT_FLEET_SMTP_PORT', '465'),
                            username=os.environ.get('AGENT_FLEET_SMTP_USERNAME', ''),
                            password=os.environ.get('AGENT_FLEET_SMTP_PASSWORD', ''),
                            sender=os.environ.get('AGENT_FLEET_SMTP_FROM', ''),
                            starttls=os.environ.get('AGENT_FLEET_SMTP_STARTTLS') == '1')
    hcaptcha_sitekey = settings.get('hcaptcha_sitekey', os.environ.get('AGENT_FLEET_HCAPTCHA_SITEKEY', ''))
    hcaptcha_secret = settings.get('hcaptcha_secret', os.environ.get('AGENT_FLEET_HCAPTCHA_SECRET', ''))
    turnstile_sitekey = settings.get('turnstile_sitekey', os.environ.get('AGENT_FLEET_TURNSTILE_SITEKEY', ''))
    turnstile_secret = settings.get('turnstile_secret', os.environ.get('AGENT_FLEET_TURNSTILE_SECRET', ''))

    app.config.update(ACCOUNTS_ENABLED=True, ACCOUNT_ORIGIN=origin,
                      ACCOUNT_REGISTRATION=registration, ACCOUNT_SENDER=sender,
                      ACCOUNT_TRUSTED_PROXIES=trusted_proxies, ACCOUNT_COOKIE_SECURE=parsed.scheme == 'https',
                      ACCOUNT_HCAPTCHA_SITEKEY=hcaptcha_sitekey, ACCOUNT_HCAPTCHA_SECRET=hcaptcha_secret,
                      ACCOUNT_TURNSTILE_SITEKEY=turnstile_sitekey, ACCOUNT_TURNSTILE_SECRET=turnstile_secret)
    app.extensions['accounts'] = AccountStore(config.root / 'var' / 'accounts' / 'accounts.db')
    app.register_blueprint(bp)

    @app.before_request
    def account_boundary():
        if not request.path.startswith('/api/') or not request.endpoint:
            return None
        if request.blueprint in MACHINE_BLUEPRINTS:
            return None
        # Other machine-only handlers must keep their own credential guard, but
        # browser cookies must never be accepted as machine credentials.
        # 只认显式登记在路由视图本身的标记（见 hub.auth.mark_machine_auth），
        # 不沿 __wrapped__ 下钻：外层 operator 装饰器不会意外继承机器认证。
        if is_machine_auth_view(app.view_functions.get(request.endpoint)):
            return None
        try:
            if request.method not in ('GET', 'HEAD', 'OPTIONS'):
                if request.headers.get('Origin') != current_app.config['ACCOUNT_ORIGIN']:
                    fail('invalid_origin', '请求来源无效，请刷新页面后重试', 403)
                if not request.is_json:
                    fail('invalid_json', '请求体必须是 JSON', 415)
            if request.endpoint in PUBLIC:
                if request.method == 'POST':
                    data = body()
                    if request.endpoint == 'accounts.login':
                        login_identifier(data.get('email'))
                    else:
                        email_address(data.get('email'))
                    operation = request.endpoint
                    if operation == 'accounts.code':
                        if data.get('purpose') not in ('register', 'reset'):
                            fail('invalid_purpose', '验证码用途无效')
                        operation += ':' + data['purpose']
                    address = client_address(request, current_app.config['ACCOUNT_TRUSTED_PROXIES'])
                    store().limit('ip:' + address + ':' + operation, 60, 900)
                return None
            user = account_identity(request)
            if not user:
                fail('unauthorized', '请先登录', 401)
            g.account = user
            g.operator = user['id']
            user_catalog = request.blueprint == 'platform' and request.method == 'GET' and request.path in (
                '/api/platform/v1/defaults', '/api/platform/v1/models', '/api/platform/v1/workspaces')
            if user['role'] != 'admin' and request.blueprint not in USER_BLUEPRINTS and not user_catalog:
                fail('forbidden', '此功能需要管理员权限', 403)
        except ApplicationError as exc:
            return error_response(exc)

    @app.after_request
    def private_response(response):
        if response.is_streamed and request.path.startswith('/api/') and g.get('account'):
            account_store = store()
            token = request.cookies.get(cookie_name(), '')
            original = response.response
            interval = float(current_app.config.get('ACCOUNT_STREAM_REVALIDATE_S', STREAM_REVALIDATE_S))
            def authorized_stream():
                # 会话在 boundary 已校验；之后每 ~30s 复核一次（而非每个 chunk 一次查库），
                # 登出/停用最迟一个间隔后断流。
                checked = time.monotonic()
                try:
                    for chunk in original:
                        now = time.monotonic()
                        if now - checked >= interval:
                            if not account_store.identity(token):
                                return
                            checked = now
                        yield chunk
                finally:
                    close = getattr(original, 'close', None)
                    if close:
                        close()
            response.response = authorized_stream()
        if request.path.startswith('/api/accounts') or request.path == '/api/operator/session':
            response.headers['Cache-Control'] = 'no-store'
        return response


@bp.errorhandler(ApplicationError)
def account_error(error):
    return error_response(error)


@bp.get('/options')
def options():
    return jsonify(enabled=True, registration=current_app.config['ACCOUNT_REGISTRATION'],
                   mail_available=current_app.config['ACCOUNT_SENDER'] is not None,
                   hcaptcha_sitekey=current_app.config.get('ACCOUNT_HCAPTCHA_SITEKEY', ''),
                   turnstile_sitekey=current_app.config.get('ACCOUNT_TURNSTILE_SITEKEY', ''))


@bp.post('/code')
def code():
    data = body()
    sender = current_app.config['ACCOUNT_SENDER']
    if sender is None:
        fail('mail_unavailable', '邮件服务尚未配置，请联系管理员', 503)

    email = data.get('email')
    address = client_address(request, current_app.config['ACCOUNT_TRUSTED_PROXIES'])

    # Cloudflare Turnstile token validation (required whenever a secret is configured)
    turnstile_secret = current_app.config.get('ACCOUNT_TURNSTILE_SECRET')
    turnstile_response = data.get('turnstile_response') or data.get('cf-turnstile-response')
    if turnstile_secret:
        # 配置了 secret 就必须带 token：缺失不能当作"跳过验证"。
        if not turnstile_response:
            fail('turnstile_required', '请先完成人机验证', 400)
        verifier = current_app.config.get('TURNSTILE_VERIFIER', verify_turnstile)
        if not verifier(turnstile_response, turnstile_secret, remote_ip=address):
            fail('invalid_turnstile', '人机验证失败，请刷新重试', 400)

    # Progressive defense: require hCaptcha after 3 or more attempts within 15 minutes window
    account_store = store()
    window = 900
    # Atomically increment and get previous attempts for IP and email
    ip_attempts = account_store.check_and_increment_limit('mail-attempt:ip:' + address, window)
    email_attempts = account_store.check_and_increment_limit('mail-attempt:email:' + str(email), window) if email else 0
    consecutive_attempts = max(ip_attempts, email_attempts)

    hcaptcha_secret = current_app.config.get('ACCOUNT_HCAPTCHA_SECRET')
    if consecutive_attempts >= 3 and hcaptcha_secret:
        hcaptcha_response = data.get('hcaptcha_response') or data.get('h-captcha-response')
        if not hcaptcha_response:
            fail('captcha_required', '发送过于频繁，请完成人机验证后再试', 400)
        verifier = current_app.config.get('HCAPTCHA_VERIFIER', verify_hcaptcha)
        if not verifier(hcaptcha_response, hcaptcha_secret, remote_ip=address):
            fail('invalid_captcha', '人机验证未通过，请重试', 400)

    try:
        store().issue_code(data.get('email'), data.get('purpose'), current_app.config['ACCOUNT_REGISTRATION'], sender,
                           invite_code=data.get('invite_code'))
    except ApplicationError as exc:
        # Delivery quotas and provider outcomes must not reveal email eligibility.
        # The independent request-IP limit is enforced before this handler.
        if exc.code not in ('rate_limited', 'mail_unavailable'):
            raise
    return jsonify(ok=True, detail='验证码请求已受理。请查看邮箱；若未收到，请稍后重试或联系管理员。')


@bp.post('/register')
def register():
    data = body()
    store().complete_code(data.get('email'), 'register', data.get('code'), data.get('password'),
                          registration=current_app.config['ACCOUNT_REGISTRATION'], name=data.get('name', ''),
                          invite_code=data.get('invite_code'))
    return jsonify(ok=True)


@bp.post('/login')
def login():
    data = body()
    address = client_address(request, current_app.config['ACCOUNT_TRUSTED_PROXIES'])
    token, user = store().login(data.get('email'), data.get('password'), client=address)
    # A new login replaces the browser's previous session.
    store().logout(request.cookies.get(cookie_name(), ''))
    return cookie(jsonify(authenticated=True, user=user), token)


@bp.post('/reset')
def reset():
    data = body()
    store().complete_code(data.get('email'), 'reset', data.get('code'), data.get('password'))
    return cookie(jsonify(ok=True), clear=True)


@bp.post('/logout')
def logout():
    store().logout(request.cookies.get(cookie_name(), ''))
    return cookie(jsonify(ok=True), clear=True)


@bp.get('/me')
def me():
    features = {}
    services = current_app.extensions.get('fleet', {}).get('services', {})
    if g.account['role'] == 'admin':
        for service, feature in (('mcp_catalog_admin', 'mcp_catalog_management'), ('integration_identity_admin', 'fleet_identity_management')):
            if services.get(service):
                features[feature] = True
        if services.get('workflow_summaries'):
            features['workflow_summary_management'] = True
    return jsonify(user=g.account, sessions=store().sessions(g.account['id'], request.cookies.get(cookie_name(), '')),
                   **({'features': features} if features else {}))


@bp.post('/profile')
def profile():
    store().update_profile(g.account['id'], body().get('name'))
    return jsonify(ok=True)


@bp.post('/password')
def password():
    data = body()
    store().change_password(g.account['id'], data.get('old_password'), data.get('password'))
    return cookie(jsonify(ok=True), clear=True)


@bp.post('/sessions/revoke')
def revoke():
    data = body()
    session_id = data.get('session_id')
    if session_id is not None and (not isinstance(session_id, str) or len(session_id) != 64):
        fail('invalid_session', '会话标识无效')
    store().revoke(g.account['id'], session_id)
    return jsonify(ok=True)


def admin():
    if g.account['role'] != 'admin':
        fail('forbidden', '需要管理员权限', 403)


@bp.get('/users')
def users():
    admin()
    return jsonify(users=store().users(request.args.get('after', '')[:64]))


@bp.get('/invitations')
def list_invitations():
    admin()
    return jsonify(invitations=store().invitations())


@bp.post('/invitations')
def invite():
    admin()
    code = store().invite(body().get('email'))
    return jsonify(ok=True, code=code, detail='邀请码已创建：' + code + '（7 天内有效，仅限单次注册）。')


@bp.post('/users/<user_id>')
def manage(user_id):
    admin()
    data = body()
    revision = data.pop('revision', None)
    user = store().manage(g.account['id'], user_id, changes=data, expected_revision=revision)
    return jsonify(ok=True, user=user)
