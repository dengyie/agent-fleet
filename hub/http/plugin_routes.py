"""Thin operator HTTP adapters for the official plugin registry."""
from flask import Blueprint, current_app, g, jsonify, request
import sqlite3

from fleet_core.plugins import PluginError
from hub.auth import require_operator
from hub.http._common import json_object, request_json
from hub.http.errors import ApplicationError, error_response
from platform_schema import PlatformValidationError
from hub.infrastructure.platform_db_base import PlatformRepositoryError

bp = Blueprint('platform_plugins', __name__, url_prefix='/api/platform/v1/plugins')


def service():
    return current_app.extensions['fleet']['services']['plugins']


def invoke(fn):
    try:
        return fn()
    except PluginError as exc:
        status = 404 if exc.code in {'plugin_not_found', 'node_not_found', 'operation_not_found', 'login_not_found'} else (
            400 if exc.code.startswith('invalid_') or exc.code in {'unknown_plugin_action', 'duplicate_plugin'} else 409)
        details = {
            'plugin_disabled': '请先启用插件及目标节点', 'not_installed': '节点尚未接入此插件',
            'offline': '节点离线或状态已过期', 'dependency_missing': '节点未安装 GitHub CLI',
            'plugin_incompatible': '节点插件版本不兼容，请升级节点',
            'context_unavailable': '节点未配置此运行环境', 'revision_conflict': '设置已变化，请刷新后重试',
            'idempotency_conflict': '同一次操作的参数发生变化',
        }
        return error_response(ApplicationError(exc.code, details.get(exc.code, '插件请求不合法或目标不存在'), status))
    except ApplicationError as exc:
        return error_response(exc)
    except PlatformValidationError as exc:
        return error_response(ApplicationError(exc.code, exc.detail, 400))
    except sqlite3.Error:
        return error_response(ApplicationError('plugin_store', '插件存储暂不可用', 503))
    except PlatformRepositoryError:
        return error_response(ApplicationError('platform_store', '节点存储暂不可用', 503))


@bp.get('')
@require_operator
def catalog():
    return invoke(lambda: jsonify(service().catalog(g.operator)))


@bp.post('/nodes')
@require_operator
def enroll_node():
    def call():
        body = json_object()
        if set(body) - {'node_id', 'label'}:
            raise PluginError('invalid_arguments')
        response = jsonify(enrollment=service().enroll(g.operator, body.get('node_id'), body.get('label', '')))
        response.headers['Cache-Control'] = 'no-store'
        return response, 201
    return invoke(call)


@bp.put('/<plugin_id>')
@require_operator
def configure(plugin_id):
    def call():
        body = json_object()
        if set(body) != {'enabled', 'revision'}:
            raise PluginError('invalid_plugin_settings')
        return jsonify(service().configure(g.operator, plugin_id, body['enabled'], body['revision']))
    return invoke(call)


@bp.get('/<plugin_id>/nodes')
@require_operator
def nodes(plugin_id):
    return invoke(lambda: jsonify(service().targets(g.operator, plugin_id)))


@bp.put('/<plugin_id>/nodes/<node_id>')
@require_operator
def target(plugin_id, node_id):
    def call():
        body = json_object()
        if set(body) != {'enabled'}:
            raise PluginError('invalid_plugin_settings')
        return jsonify(service().set_target(g.operator, plugin_id, node_id, body['enabled']))
    return invoke(call)


@bp.get('/<plugin_id>/operations')
@require_operator
def operations(plugin_id):
    return invoke(lambda: jsonify(service().operations(
        g.operator, plugin_id, node_id=request.args.get('node_id'),
        context_id=request.args.get('context_id'), hostname=request.args.get('hostname'))))


@bp.post('/<plugin_id>/operations')
@require_operator
def submit(plugin_id):
    def call():
        body = request_json()
        if not isinstance(body, dict) or set(body) - {'node_id', 'action', 'arguments', 'idempotency_key'}:
            raise PluginError('invalid_arguments')
        result = service().submit(g.operator, plugin_id, body.get('node_id'), body.get('action'),
                                  body.get('arguments', {}), body.get('idempotency_key'))
        return jsonify(operation=result), 202
    return invoke(call)


@bp.get('/<plugin_id>/operations/<operation_id>')
@require_operator
def operation(plugin_id, operation_id):
    return invoke(lambda: jsonify(operation=service().operation(g.operator, plugin_id, operation_id)))
