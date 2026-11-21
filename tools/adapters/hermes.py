"""tools/adapters/hermes.py — Hermes adapter

Hermes CLI 版本和入口有差异，default_command=None：
必须在 runner.yaml agents.hermes.command 显式配置本机命令模板。
"""
from tools.adapters.base import BaseAdapter


class HermesAdapter(BaseAdapter):
    name = "hermes"
    default_command = None
    env_keys = ('HERMES_HOME', 'HERMES_INFERENCE_MODEL', 'HERMES_INFERENCE_PROVIDER',
                'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'ANTHROPIC_API_KEY', 'TERMINAL_CWD')

    def execution_env(self, workdir):
        from pathlib import Path
        env = self.child_env()
        if self.command and Path(self.command[0]).name.lower() in {'hermes', 'hermes.exe'}:
            from tools.hermes_workspace import workspace_environment
            return workspace_environment(env, workdir)
        return env
