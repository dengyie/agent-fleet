"""Node-local cc-switch profile registry (Phase 5).

Hub may send only a locally registered opaque ``profile_id``.  This module is
the node-side authority:

- list labels / ids / family / current flag (never secrets);
- apply by flipping ``is_current`` in the local cc-switch db;
- never read ``settings_config`` / ``api_key`` / ``auth`` onto any wire;
- never write those values into a control receipt, log, or Hub payload.

The executor does **not** copy credentials into ``~/.claude`` / ``~/.codex``
(that would be ``write_file`` / ``change_credential``).  It only updates the
local registry's current pointer.  Missing db / unknown id / family mismatch
are bounded codes.
"""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any

_DEFAULT_DB = Path.home() / ".cc-switch" / "cc-switch.db"
_MAX_ID = 128
_MAX_LABEL = 64
_MAX_ROWS = 64
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ID_REJECT = ("/", "\\", "token", "secret", "private", "password",
              "api_key", "apikey", "credential")

#: cc-switch ``app_type`` -> fleet agent family.  Unknown app types are
#: omitted from the public list so they can never be selected from the hub.
_APP_TO_FAMILY = {
    "claude": "claude_code",
    "claude-desktop": "claude_code",
    "codex": "codex",
    "hermes": "hermes",
    "pi": "pi",
}

_FAMILY_TO_APPS = {
    "claude_code": frozenset({"claude", "claude-desktop"}),
    "codex": frozenset({"codex"}),
    "hermes": frozenset({"hermes"}),
    "pi": frozenset({"pi"}),
}


def _db_path() -> Path:
    override = os.environ.get("AGENT_FLEET_CC_SWITCH_DB", "")
    if override:
        return Path(override).expanduser()
    return _DEFAULT_DB


def _safe_id(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > _MAX_ID:
        return None
    lowered = value.lower()
    if any(marker in lowered for marker in _ID_REJECT):
        return None
    if _ID_RE.fullmatch(value) is None:
        return None
    return value


def _safe_label(value: Any, fallback: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()[:_MAX_LABEL]
    return fallback[:_MAX_LABEL]


def list_local_profiles(db_path: Path | None = None) -> list[dict]:
    """Public, secret-free local profile rows.  Never raises."""
    path = Path(db_path) if db_path is not None else _db_path()
    try:
        if not path.is_file():
            return []
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
    except (OSError, sqlite3.Error):
        return []
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, app_type, name, is_current FROM providers"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        try:
            con.close()
        except Exception:
            pass
    out: list[dict] = []
    for row in rows:
        if len(out) >= _MAX_ROWS:
            break
        profile_id = _safe_id(row["id"])
        app_type = str(row["app_type"] or "")
        family = _APP_TO_FAMILY.get(app_type)
        if profile_id is None or family is None:
            continue
        out.append({
            "profile_id": profile_id,
            "family": family,
            "label": _safe_label(row["name"], profile_id),
            "origin": "cc_switch",
            "current": bool(row["is_current"]),
        })
    return out


def apply_local_profile(profile_id: str, *, family: str | None = None,
                        db_path: Path | None = None) -> str:
    """Flip the local current pointer.  Returns a bounded outcome code.

    Never copies ``settings_config`` / keys / endpoints.  Unknown, malformed,
    or family-mismatched ids fail closed.
    """
    safe = _safe_id(profile_id)
    if safe is None:
        return "invalid_payload"
    path = Path(db_path) if db_path is not None else _db_path()
    try:
        if not path.is_file():
            return "unknown_profile"
        con = sqlite3.connect(str(path), timeout=1.0)
    except (OSError, sqlite3.Error):
        return "unknown_profile"
    try:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT id, app_type FROM providers WHERE id = ? LIMIT 1",
            (safe,),
        ).fetchone()
        if row is None:
            return "unknown_profile"
        app_type = str(row["app_type"] or "")
        found_family = _APP_TO_FAMILY.get(app_type)
        if found_family is None:
            return "unknown_profile"
        if family and family not in ("", "generic"):
            allowed = _FAMILY_TO_APPS.get(str(family), frozenset())
            if app_type not in allowed:
                return "family_mismatch"
        con.execute(
            "UPDATE providers SET is_current = 0 WHERE app_type = ?",
            (app_type,),
        )
        con.execute(
            "UPDATE providers SET is_current = 1 WHERE id = ? AND app_type = ?",
            (safe, app_type),
        )
        con.commit()
        return "applied"
    except sqlite3.Error:
        return "control_failed"
    finally:
        try:
            con.close()
        except Exception:
            pass


__all__ = ["apply_local_profile", "list_local_profiles"]
