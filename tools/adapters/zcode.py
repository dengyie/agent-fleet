"""tools/adapters/zcode.py — ZCode CLI adapter"""
from tools.adapters.base import BaseAdapter
from agent_profiles import default_command


class ZcodeAdapter(BaseAdapter):
    name = "zcode"
    # zcode -p：非交互 print 模式。桌面版内嵌 zcode.cjs 的机器（无 `zcode`
    # shim）在 runner.yaml 按本机覆盖 command：
    #   ["node", "<...>/resources/glm/zcode.cjs", "-p", "{instruction}"]
    default_command = list(default_command('zcode'))
    env_keys = ('ZCODE_DATA_BASE_DIR', 'ZCODE_BUILTIN_PROVIDER_CONFIG_FILE',
                'ZCODE_PERSONAL_PROVIDER_CONFIG_FILE', 'ZCODE_BUILTIN_PROVIDER_BUNDLED_CONFIG_FILE')
