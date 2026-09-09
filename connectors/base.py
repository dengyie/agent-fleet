"""connectors/base.py — Connector 抽象基类

每种 agent 类型实现一个 Connector 子类，注册后即可被中枢调度。
接入新 agent 只需：写一个继承 BaseConnector 的类 + 实现 collect/control。
"""

from abc import ABC, abstractmethod
import json


class BaseConnector(ABC):
    """agent 连接器抽象基类

    TYPE 标识符对应 hosts.yaml 中 agents[].type
    """

    TYPE = "generic"  # 子类必须覆盖

    # ------------------------------------------------------------------
    @abstractmethod
    def collect(self, ctx):
        """采集该 agent 的当前状态。

        Args:
            ctx (ProbeContext): 执行上下文，含 machine 和本地执行工具

        Returns:
            dict: 该 agent 的状态快照（任意结构，事件总线与汇总会消费）
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    def control(self, ctx, action, **kwargs):
        """管控操作（可选实现）。

        Args:
            ctx (ProbeContext): 执行上下文
            action (str): status | restart | tmux | exec | ...
            **kwargs: 附加参数

        Returns:
            dict: {"ok": bool, "message": str, ...}
        """
        raise NotImplementedError(f"{self.TYPE} 连接器未实现 control")

    # ------------------------------------------------------------------
    def detect(self, ctx):
        """检测该 agent 是否部署在目标机器上（可选，默认 True）。

        Returns:
            bool
        """
        return True


# --- codex/pi 共用的 jsonl 会话目录采集脚本 --------------------------------
# 两个连接器的采集逻辑逐字一致，仅扫描根目录不同（~/.codex/sessions、
# ~/.pi/agent/sessions）；模板以 base 作唯一参数，避免逐字复制走样。
_JSONL_SESSION_SCRIPT_TEMPLATE = r"""
import json, os, glob, time
base = os.path.expanduser(%(base)r)
out = {'installed': os.path.isdir(base)}
if not out['installed']:
    print(json.dumps(out)); raise SystemExit(0)
files = []
try:
    for fp in glob.glob(os.path.join(base, '**', '*.jsonl'), recursive=True):
        try:
            st = os.stat(fp)
        except OSError:
            continue
        files.append({'file': os.path.relpath(fp, base), 'mtime': st.st_mtime,
                      'size': st.st_size, 'age_s': int(time.time() - st.st_mtime)})
except Exception:
    pass
files.sort(key=lambda x: x['mtime'], reverse=True)
now = time.time()
out['sessions'] = files[:10]
out['active_count'] = sum(1 for f in files if now - f['mtime'] < 3600)
out['session_count'] = len(files)
print(json.dumps(out))
"""


def jsonl_session_script(base: str) -> str:
    """生成扫描 ``base`` 下 *.jsonl 会话元数据的远端脚本（codex/pi 共用）。"""
    return _JSONL_SESSION_SCRIPT_TEMPLATE % {'base': base}


class JsonlConnector(BaseConnector):
    """只读扫描远端 jsonl 会话目录元数据的连接器基类（codex/pi 共用）。

    子类仅需声明 TYPE、SESSION_BASE 与文案前缀；collect 跑共用脚本并解析，
    control 只支持 status（这类 CLI 自管 agent 不做远程管控）。
    """

    SESSION_BASE = ""  # 子类必须覆盖：远端会话根目录（如 ~/.codex/sessions）

    def collect(self, ctx):
        raw = ctx.run_python(jsonl_session_script(self.SESSION_BASE))
        try:
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError(f"期望 dict，得到 {type(result).__name__}")
            # 兼容脚本可能返回 error 字段的情况
            if "error" in result:
                return result  # 直接传递脚本报告的错误
            if "sessions" not in result:
                raise ValueError("缺少 'sessions' 字段")
            return result
        except (json.JSONDecodeError, ValueError) as exc:
            # 返回结构化错误而非抛出，保持与其它 connector 的契约一致
            return {
                "sessions": [],
                "error": f"collect failed: {type(exc).__name__}: {exc}",
                "raw_preview": raw[:500]
            }

    def control(self, ctx, action, **kwargs):
        if action == "status":
            return {"ok": True, "data": self.collect(ctx)}
        return {"ok": False,
                "message": f"{self.TYPE} 不支持 action={action}（CLI 自管，建议只观测）"}
