"""Explicit restart-time settings backed by the deployment's existing files."""
from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from fleet_core.locking import OwnedLock
from hub.application.errors import ApplicationError

# (FleetConfig field, label, type, minimum, maximum). No credentials in this list.
FIELDS = [
    ('platform_enabled', '平台功能', 'bool'),
    ('platform_worker_enabled', '本地执行 Worker', 'bool'),
    ('platform_worker_scheduler_enabled', 'Worker 调度器', 'bool'),
    ('platform_provider_network_enabled', '模型网络访问', 'bool'),
    ('platform_workflows_enabled', '工作流', 'bool'),
    ('platform_workflow_dispatch_enabled', '工作流自动派发', 'bool'),
    ('platform_capability_routing_enabled', '节点能力路由', 'bool'),
    ('platform_external_agents_enabled', '外部 Agent', 'bool'),
    ('platform_remote_execution_enabled', '远程节点执行', 'bool'),
    ('platform_fleet_mcp_enabled', 'Fleet MCP 身份与摘要', 'bool'),
    ('platform_mcp_connectors_enabled', 'MCP 连接器', 'bool'),
    ('platform_mcp_catalog_registration_enabled', 'MCP 连接登记', 'bool'),
    ('platform_artifact_transfer_enabled', '产物传输', 'bool'),
    ('platform_memory_enabled', '记忆管理', 'bool'),
    ('platform_memory_context_enabled', '记忆上下文', 'bool'),
    ('platform_browser_enabled', '浏览器执行', 'bool'),
    ('platform_browser_network_enabled', '浏览器网络访问', 'bool'),
    ('platform_browser_submit_enabled', '浏览器表单提交', 'bool'),
    ('service_monitoring_enabled', '服务监控', 'bool'),
    ('service_actions_enabled', '服务操作', 'bool'),
    ('execution_windows_enabled', '执行窗口', 'bool'),
    ('komari_enabled', 'Komari 数据源', 'bool'),
    ('komari_network_enabled', 'Komari 网络访问', 'bool'),
    ('komari_sync_enabled', 'Komari 自动同步', 'bool'),
    ('http_probe_enabled', 'HTTP 探测', 'bool'),
    ('http_probe_network_enabled', 'HTTP 探测网络访问', 'bool'),
    ('http_probe_sync_enabled', 'HTTP 自动探测', 'bool'),
    ('session_repositories_enabled', '会话管理', 'bool'),
    ('session_search_enabled', '会话搜索', 'bool'),
    ('supervisor_enabled', '节点 Supervisor', 'bool'),
    ('adoption_repositories_enabled', '会话纳管', 'bool'),
    ('platform_worker_interval_s', 'Worker 轮询间隔（秒）', 'number', .1, 60),
    ('platform_worker_lease_s', 'Worker 租约（秒）', 'number', 5, 3600),
    ('platform_worker_max_concurrency', '全局并发数', 'integer', 1, 64),
    ('platform_worker_max_workspace_concurrency', '每工作区并发数', 'integer', 1, 64),
    ('history_retention_days', '会话历史保留天数', 'integer', 7, 3650),
    ('komari_sync_interval_s', 'Komari 同步间隔（秒）', 'number', 5, 3600),
    ('http_probe_sync_interval_s', 'HTTP 探测间隔（秒）', 'number', 5, 3600),
    ('komari_base_url', 'Komari API URL', 'url'),
    ('komari_nodes_path', 'Komari 节点 API 路径', 'text'),
    ('platform_workflow_registration_file', '工作流登记文件', 'text'),
    ('platform_muse_registration_file', 'Muse 登记文件', 'text'),
    ('platform_mcp_registration_file', 'MCP 登记文件', 'text'),
    ('platform_browser_allowed_origins', '浏览器允许的 Origin（JSON 数组）', 'array'),
    ('http_probe_allowed_origins', 'HTTP 探测允许的 Origin（JSON 数组）', 'array'),
    ('platform_sandbox_launcher', '本地沙盒启动命令（JSON argv）', 'array'),
]
SPECS = {'AGENT_FLEET_' + spec[0].upper(): spec for spec in FIELDS}


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _read(path):
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b''


def _lines(raw):
    result = {}
    for line in raw.decode('utf-8').splitlines():
        if line.strip().startswith(('#', ';')):
            continue
        key, sep, value = line.partition('=')
        if sep:
            result.setdefault(key.strip(), value.strip())
    return result


def _value(spec, value):
    kind = spec[2]
    if kind == 'bool':
        if not isinstance(value, bool):
            raise ValueError()
        return '1' if value else '0'
    if kind in {'number', 'integer'}:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not spec[3] <= value <= spec[4] or (kind == 'integer' and int(value) != value):
            raise ValueError()
        return str(value)
    if kind == 'array':
        if not isinstance(value, list) or len(value) > 64 or any(not isinstance(v, str) or len(v) > 2048 or '\x00' in v for v in value):
            raise ValueError()
        if spec[0].endswith('allowed_origins'):
            from hub.integrations.http_probe import (
                HttpProbeError,
                canonical_probe_origin,
            )
            try:
                return ','.join(canonical_probe_origin(item) for item in value)
            except HttpProbeError:
                raise ValueError() from None
        if any(not item for item in value):
            raise ValueError()
        return json.dumps(value, ensure_ascii=False)
    if not isinstance(value, str) or len(value) > 2048 or any(c in value for c in '\r\n\x00'):
        raise ValueError()
    if kind == 'url' and value:
        parts = urlsplit(value)
        if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError()
    return value


class RuntimeSettings:
    def __init__(self, config, effective):
        self.config = config
        self.effective = effective
        self.path = config.root / 'fleet-gates.conf'

    def read(self):
        raw = _read(self.path)
        saved = _lines(raw)
        settings = []
        for key, spec in SPECS.items():
            effective = self.effective.get(spec[0].upper(), getattr(self.config, spec[0], None))
            if isinstance(effective, Path):
                effective = str(effective)
            if isinstance(effective, tuple):
                effective = list(effective)
            settings.append({'key': key, 'label': spec[1], 'type': spec[2],
                'effective': effective, 'saved': saved.get(key),
                'environment_override': bool(os.environ.get(key)),
                'min': spec[3] if len(spec) > 3 else None, 'max': spec[4] if len(spec) > 4 else None})
        return {'ok': True, 'digest': _digest(raw), 'settings': settings, 'restart_required': True,
                'path': str(self.path), 'paths': {name: str(getattr(self.config, name)) for name in ('root', 'state_dir', 'hosts_file', 'platform_db', 'platform_artifact_root')}}

    def _write(self, path, expected, validate):
        path.parent.mkdir(parents=True, exist_ok=True)
        with OwnedLock(path.with_name(path.name + '.settings.lock'), purpose='settings-write'):
            before = _read(path)
            if _digest(before) != expected:
                raise ApplicationError('revision_conflict', '文件已更改，请重新加载后保存', 409)
            content = validate(before)
            backup = path.with_name(path.name + '.before-settings-' + uuid.uuid4().hex)
            if path.exists():
                with open(backup, 'xb') as stream:
                    stream.write(before)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(backup, 0o600)
            temporary = path.with_name(path.name + '.settings-' + uuid.uuid4().hex)
            with open(temporary, 'xb') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        return str(backup) if backup.exists() else None

    def save(self, body):
        if not isinstance(body, dict) or not isinstance(body.get('digest'), str) or not isinstance(body.get('values'), dict):
            raise ApplicationError('invalid_settings', '需要文件版本与配置项', 400)
        changes = body['values']
        try:
            if any(key not in SPECS for key in changes):
                raise ValueError()
            converted = {key: _value(SPECS[key], value) for key, value in changes.items()}
        except (ValueError, TypeError, OverflowError):
            raise ApplicationError('invalid_settings', '配置项不支持或数值超出范围', 400) from None

        def merge(before):
            lines = []
            for line in before.decode('utf-8').splitlines():
                key = line.partition('=')[0].strip()
                if key not in converted:
                    lines.append(line)
            lines.extend(key + '=' + value for key, value in converted.items())
            return ('\n'.join(lines) + '\n').encode('utf-8')

        backup = self._write(self.path, body['digest'], merge)
        return {**self.read(), 'backup_path': backup}

    def hosts(self):
        raw = _read(self.config.hosts_file)
        return {'ok': True, 'digest': _digest(raw), 'content': raw.decode('utf-8'), 'path': str(self.config.hosts_file), 'restart_required': True}

    def save_hosts(self, body):
        if not isinstance(body, dict) or not isinstance(body.get('content'), str) or not isinstance(body.get('digest'), str):
            raise ApplicationError('invalid_settings', '需要 YAML 内容及文件版本', 400)
        raw = body['content'].encode('utf-8')
        try:
            data = yaml.safe_load(raw)
            if not isinstance(data, dict) or not isinstance(data.get('hosts'), list) or len(data['hosts']) > 1000:
                raise ValueError()
            names = set()
            from hub.repositories import MACHINE_NAME_RE
            for host in data['hosts']:
                if not isinstance(host, dict) or not isinstance(host.get('name'), str) or not MACHINE_NAME_RE.fullmatch(host['name']) or host['name'] in names:
                    raise ValueError()
                names.add(host['name'])
                if 'projects' in host and (not isinstance(host['projects'], list) or any(not isinstance(item, str) for item in host['projects'])):
                    raise ValueError()
                if host.get('transport', 'ingest') != 'ingest' or not 1 <= int(host.get('stale_after_s', 300)) <= 86400:
                    raise ValueError()
            thresholds = data.get('thresholds', {})
            if not isinstance(thresholds, dict):
                raise TypeError()
            for key in ('high_load', 'disk_high_pct'):
                if key in thresholds and (isinstance(thresholds[key], bool) or not isinstance(thresholds[key], (int, float)) or not math.isfinite(thresholds[key]) or thresholds[key] < 0):
                    raise ValueError()
        except (ValueError, TypeError, yaml.YAMLError, RecursionError):
            raise ApplicationError('invalid_settings', '节点清单需要唯一名称、ingest 传输和有效的 stale_after_s', 400) from None
        backup = self._write(self.config.hosts_file, body['digest'], lambda _: raw)
        return {**self.hosts(), 'backup_path': backup}
