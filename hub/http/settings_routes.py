"""Thin administrator boundary for centralized configuration."""
from flask import Blueprint, current_app, g, jsonify, request

from fleet_core.locking import LockBusy
from hub.application.defaults_service import DefaultsService
from hub.auth import require_operator
from hub.http._common import json_object
from hub.http.errors import ApplicationError, error_response
from hub.infrastructure.platform_db_base import PlatformRepositoryError
from platform_schema import PlatformValidationError, validate_owner_id

bp = Blueprint('platform_settings', __name__, url_prefix='/api/platform/v1/settings')


def _invoke(fn):
    try:
        if current_app.config.get('ACCOUNTS_ENABLED') and g.get('account', {}).get('role') != 'admin':
            raise ApplicationError('forbidden', '设置管理需要管理员权限', 403)
        response = jsonify(fn())
        response.headers['Cache-Control'] = 'no-store'
        return response
    except ApplicationError as exc:
        return error_response(exc)
    except PlatformValidationError as exc:
        return error_response(ApplicationError(exc.code, exc.detail, 400))
    except PlatformRepositoryError as exc:
        return error_response(DefaultsService._translate(exc))
    except LockBusy:
        return error_response(ApplicationError('settings_busy', '其他配置正在保存，请稍后重新加载', 409))
    except (OSError, UnicodeError):
        return error_response(ApplicationError('settings_unavailable', '配置文件暂时无法读写', 503))


def _catalog():
    service = current_app.extensions['fleet']['services'].get('settings')
    if service is None:
        raise ApplicationError('platform_disabled', '平台尚未启用，请在系统与运行参数中配置并重启 Hub', 503)
    return service


def _runtime():
    return current_app.extensions['fleet']['services']['runtime_settings']


def _owner():
    # An authenticated administrator can explicitly provision a user's catalogs.
    # Ordinary users never enter this blueprint. Default scope stays the caller.
    return validate_owner_id(request.args.get('owner_id') or g.operator)


@bp.get('')
@require_operator
def catalog():
    return _invoke(lambda: _catalog().catalog(_owner()))


@bp.put('/<kind>/<identifier>')
@require_operator
def save(kind, identifier):
    return _invoke(lambda: _catalog().save(_owner(), kind, identifier, json_object()))


@bp.get('/runtime')
@require_operator
def runtime():
    return _invoke(lambda: _runtime().read())


@bp.put('/runtime')
@require_operator
def save_runtime():
    return _invoke(lambda: _runtime().save(json_object()))


@bp.get('/hosts')
@require_operator
def hosts():
    return _invoke(lambda: _runtime().hosts())


@bp.put('/hosts')
@require_operator
def save_hosts():
    return _invoke(lambda: _runtime().save_hosts(json_object()))


@bp.post('/nodes/<identifier>/credential')
@require_operator
def provision_node(identifier):
    def issue():
        return {'ok': True, **_catalog().repository.provision_node_credential(_owner(), identifier)}
    return _invoke(issue)
