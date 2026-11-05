"""Allowlisted wire schema for agent reports and public status output.

``sanitize_instances()`` keeps the fixed instance metadata allowlist so probe
snapshots publish discovered agent rows without leaking pid internals,
conversation content, or unknown keys.
"""

import dataclasses
import re

from agent_profiles import OBSERVABLE_AGENT_TYPES, PROFILES

COMMON_AGENT_FIELDS = {
    "installed",
    "error",
    "note",
    "active_count",
    "session_count",
    "process_count",
}

INSTANCE_FIELDS = (
    "pid",
    "pgid",
    "exe_path",
    "cmdline",
    "agent_family",
    "native_file_path",
    "started_at",
    "attachable",
)

# Exclusive observable families — derived from the agent_profiles registry so a
# new family registered there is ingestable/attachable without touching this file.
INSTANCE_FAMILIES = frozenset(OBSERVABLE_AGENT_TYPES)

# ISO8601 timestamps never exceed 32 chars (mirrors discovery MAX_STARTED_AT_CHARS).
MAX_STARTED_AT_CHARS = 32

# Per-family extra observable state fields (on top of COMMON_AGENT_FIELDS);
# families without an entry fall back to COMMON_AGENT_FIELDS at sanitize time.
_FAMILY_EXTRA_FIELDS = {
    "hermes": {
        "gateway_state",
        "active_agents",
        "platforms",
    },
    "claude_code": {"project_count"},
}

AGENT_FIELDS = {
    family: COMMON_AGENT_FIELDS | _FAMILY_EXTRA_FIELDS.get(family, set())
    for family in PROFILES
}

SYSTEM_FIELDS = {
    "platform",
    "load",
    "mem_total_mb",
    "mem_used_mb",
    "disk_used_pct",
    "uptime",
}

LOCAL_PROFILE_FIELDS = (
    "profile_id",
    "family",
    "label",
    "origin",
    "current",
)
_MAX_LOCAL_PROFILES = 64
_MAX_PROFILE_ID = 128
_MAX_PROFILE_LABEL = 64


def _safe_value(value):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:200]
    if isinstance(value, list):
        return [item[:64] for item in value[:20] if isinstance(item, str)]
    return None


def sanitize_agent_state(agent_type, state):
    if not isinstance(state, dict):
        return {}
    allowed = AGENT_FIELDS.get(agent_type, COMMON_AGENT_FIELDS)
    clean = {}
    for key in allowed:
        if key not in state:
            continue
        value = _safe_value(state[key])
        if value is not None:
            clean[key] = value
    if agent_type == "claude_code" and "project_count" not in clean:
        projects = state.get("projects")
        if isinstance(projects, list):
            clean["project_count"] = len(projects)
    return clean


def sanitize_agents(agents):
    if not isinstance(agents, dict):
        return {}
    return {
        str(agent_type)[:64]: sanitize_agent_state(str(agent_type), state)
        for agent_type, state in list(agents.items())[:64]
    }


def sanitize_system(system):
    if not isinstance(system, dict):
        return {}
    clean = {}
    for key in SYSTEM_FIELDS:
        if key not in system:
            continue
        value = _safe_value(system[key])
        if value is not None and not isinstance(value, list):
            clean[key] = value
    return clean


def sanitize_local_profiles(value) -> list[dict]:
    """Public local-profile rows: ids/labels only, never secrets."""
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    for item in value[:_MAX_LOCAL_PROFILES]:
        if not isinstance(item, dict):
            continue
        profile_id = item.get("profile_id")
        family = item.get("family")
        if not isinstance(profile_id, str) or not profile_id:
            continue
        if not isinstance(family, str) or not family:
            continue
        if len(profile_id) > _MAX_PROFILE_ID:
            continue
        lowered = profile_id.lower()
        if any(marker in lowered for marker in (
                "token", "secret", "password", "api_key", "credential")):
            continue
        row = {
            "profile_id": profile_id[:_MAX_PROFILE_ID],
            "family": family[:32],
            "label": str(item.get("label") or profile_id)[:_MAX_PROFILE_LABEL],
            "origin": str(item.get("origin") or "cc_switch")[:32],
            "current": bool(item.get("current")),
        }
        out.append(row)
    return out


def _to_int(value):
    """Coerce an integer field, or return ``None`` for malformed values."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        sign = ""
        if text[:1] in ("+", "-"):
            sign, text = text[:1], text[1:]
        if text.isdigit():
            return int(sign + text)
    return None


def _to_bool(value):
    """Coerce a boolean field to real ``bool``, never returning ``None``."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "")
    return False


def _instance_dict(item):
    """Accept task-1 ``Instance`` dataclasses and plain dicts alike."""
    if isinstance(item, dict):
        return item
    if dataclasses.is_dataclass(item):
        try:
            return dataclasses.asdict(item)
        except (TypeError, ValueError):
            return None
    return None


# --settings 的值是 JSON/路径，可能内嵌 env（API key / auth token），整段
# 替换到下一个 ``--flag`` 或行尾（ps 输出不保留引号，无法按 argv 精确切分）。
_SETTINGS_ARG_RE = re.compile(r"(--settings)(?:=|\s+)(?:(?!\s--?[A-Za-z]).)+")
# 名字中含凭据词（按 -/_ 分段整词匹配，--max-tokens 等不误伤）的 flag 值。
_FLAG_ARG_RE = re.compile(r"(?:(?<=\s)|^)(--?([A-Za-z][\w-]*))(=|\s+)(?!-)(\S+)")
_SECRET_FLAG_WORDS = frozenset({
    "key", "apikey", "token", "secret", "password", "passwd", "pass",
    "credential", "credentials", "auth"})


def _redact_flag(match):
    flag, name, sep, value = match.groups()
    parts = [part for part in re.split(r"[-_]", name.lower()) if part]
    if (not parts or parts[0] == "no" or value.startswith("[REDACTED:")
            or not _SECRET_FLAG_WORDS.intersection(parts)):
        return match.group(0)
    return f"{flag}{sep}[REDACTED:credential]"


def redact_cmdline(cmdline: str) -> str:
    """Replace secret-looking argument values before a cmdline is reported.

    Covers ``--settings <json>``, ``--api-key=...``/``--token X`` style flags,
    then the shared secret rules (``KEY=value`` env prefixes, ``sk-``/Bearer
    tokens …).  Idempotent; runs on the full string before truncation.
    """
    if not cmdline:
        return cmdline
    text = _SETTINGS_ARG_RE.sub(r"\1 [REDACTED:settings]", cmdline)
    text = _FLAG_ARG_RE.sub(_redact_flag, text)
    try:
        from tools.session.redact import redact_secrets_text
    except ImportError:  # 极简部署缺少 tools 包时仍保留上面的 flag 级脱敏
        return text
    return redact_secrets_text(text)[0]


def sanitize_instances(value: object) -> list[dict]:
    """Sanitize discovered agent instances into the fixed allowlisted pivot.

    Each accepted row keeps exactly ``pid, pgid, exe_path, cmdline,
    agent_family, native_file_path, started_at, attachable``; unknown keys are
    dropped, bounded strings are truncated, integer/boolean fields are
    normalized, and malformed entries — including negative ``pid``/``pgid``
    (which the frontend ``parseInstanceRow`` rejects) — are dropped
    individually.
    """
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    for item in value:
        record = _instance_dict(item)
        if record is None:
            continue
        pid = _to_int(record.get("pid"))
        pgid = _to_int(record.get("pgid"))
        family = record.get("agent_family")
        if not isinstance(family, str) or family not in INSTANCE_FAMILIES:
            continue
        if pid is None or pgid is None or pid < 0 or pgid < 0:
            continue
        exe_path = _safe_value(record.get("exe_path"))
        if not isinstance(exe_path, str):
            exe_path = ""
        if not exe_path.strip():
            continue
        raw_cmdline = record.get("cmdline")
        # 先在完整字符串上脱敏再截断，避免密钥被 200 字符截断拆开后漏检
        cmdline = _safe_value(redact_cmdline(raw_cmdline)
                              if isinstance(raw_cmdline, str) else raw_cmdline)
        if not isinstance(cmdline, str):
            cmdline = ""
        native = _safe_value(record.get("native_file_path"))
        native_file_path = native if isinstance(native, str) and native else None
        started_at = _safe_value(record.get("started_at"))
        if not isinstance(started_at, str):
            started_at = ""
        started_at = started_at[:MAX_STARTED_AT_CHARS]
        out.append({
            "pid": pid,
            "pgid": pgid,
            "exe_path": exe_path,
            "cmdline": cmdline,
            "agent_family": family,
            "native_file_path": native_file_path,
            "started_at": started_at,
            "attachable": _to_bool(record.get("attachable")),
        })
    return out
