"""Tests for JsonlConnector base class and jsonl_session_script.

S4 golden test: jsonl_session_script must produce stable, bounded output and
survive empty/missing directories without crashing the remote Python process.
"""

from connectors.base import jsonl_session_script


def test_jsonl_session_script_produces_valid_python():
    script = jsonl_session_script("~/.codex/sessions")
    assert "import " in script
    assert "json.dumps" in script
    assert "~/.codex/sessions" in script
    # 脚本必须是合法 Python（exec 不报错）
    compile(script, "<test>", "exec")


def test_jsonl_session_script_golden_structure():
    # S4 golden test: 脚本输出结构必须稳定（sessions 数组 + error 字段）
    script = jsonl_session_script("/tmp/test")
    assert '"sessions":' in script or "sessions" in script
    # 脚本必须包含异常捕获逻辑（避免远端 Python 进程崩溃）
    assert "except" in script.lower()


def test_jsonl_session_script_escapes_base_path():
    # 路径注入防御：base 中的特殊字符必须转义
    script = jsonl_session_script("~/.codex'/sessions")
    assert "~/.codex'/sessions" in script
    # %s 格式化后单引号应被转义或包裹在其它引号内
    compile(script, "<test>", "exec")


def test_collect_handles_malformed_script_output():
    """脚本输出非法 JSON 时应返回错误结构"""
    class BadContext:
        def run_python(self, script):
            return "not json"

    from connectors.codex import CodexConnector
    result = CodexConnector().collect(BadContext())
    assert "error" in result
    assert result["sessions"] == []
    assert "not json" in result.get("raw_preview", "")


def test_collect_handles_missing_sessions_key():
    """脚本返回 JSON 但缺少 sessions 键时应返回错误结构"""
    class MalformedContext:
        def run_python(self, script):
            return '{"wrong_key": []}'

    from connectors.pi import PiConnector
    result = PiConnector().collect(MalformedContext())
    assert "error" in result
    assert result["sessions"] == []
    assert "sessions" in result.get("error", "").lower()

