"""Regression tests for the report wire schema sanitizers.

These pin the existing ``sanitize_*`` conventions in ``report_schema``:
allowlisted fields only, bounded string truncation, and safe scalar
coercion.  Task 2 keeps these stable while adding ``sanitize_instances``.
"""

from report_schema import sanitize_agent_state, sanitize_agents, sanitize_system


def test_sanitize_agent_state_allowlists_and_bounds():
    state = {
        "installed": True,
        "active_count": 2,
        "session_count": 5,
        "sessions": [{"id": "s1"}, {"id": "s2"}],
        "projects": [{"path": "/x" * 100}],
        "secret": "token=drop",
        "processes": [{"cmd": "agent --api-key abc"}],
    }
    clean = sanitize_agent_state("claude_code", state)
    assert clean == {
        "installed": True,
        "active_count": 2,
        "session_count": 5,
        "project_count": 1,
    }
    assert "secret" not in clean


def test_sanitize_agents_bounds_strings_and_falls_back_to_common():
    agents = {
        "codex": {"installed": True, "note": "n" * 300},
        "editor": {"installed": True, "secret": "x"},
    }
    clean = sanitize_agents(agents)
    assert clean["codex"] == {"installed": True, "note": "n" * 200}
    # Unknown agent type falls back to the common allowlist; secrets dropped.
    assert clean["editor"] == {"installed": True}
    assert "secret" not in clean["editor"]


def test_sanitize_system_allowlists():
    system = {
        "platform": "darwin",
        "load": "1.25",
        "mem_total_mb": "16384",
        "disk_used_pct": "25%",
        "uptime": 1200,
        "internal_token": "secret",
    }
    clean = sanitize_system(system)
    assert clean == {
        "platform": "darwin",
        "load": "1.25",
        "mem_total_mb": "16384",
        "disk_used_pct": "25%",
        "uptime": 1200,
    }


def test_sanitize_agent_state_rejects_non_dict():
    assert sanitize_agent_state("codex", None) == {}
    assert sanitize_agent_state("codex", "x") == {}