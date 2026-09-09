"""tools/adapters/codex.py — Codex CLI adapter"""
from tools.adapters.base import BaseAdapter


class CodexAdapter(BaseAdapter):
    name = "codex"
    # codex exec：--approve-for-me 自带 workspace-write sandbox（codex-cli 0.147.0
    # 起与显式 --sandbox 互斥，clap 报 "cannot be used with"），故不重复传。
    default_command = [
        "codex", "exec", "--approve-for-me",
        "--ephemeral", "--ignore-user-config", "--json", "{instruction}",
    ]
