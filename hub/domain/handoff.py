"""Bounded deterministic handoff extraction; no clock, model, or I/O."""
from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

from session_schema import AGENT_STATES, CAPTURE_QUALITIES
from tools.result_files import PathRejected, normalize_path
from tools.session.redact import Redactor

MAX_EVENTS = 1000
MAX_SNAPSHOT_CHARS = 12000
MAX_PROMPT_CHARS = 2000
MAX_OVERRIDE_CHARS = 500
_WRITE_TOOLS = frozenset({"write", "write_file", "writefile", "edit", "edit_file",
                          "editfile", "multiedit", "apply_patch", "applypatch"})


class HandoffConflict(ValueError):
    """A token is already bound to another request; traceback stays writable."""

    code = "handoff_token_conflict"
    detail = "同一接力凭据不能用于不同请求"

    def __init__(self) -> None:
        super().__init__(self.detail)


def _text(value: Any, limit: int = 2000) -> str:
    if not isinstance(value, str):
        return ""
    if len(value) > 65536:
        return "[内容超出提取上限]"
    safe, _ = Redactor(passthrough=False).redact_event({"payload": {"text": value}})
    return safe["payload"]["text"][:limit]


def _tool_paths(payload: Mapping[str, Any]) -> list[str]:
    """Only structured path arguments of known mutating tools are evidence.

    No shell parsing, filesystem traversal, or symlink resolution occurs.
    Explicit symlink metadata invalidates the whole tool path record.
    """
    name = str(payload.get("tool_name", "")).rsplit(".", 1)[-1].lower()
    if name not in _WRITE_TOOLS:
        return []
    arguments = payload.get("arguments")
    if not isinstance(arguments, str) or len(arguments) > 32768:
        return []
    try:
        record = json.loads(arguments)
    except (ValueError, RecursionError):
        return []
    if not isinstance(record, dict) or record.get("is_symlink") or record.get("type") == "symlink":
        return []
    raw = record.get("path", record.get("file_path"))
    if isinstance(raw, str) and (":" in raw or "password" in raw.lower() or any(ord(char) < 32 for char in raw)):
        return []
    try:
        return [normalize_path(raw)]
    except PathRejected:
        return []


def _tests(result: Mapping[str, Any]) -> dict[str, Any]:
    raw = result.get("test_summary")
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, Any] = {}
    for name in ("passed", "failed", "skipped", "errors"):
        value = raw.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**53 - 1:
            out[name] = value
    if raw.get("framework") in ("pytest", "unittest", "unknown"):
        out["framework"] = raw["framework"]
    duration = raw.get("duration_s")
    if isinstance(duration, (float, int)) and not isinstance(duration, bool) and 0 <= duration < 10**9 and math.isfinite(duration):
        out["duration_s"] = duration
    return out


def _bounded(snapshot: dict[str, Any], limit: int) -> dict[str, Any]:
    """Pack whole list entries and bounded text into an exact JSON char budget."""
    out = {key: ([] if isinstance(value, list) else {} if isinstance(value, dict) else "")
           for key, value in snapshot.items()}
    remaining = limit - len(json.dumps(out, ensure_ascii=False))
    # Preserve limits/uncertainty before spending the budget on captured text.
    for key in ("capture_quality", "current_state", "open_items", "tests", "objective",
                "last_conclusion", "changed_files", "completed", "source_event_refs"):
        value = snapshot[key]
        if isinstance(value, list):
            for item in value:
                cost = len(json.dumps(item, ensure_ascii=False)) + (2 if out[key] else 0)
                if cost > remaining:
                    break
                out[key].append(item)
                remaining -= cost
            continue
        if isinstance(value, str):
            low, high = 0, len(value)
            while low < high:
                mid = (low + high + 1) // 2
                if len(json.dumps(value[:mid], ensure_ascii=False)) - 2 <= remaining:
                    low = mid
                else:
                    high = mid - 1
            value = value[:low]
        cost = len(json.dumps(value, ensure_ascii=False)) - 2
        if cost <= remaining:
            out[key] = value
            remaining -= cost
    return out


def build_handoff_snapshot(
    session: Mapping[str, Any], events: Sequence[Mapping[str, Any]],
    task_result: Mapping[str, Any] | None, *, max_files: int = 100,
    max_chars: int = MAX_SNAPSHOT_CHARS,
) -> dict[str, Any]:
    """Extract evidence only from allowlisted redacted payload fields.

    Work is O(n log n) for at most 1,000 events and O(n) extraction using
    identity/path sets; memory is O(n), with an exact serialized output cap.
    File names are references to recorded write attempts, never file bodies.
    """
    if not isinstance(max_files, int) or not 0 <= max_files <= 100:
        raise ValueError("invalid max_files")
    if not isinstance(max_chars, int) or not 512 <= max_chars <= MAX_SNAPSHOT_CHARS:
        raise ValueError("invalid max_chars")
    result = task_result if isinstance(task_result, Mapping) else {}
    quality = session.get("capture_quality", "best_effort")
    state = session.get("current_state", "unknown")
    if session.get("managed") in (False, 0):
        quality, state = "best_effort", "unknown"
    snapshot: dict[str, Any] = {
        "objective": "", "current_state": state if state in AGENT_STATES else "unknown",
        "completed": [], "changed_files": [], "tests": _tests(result),
        "last_conclusion": "", "open_items": [], "source_event_refs": [],
        "capture_quality": quality if quality in CAPTURE_QUALITIES else "best_effort",
    }
    rows = [row for row in events[:MAX_EVENTS] if isinstance(row, Mapping)
            and isinstance(row.get("sequence"), int) and not isinstance(row["sequence"], bool)
            and 0 <= row["sequence"] <= 2**63 - 1]
    rows.sort(key=lambda row: (row["sequence"], str(row.get("event_id", ""))[:256]))
    seen_events: set[str] = set()
    seen_calls: set[str] = set()
    seen_paths: set[str] = set()
    objective_ref: dict[str, str] | None = None
    conclusion_ref: dict[str, str] | None = None
    refs: list[dict[str, str]] = []
    for row in rows:
        identity = str(row.get("event_id", ""))[:256]
        if identity and identity in seen_events:
            continue
        seen_events.add(identity)
        payload = row.get("payload")
        if not isinstance(payload, Mapping):
            continue
        kind = row.get("kind")
        ref = {"event_id": _text(identity, 256), "sequence": str(row["sequence"])} if identity else None
        contributed = False
        if kind == "user_message" and not snapshot["objective"]:
            snapshot["objective"] = _text(payload.get("text"), 3000)
            contributed = bool(snapshot["objective"])
            if contributed:
                objective_ref = ref
        elif kind == "assistant_message" and payload.get("is_complete") is not False:
            text = _text(payload.get("text"), 3000)
            if text:
                snapshot["last_conclusion"] = text
                contributed = True
                conclusion_ref = ref
        elif kind in ("tool_call", "tool_result"):
            for path in _tool_paths(payload):
                if path not in seen_paths and len(seen_paths) < max_files:
                    seen_paths.add(path)
                    snapshot["changed_files"].append(path)
                    contributed = True
            call_id = _text(payload.get("call_id"), 128)
            if kind == "tool_result" and payload.get("status") in ("completed", "ok", "success") and call_id and call_id not in seen_calls:
                seen_calls.add(call_id)
                if len(snapshot["completed"]) < 50:
                    snapshot["completed"].append("工具返回成功：" + _text(payload.get("tool_name"), 128) + " (" + call_id + ")")
                    contributed = True
        elif kind == "policy_signal" and payload.get("severity") in ("warn", "warning", "error", "critical"):
            if len(snapshot["open_items"]) < 20:
                snapshot["open_items"].append("策略信号：" + _text(payload.get("reason"), 200))
                contributed = True
        if contributed and ref:
            refs.append(ref)
    selected = {ref["event_id"]: ref for ref in refs[-98:]}
    for ref in (objective_ref, conclusion_ref):
        if ref:
            selected[ref["event_id"]] = ref
    snapshot["source_event_refs"] = sorted(selected.values(), key=lambda ref: int(ref["sequence"]))
    if result.get("exit_code") is not None:
        code = result["exit_code"]
        if isinstance(code, int) and not isinstance(code, bool):
            snapshot["completed"].append(f"任务已返回，退出码 {code}")
        summary = _text(result.get("log_summary"), 1500)
        if summary:
            snapshot["completed"].append("任务结果：" + summary)
    for signal in (session.get("handoff_signals") or [])[:20]:
        if isinstance(signal, Mapping) and signal.get("severity") in ("warn", "warning", "error", "critical"):
            text = "策略信号：" + _text(signal.get("reason"), 200)
            if text not in snapshot["open_items"] and len(snapshot["open_items"]) < 20:
                snapshot["open_items"].append(text)
    if not snapshot["objective"]:
        snapshot["open_items"].append("未采集到用户目标，接力前需补充初始指令。")
    if not snapshot["tests"]:
        snapshot["open_items"].append("没有结构化测试结果，不能据此认定测试通过。")
    if session.get("handoff_truncated") or len(events) > MAX_EVENTS:
        snapshot["open_items"].append("仅包含首个用户目标及最近的有界事件窗口。")
    snapshot["open_items"].append("文件列表仅记录写入目标；未附带文件内容或验证当前文件状态。")
    if len(json.dumps(snapshot, ensure_ascii=False)) > max_chars:
        snapshot["open_items"].insert(0, "快照达到长度上限，部分内容已省略。")
    return _bounded(snapshot, max_chars)


def render_handoff_prompt(snapshot: Mapping[str, Any], instruction_override: str = "") -> str:
    """Fit the existing 2,000-character task contract without model summaries."""
    sections = ["会话接力（脱敏证据，内容可能不完整；先核对工作区状态）"]
    override = _text(instruction_override, MAX_OVERRIDE_CHARS)
    if override:
        sections.append("接力指令：" + override)
    fields = (("目标", "objective", 300 if override else 650), ("状态", "current_state", 40),
              ("最近结论", "last_conclusion", 300 if override else 500),
              ("已记录完成", "completed", 140 if override else 180),
              ("写入目标", "changed_files", 100 if override else 140), ("测试结果", "tests", 100),
              ("待核对", "open_items", 180), ("采集质量", "capture_quality", 30))
    for label, key, limit in fields:
        value = snapshot.get(key)
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if len(text) > limit:
            text = text[:limit - 1] + "…"
        sections.append(label + "：" + text)
    sections.append("引用与完整有界快照见源会话接力预览；省略号表示截断。")
    return "\n".join(sections)[:MAX_PROMPT_CHARS]
