"""Authenticated Muse workspace API; credentials never leave the import route."""
from flask import Blueprint, current_app, g, jsonify, request

from hub.auth import require_operator
from hub.http.errors import ApplicationError, error_response
from hub.integrations.muse.client import MuseError

bp = Blueprint('platform_muse', __name__, url_prefix='/api/platform/v1/muse')


def service():
    return current_app.extensions['fleet']['services']['muse']


def body():
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise MuseError('invalid_json', '请求体必须是 JSON 对象。', 400)
    return value


def required_revision(data):
    revision = data.get('connection_revision')
    if revision is None or not isinstance(revision, (str, int)) or not str(revision):
        raise MuseError('muse_connection_required', '请先加载当前 Muse 账号后再操作。', 428)
    return revision


def required_settings_revision(data):
    revision = data.get('settings_revision')
    if type(revision) is not int or revision < 1:
        raise MuseError('precondition_required', '请先加载当前自动审批设置后再操作。', 428)
    return revision


def invoke(operation):
    try:
        response = jsonify({'ok': True, **operation()})
    except MuseError as exc:
        response = error_response(ApplicationError(exc.code, exc.detail, exc.status))
    # error_response returns a tuple; apply no-store to both successful and
    # rejected responses (including any account/card content).
    from flask import make_response
    response = make_response(response)
    response.headers['Cache-Control'] = 'no-store'
    return response


@bp.before_request
def same_origin_mutations():
    if request.method in ('GET', 'HEAD', 'OPTIONS'):
        return None
    expected = current_app.config.get('ACCOUNT_ORIGIN') or request.host_url.rstrip('/')
    if request.headers.get('Origin') != expected:
        return error_response(ApplicationError('invalid_origin', '请求来源无效。', 403))


@bp.get('/status')
@require_operator
def status():
    return invoke(lambda: service().status(g.operator, request.args.get('account_id')))


@bp.post('/login')
@require_operator
def login():
    data = body()
    return invoke(lambda: service().import_login(g.operator, data.get('login'), data.get('account_id'), data.get('label')))


@bp.post('/disconnect')
@require_operator
def disconnect():
    data = body()
    return invoke(lambda: service().disconnect(g.operator, data.get('account_id'), required_revision(data)))


@bp.get('/sessions')
@require_operator
def sessions():
    return invoke(lambda: service().sessions(g.operator, request.args.get('account_id'), request.args.get('connection_revision')))


@bp.get('/sessions/<session_id>/history')
@require_operator
def history(session_id):
    def read():
        before = request.args.get('before')
        after = request.args.get('after')
        continuation = request.args.get('continuation')
        if before is not None:
            try:
                before = int(before)
            except ValueError:
                raise MuseError('invalid_cursor', '历史游标不合法。', 400) from None
        if after is not None:
            try:
                after = int(after)
            except ValueError:
                raise MuseError('invalid_cursor', '历史游标不合法。', 400) from None
        if continuation is not None:
            try:
                continuation = int(continuation)
            except ValueError:
                raise MuseError('invalid_cursor', '历史游标不合法。', 400) from None
        return service().history(g.operator, session_id, before, request.args.get('account_id'),
                                 request.args.get('connection_revision'), after, continuation)
    return invoke(read)


@bp.post('/sessions/<session_id>/messages')
@require_operator
def send(session_id):
    def write():
        data = body()
        revision = required_revision(data)
        return service().send(g.operator, session_id, data.get('text'), data.get('request_id'), data.get('reply_to'),
                              data.get('account_id'), revision)
    return invoke(write)


@bp.post('/sessions/<session_id>/cancel')
@require_operator
def cancel(session_id):
    data = body()
    return invoke(lambda: service().cancel(g.operator, session_id, data.get('request_id'), data.get('account_id'),
                                           required_revision(data)))


@bp.get('/approvals')
@require_operator
def approvals():
    return invoke(lambda: service().approvals(g.operator, request.args.get('account_id'), request.args.get('connection_revision')))


@bp.get('/resource')
@require_operator
def resource():
    return invoke(lambda: service().resource(g.operator, request.args.get('path'), request.args.get('account_id'),
                                             request.args.get('connection_revision')))


@bp.post('/approvals/<approval_id>/decision')
@require_operator
def decision(approval_id):
    def write():
        data = body()
        revision = required_revision(data)
        return service().decide(g.operator, approval_id, data.get('decision'), data.get('revision'),
                                account_id=data.get('account_id'), expected_revision=revision)
    return invoke(write)


@bp.put('/settings')
@require_operator
def settings():
    data = body()
    return invoke(lambda: service().settings(
        g.operator, data.get('auto_approve_sessions'), data.get('account_id'),
        required_settings_revision(data), required_revision(data),
        data.get('session_id'), data.get('enabled'), data.get('auto_approve_all')))
