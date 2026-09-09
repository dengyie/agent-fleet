"""Fleet-level agent profile registry — single source of truth for agent families.

Mirrors report_schema.py / session_schema.py: stdlib-only, importable by both
hub/ and tools/ without Flask or CLI dependencies.  The runner falls back to
these defaults when a machine's runner.yaml omits a family; the hub and probe
derive their canonical agent-type lists from here.
"""
from __future__ import annotations

from dataclasses import dataclass

DEFAULT_TIMEOUT_S = 1800


@dataclass(frozen=True)
class Profile:
    family: str                              # canonical family id (adapter/connector key)
    default_command: tuple[str, ...] | None  # argv template with {instruction}
    default_timeout_s: int = DEFAULT_TIMEOUT_S
    executable: bool = True                  # has a runner adapter (tools/adapters)
    observable: bool = True                  # probe/connector knows it (report_schema)
    pattern: str | None = None               # generic process match (generic only)
    # Exact process basenames that identify the family (discovery classification).
    # Only for families recognizable by precise basename; ``generic`` keeps its
    # substring ``pattern`` and must NOT be listed here.
    basenames: tuple[str, ...] = ()
    # Native session transcript location (profile-driven discovery fallback for
    # families without a dedicated resolver; codex/claude_code keep theirs).
    native_session_root: str | None = None   # e.g. "~/.pi/agent/sessions"
    native_session_depth: int = 2            # os.walk depth covering the layout


PROFILES: dict[str, Profile] = {
    # --approve-for-me 自带 workspace-write sandbox，勿再传显式 --sandbox
    # （codex-cli 0.147.0 起两者互斥）。
    "codex": Profile("codex", (
        "codex", "exec", "--approve-for-me",
        "--ephemeral", "--ignore-user-config", "--json", "{instruction}"),
        basenames=("codex", "codex-cli", "codex.js")),
    "claude_code": Profile("claude_code", ("claude", "-p", "{instruction}"),
        basenames=("claude",)),
    "hermes": Profile("hermes", None, basenames=("hermes",)),
    "pi": Profile("pi", ("pi", "-p", "{instruction}"),
        basenames=("pi",),
        native_session_root="~/.pi/agent/sessions",
        native_session_depth=2),
    "generic": Profile(
        "generic", None, executable=False,
        pattern="claude|codex|astrbot|openclaw|opencode|aider",
    ),
}

EXECUTABLE_AGENT_TYPES = tuple(k for k, p in PROFILES.items() if p.executable)
OBSERVABLE_AGENT_TYPES = tuple(PROFILES)


def is_executable(agent_type: str) -> bool:
    profile = PROFILES.get(agent_type)
    return bool(profile and profile.executable)


def default_command(agent_type: str) -> tuple[str, ...] | None:
    profile = PROFILES.get(agent_type)
    return profile.default_command if profile else None


def profile_basenames(agent_type: str) -> tuple[str, ...]:
    """Exact process basenames for a family (empty for ``generic``/unknown)."""
    profile = PROFILES.get(agent_type)
    return profile.basenames if profile else ()


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "EXECUTABLE_AGENT_TYPES",
    "OBSERVABLE_AGENT_TYPES",
    "PROFILES",
    "Profile",
    "default_command",
    "is_executable",
    "profile_basenames",
]
