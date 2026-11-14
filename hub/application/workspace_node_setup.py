"""Generate explicit workspace Node configuration; never provisions a cloud VM."""
import base64

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from fleet_core.workspace_execution import parse_workspace_execution, valid_hub_origin
from hub.application.errors import ApplicationError
from platform_schema import validate_id


class WorkspaceNodeSetupService:
    def __init__(self, repository, delivery, *, remote_enabled):
        self.repository, self.delivery, self.remote_enabled = repository, delivery, remote_enabled

    def enroll(self, owner, node_id, body):
        node_id = validate_id(node_id, 'node_id')
        if type(body) is not dict or set(body) != {'hub_url', 'execution_mode', 'sandbox_launcher'}:
            raise ApplicationError('invalid_settings', '需要 Hub 地址、执行模式和启动参数', 400)
        try:
            origin = valid_hub_origin(body['hub_url'])
        except ValueError:
            raise ApplicationError('invalid_settings', 'Hub 地址必须是 HTTPS 源站地址（本机可使用 HTTP）', 400) from None
        if not self.remote_enabled:
            raise ApplicationError('remote_execution_disabled', '请先启用远程执行并重启 Hub', 503)
        if self.delivery is None or not self.delivery.signing_key:
            raise ApplicationError('command_signature_required', '请先配置平台命令签名并重启 Hub', 503)
        node = self.repository.get_node(owner, node_id)
        if not node or not node['enabled'] or node.get('capabilities', {}).get('workspace.exec') is not True:
            raise ApplicationError('node_unavailable', '节点必须启用并声明 workspace.exec 能力', 409)
        defaults = self.repository.get_defaults(owner)
        roots = {row['workspace_id']: row['root_path'] for row in self.repository.list_workspaces(owner)
                 if row['enabled'] and row['backend'] == 'directory'
                 and (row.get('default_node_id') or defaults.get('execution_node_id')) == node_id}
        if not roots:
            raise ApplicationError('workspace_unavailable', '请先在工作区设置中绑定此节点及绝对目录', 409)
        try:
            workspace = parse_workspace_execution({key: body[key] for key in ('execution_mode', 'sandbox_launcher')} | {'roots': roots})
        except ValueError:
            raise ApplicationError('invalid_settings', '执行模式、工作目录或沙盒参数无效；VM 直接执行不需要沙盒参数', 400) from None
        public = Ed25519PrivateKey.from_private_bytes(self.delivery.signing_key).public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw)
        # All validation precedes credential rotation. The operator installs
        # this file and restarts the Node to apply mode/path changes.
        credential = self.repository.provision_node_credential(owner, node_id)['credential']
        return {'ok': True, 'enrollment': {'node_id': node_id, 'hub_url': origin, 'credential': credential,
                'public_key': base64.b64encode(public).decode('ascii'), 'plugins': {}, 'workspace': workspace}}
