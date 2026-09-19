"""connectors/zcode.py — ZCode agent 连接器

采集: ~/.zcode/cli/rollout 下全部 *.jsonl（当前为 ZCode 会话
      model-io-sess_*.jsonl）——只读目录元数据（mtime/size），不解析
      内容，避免隐私与性能开销；与 JsonlConnector 的通用 glob 一致，
      未来 rollout 出现其它 jsonl 时计入会话数（元数据仍不涉内容）。
管控: status 查看活动会话；restart 暂不支持（由 CLI 自管）。
"""

from .base import JsonlConnector


class ZcodeConnector(JsonlConnector):
    TYPE = "zcode"
    SESSION_BASE = '~/.zcode/cli/rollout'
