"""Pure public machine read models.

Observation data is reduced through the existing report-schema allowlists before
it crosses the HTTP boundary.  No persistence, Flask, or request state belongs
in this module.
"""

from collections.abc import Mapping
from typing import Any

from report_schema import sanitize_agents, sanitize_instances, sanitize_system, sanitize_local_profiles


_HISTORY_LIMIT = 720


def _host_name(host: Mapping[str, Any]) -> str:
    return str(host.get("name", "")) if isinstance(host, Mapping) else ""


def _host_desc(host: Mapping[str, Any]) -> str:
    if not isinstance(host, Mapping):
        return ""
    return str(host.get("desc", ""))[:200]


def _agent_summaries(agents: Mapping[str, Any]) -> list[dict[str, str]]:
    summaries = []
    for agent_type, state in agents.items():
        if not isinstance(state, Mapping):
            continue
        if state.get("error"):
            summaries.append({
                "type": str(agent_type)[:64],
                "status": "error",
                "detail": str(state["error"])[:200],
            })
        elif state.get("installed") is False:
            summaries.append({"type": str(agent_type)[:64], "status": "absent"})
        else:
            summaries.append({"type": str(agent_type)[:64], "status": "ok"})
    return summaries


def public_machine_summary(
    snapshot: Mapping[str, Any] | None, host: Mapping[str, Any]
) -> dict[str, Any]:
    """Build the public row used by the fleet/status view."""
    name = _host_name(host)
    desc = _host_desc(host)
    if not isinstance(snapshot, Mapping):
        return {"machine": name, "desc": desc, "online": False, "error": "no data"}

    if not snapshot.get("reachable", True):
        return {
            "machine": name,
            "desc": desc,
            "online": False,
            "error": str(snapshot.get("remote_error", "unreachable"))[:200],
        }

    agents = sanitize_agents(snapshot.get("agents", {}))
    hermes = agents.get("hermes") if isinstance(agents, dict) else None
    is_hermes = bool(isinstance(hermes, Mapping) and hermes.get("installed"))
    return {
        "machine": name,
        "desc": desc,
        "online": True,
        "timestamp": snapshot.get("timestamp"),
        "system": sanitize_system(snapshot.get("system", {})),
        "agents": agents,
        "agent_summaries": _agent_summaries(agents),
        "agent_count": len(_agent_summaries(agents)),
        "has_hermes": is_hermes,
        "hermes_state": (hermes or {}).get("gateway_state", "n/a") if is_hermes else None,
    }


def public_machine_detail(
    snapshot: Mapping[str, Any], history: list[Mapping[str, Any]] | None
) -> dict[str, Any]:
    """Build the public machine detail/current/history shape."""
    current = snapshot if isinstance(snapshot, Mapping) else {}
    rows = []
    for item in (history or [])[-_HISTORY_LIMIT:]:
        if not isinstance(item, Mapping):
            continue
        rows.append({
            "ts": item.get("_ts"),
            "reachable": bool(item.get("reachable", True)),
        })
    return {
        "machine": str(current.get("machine", ""))[:64],
        "current": {
            "timestamp": current.get("timestamp"),
            "reachable": bool(current.get("reachable", True)),
            "remote_error": current.get("remote_error"),
            "agents": sanitize_agents(current.get("agents", {})),
            "system": sanitize_system(current.get("system", {})),
            "instances": sanitize_instances(current.get("instances", [])),
            "local_profiles": sanitize_local_profiles(current.get("local_profiles", [])),
        },
        "history": rows,
    }
