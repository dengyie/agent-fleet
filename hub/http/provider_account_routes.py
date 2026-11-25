"""Thin operator and machine-authenticated provider-account HTTP boundaries."""
from flask import Blueprint, current_app, g, jsonify, request

from hub.application.errors import ApplicationError
from hub.application.supervisor_service import SupervisorServiceError
from hub.auth import require_operator, require_supervisor
from hub.http._common import json_object
from hub.http.errors import error_response

bp = Blueprint('provider_accounts', __name__)


def _service():
    service = current_app.extensions['fleet']['services'].get('provider_accounts')
    if service is None:
        raise ApplicationError('provider_accounts_unavailable', '', 503)
    return service


def _body():
    request.max_content_length = 16384
    return json_object()


def _reply(call, *, created_status=200):
    try:
        result = call()
        response = jsonify(result)
        response.status_code = created_status if result.get('created') else 200
        response.headers['Cache-Control'] = 'no-store'
        return response
    except (ApplicationError, SupervisorServiceError) as exc:
        return error_response(exc)


@bp.get('/api/machines/<machine>/provider-accounts')
@require_operator
def list_accounts(machine):
    return _reply(lambda: _service().list(machine, g.operator))


@bp.post('/api/machines/<machine>/provider-accounts')
@require_operator
def create_account(machine):
    return _reply(lambda: _service().create(machine, _body(), g.operator), created_status=201)


@bp.get('/api/provider-accounts/<account_id>')
@require_operator
def get_account(account_id):
    return _reply(lambda: _service().get(account_id, g.operator))


@bp.post('/api/provider-accounts/<account_id>/operations')
@require_operator
def operate(account_id):
    return _reply(lambda: _service().operate(account_id, _body(), g.operator), created_status=202)


@bp.get('/api/provider-accounts/<account_id>/operations/<command_id>')
@require_operator
def get_operation(account_id, command_id):
    return _reply(lambda: _service().operation(account_id, command_id, g.operator))


@bp.post('/api/supervisor/provider-accounts/resolve')
@require_supervisor
def resolve():
    def perform():
        data = _body()
        if set(data) != {'command_id'}:
            raise ApplicationError('invalid_request', '', 400)
        return _service().resolve(g.supervisor_machine, data['command_id'])
    return _reply(perform)


@bp.post('/api/supervisor/provider-accounts/report')
@require_supervisor
def report():
    return _reply(lambda: _service().report(g.supervisor_machine, _body()))
