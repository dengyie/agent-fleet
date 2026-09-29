"""tests/test_task_view_review.py — Task 16 复查：任务视图 / 兼容 shell cutover

聚焦 Task 16 final review fixes，不重写既有的 1100 行 frontend 契约测试：
  - 前端 task.js 源码契约：加载代际栅栏、终态 class 白名单、命名日志上界
  - 前端 cutover 配置：默认关闭、暴露 frontend_dir、应用装配兼容
  - cutover 开启时 / /machine/<n> /task/<id> 返回静态 shell + root base href
  - cutover 关闭时旧模板/状态行为保持不变
  - shell 缺失失败关闭为有界非敏感响应
"""
import tempfile
import unittest
from pathlib import Path

from hub import events
from hub import state as store
from hub import task_store
from hub.bootstrap import create_app
from hub.config import FleetConfig

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = REPO_ROOT / "frontend"

_TASK_JS = None


def _task_js():
    global _TASK_JS
    if _TASK_JS is None:
        _TASK_JS = FRONTEND_DIR.joinpath("assets", "views", "task.js").read_text(encoding="utf-8")
    return _TASK_JS


class TaskJsSourceContractTests(unittest.TestCase):
    """前端 task.js 源码级契约（无需 Node 运行时）。"""

    def test_load_task_uses_generation_fencing(self):
        source = _task_js()
        # 请求代际单调递增：重试发起新 loadTask 时旧请求失效
        self.assertIn("++loadSeq", source)
        # 成功与失败回调都必须先校验代际再写状态（两处）
        fail_branch = source.split("function loadTask()", 1)[1]
        self.assertEqual(fail_branch.count("generation !== loadSeq"), 2)
        # 卸载栅栏保留
        self.assertIn("if (disposed", source)
        self.assertIn("disposed = true", source)

    def test_terminal_class_is_allowlisted_not_concatenated(self):
        source = _task_js()
        # 终止节点 class 来自显式白名单映射
        self.assertIn("var TERMINAL_CLASS", source)
        field = source.split("var TERMINAL_CLASS", 1)[1].split("};", 1)[0]
        for cls in ("terminal-failed", "terminal-cancelled", "terminal-expired"):
            self.assertIn(cls, field)
        # 渲染使用白名单解析器，而非把状态名拼进 class 串
        self.assertIn("terminalClass(state)", source)
        self.assertNotIn("terminal-' + state", source)

    def test_log_summary_bound_is_named(self):
        source = _task_js()
        self.assertIn("var MAX_LOG_SUMMARY_RENDER", source)
        render_slice = source.split("function renderLogs", 1)[1]
        # 渲染处使用命名常量而非硬编码 4096
        self.assertIn("slice(0, MAX_LOG_SUMMARY_RENDER)", render_slice)
        self.assertNotIn("slice(0, 4096)", render_slice)
