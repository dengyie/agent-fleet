"""Antigravity via an owned ACP worker; prompts travel in native JSON-RPC."""
import json
import re
import shutil
import sys
from pathlib import Path

from agent_profiles import default_command
from tools.adapters.base import BaseAdapter
from tools.spawn_policy import UnsafeInstruction


class AgyAdapter(BaseAdapter):
    name = 'agy'
    default_command = list(default_command('agy'))
    env_keys = ('GEMINI_HOME', 'GEMINI_API_KEY', 'GOOGLE_API_KEY',
                'GOOGLE_APPLICATION_CREDENTIALS', 'GOOGLE_CLOUD_PROJECT', 'GOOGLE_CLOUD_LOCATION')

    def bind_account(self, root, identifier, owner_digest):
        from fleet_core.provider_accounts import account_id
        try:
            identifier = account_id(identifier)
            root = Path(root)
            if (not root.is_absolute() or not isinstance(owner_digest, str)
                    or re.fullmatch('[a-f0-9]{64}', owner_digest) is None):
                raise ValueError('invalid_provider_account')
        except (ValueError, TypeError):
            raise UnsafeInstruction('invalid_provider_account') from None
        self._account_binding = (str(root.resolve()), identifier, owner_digest)
        return self

    def child_env(self):
        if getattr(self, '_account_binding', None):
            # The owned worker builds the selected account's vendor env. Do not
            # retain terminal/IDE Google credentials in its continuation recipe.
            from tools.spawn_policy import build_child_env
            import os
            return build_child_env(source={**os.environ, **self.environment})
        return super().child_env()

    def build_argv(self, instruction):
        command = list(self.command or [])
        if command == ['agy', '--output-format', 'stream-json', '--print', '{instruction}']:
            command = ['agy', 'acp']
        if (not command or not all(isinstance(arg, str) and arg for arg in command)
                or any('{instruction}' in arg or arg.split('=', 1)[0] in
                       {'--print', '-p', '--conversation', '--continue', '--resume'} for arg in command)):
            raise UnsafeInstruction('agy_acp_command_required')
        name = command[0].replace('\\', '/').rsplit('/', 1)[-1].lower()
        if name in {'agy', 'agy.exe'} and (len(command) < 2 or command[1] != 'acp'):
            raise UnsafeInstruction('agy_acp_command_required')
        # Existing node configuration may choose a native model or mode. They
        # are negotiated RPC writes, not speculative vendor CLI flags.
        retained, requested, index = [command[0]], {}, 1
        while index < len(command):
            argument = command[index]
            flag = argument.split('=', 1)[0]
            if flag in {'--model', '--mode'}:
                value = argument.split('=', 1)[1] if '=' in argument else command[index + 1] if index + 1 < len(command) else None
                if (not isinstance(value, str) or not 0 < len(value) <= 256
                        or any(ord(char) < 33 or ord(char) == 127 for char in value)
                        or flag in requested):
                    raise UnsafeInstruction('agy_acp_configuration_invalid')
                requested[flag] = value
                index += 1 if '=' in argument else 2
            else:
                retained.append(argument)
                index += 1
        command = retained
        resolved = shutil.which(command[0], path=self.child_env().get('PATH')) or command[0]
        if Path(resolved).suffix.lower() in {'.cmd', '.bat'}:
            raise UnsafeInstruction('agy_acp_native_launcher_required')
        command[0] = resolved
        worker = Path(__file__).resolve().parents[1] / 'providers/agy_worker.py'
        config = [value for key, item in requested.items() for value in ('--requested-' + key[2:], item)]
        account = getattr(self, '_account_binding', None)
        if account:
            config.extend(['--account-root', account[0], '--account-id', account[1],
                           '--account-owner', account[2]])
        return [sys.executable, str(worker), '--vendor-command', json.dumps(command, ensure_ascii=False),
                *config, '--instruction=' + str(instruction)]
