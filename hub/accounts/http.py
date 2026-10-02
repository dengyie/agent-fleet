"""Account HTTP endpoints and fail-closed role boundary for account mode."""
import os
from urllib.parse import urlsplit

from flask import Blueprint, current_app, g, jsonify, request
from hub.accounts.store import AccountStore, email_address, fail
from hub.accounts.client_address import client_address, proxy_networks
from hub.accounts.mail import OneMailSender, SmtpSender
from hub.http.errors import ApplicationError, error_response

bp = Blueprint('accounts', __name__, url_prefix='/api/accounts')
def cookie_name():
    # Host-bound production cookies cannot be planted by sibling subdomains.
    return '__Host-fleet_session' if current_app.config['ACCOUNT_COOKIE_SECURE'] else 'fleet_session'
PUBLIC = {'accounts.options', 'accounts.code', 'accounts.register', 'accounts.login', 'accounts.reset'}
# These handlers already enforce their own distinct machine credential domain.
MACHINE_BLUEPRINTS = {'commands', 'platform_nodes'}
# Only audited owner-scoped surfaces are available to ordinary accounts.
USER_BLUEPRINTS = {'accounts', 'operator', 'platform_conversations', 'platform_artifacts'}


def store():
    return current_app.extensions['accounts']


def body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        fail('invalid_json', '请求体必须是 JSON 对象')
    return data


def account_identity(req):
    if any(req.headers.get(header) for header in (
        'X-Agent-Fleet-Token', 'X-Runner-Credential', 'X-Supervisor-Credential', 'X-Platform-Node-Credential')):
        return None
    return store().identity(req.cookies.get(cookie_name(), ''))


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
    app.config.update(ACCOUNTS_ENABLED=True, ACCOUNT_ORIGIN=origin,
                      ACCOUNT_REGISTRATION=registration, ACCOUNT_SENDER=sender,
                      ACCOUNT_TRUSTED_PROXIES=trusted_proxies, ACCOUNT_COOKIE_SECURE=parsed.scheme == 'https')
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
        view = app.view_functions.get(request.endpoint)
        while view:
            if getattr(view, '_fleet_machine_auth', False):
                return None
            view = getattr(view, '__wrapped__', None)
        try:
            if request.method not in ('GET', 'HEAD', 'OPTIONS'):
                if request.headers.get('Origin') != current_app.config['ACCOUNT_ORIGIN']:
                    fail('invalid_origin', '请求来源无效，请刷新页面后重试', 403)
                if not request.is_json:
                    fail('invalid_json', '请求体必须是 JSON', 415)
            if request.endpoint in PUBLIC:
                if request.method == 'POST':
                    data = body()
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
            def authorized_stream():
                try:
                    for chunk in original:
                        if not account_store.identity(token):
                            return
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
                   mail_available=current_app.config['ACCOUNT_SENDER'] is not None)


@bp.post('/code')
def code():
    data = body()
    sender = current_app.config['ACCOUNT_SENDER']
    if sender is None:
        fail('mail_unavailable', '邮件服务尚未配置，请联系管理员', 503)
    try:
        store().issue_code(data.get('email'), data.get('purpose'), current_app.config['ACCOUNT_REGISTRATION'], sender)
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
                          registration=current_app.config['ACCOUNT_REGISTRATION'], name=data.get('name', ''))
    return jsonify(ok=True)


@bp.post('/login')
def login():
    data = body()
    token, user = store().login(data.get('email'), data.get('password'))
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
    return jsonify(user=g.account, sessions=store().sessions(g.account['id'], request.cookies.get(cookie_name(), '')))


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


@bp.post('/invitations')
def invite():
    admin()
    store().invite(body().get('email'))
    return jsonify(ok=True, detail='邀请资格已创建，7 天内可通过注册页验证邮箱。')


@bp.post('/users/<user_id>')
def manage(user_id):
    admin()
    data = body()
    revision = data.pop('revision', None)
    user = store().manage(g.account['id'], user_id, changes=data, expected_revision=revision)
    return jsonify(ok=True, user=user)
