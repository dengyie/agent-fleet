"""Task 1 tests: pure-local agent process discovery -> stable Instance metadata.

These tests pin the Task 1 contract from the agent-discovery-adoption plan:

- ``discover_instances()`` adapts process rows into frozen ``Instance`` values
  and returns them stably sorted by ``(pid, started_at)``;
- each bad row is skipped individually and never poisons sibling rows;
- a whole-``ps`` failure yields an empty list (callers bucket it as the fixed
  ``discovery_error`` code), never an exception;
- family classification is bounded to ``OBSERVABLE_AGENT_TYPES`` only;
- ``attachable`` comes from same-user + native path readability.
"""

from __future__ import annotations

from tools.probe import discovery


def test_discover_instances_returns_identity(monkeypatch):
    monkeypatch.setattr(discovery, "enumerate_process_rows", lambda: [
        discovery.ProcRow(
            user="mango", pid=321, pgid=320,
            exe_path="/usr/local/bin/codex", cmdline="codex --resume abc",
            started_at="2026-08-30T00:00:01Z",
        )
    ])
    monkeypatch.setattr(discovery, "native_path_for", lambda row: None)
    monkeypatch.setattr(discovery, "can_attach", lambda row, native: True)
    assert discovery.discover_instances() == [discovery.Instance(
        321, 320, "/usr/local/bin/codex", "codex --resume abc", "codex",
        None, "2026-08-30T00:00:01Z", True,
    )]


def test_bad_row_does_not_poison_other_rows(monkeypatch):
    monkeypatch.setattr(discovery, "enumerate_process_rows", lambda: [
        object(), discovery.ProcRow(
            user="mango", pid=9, pgid=9, exe_path="/bin/hermes",
            cmdline="hermes", started_at="2026-08-30T00:00:02Z")])
    monkeypatch.setattr(discovery, "native_path_for", lambda row: None)
    monkeypatch.setattr(discovery, "can_attach", lambda row, native: True)
    assert [item.pid for item in discovery.discover_instances()] == [9]


def test_results_are_sorted_by_pid_then_started_at(monkeypatch):
    monkeypatch.setattr(discovery, "enumerate_process_rows", lambda: [
        discovery.ProcRow("u", 30, 30, "/bin/codex", "codex", "2026-08-30T00:03:00Z"),
        discovery.ProcRow("u", 10, 10, "/bin/hermes", "hermes", "2026-08-30T00:01:00Z"),
        discovery.ProcRow("u", 12, 12, "/bin/codex", "codex", "2026-08-30T00:00:00Z"),
        discovery.ProcRow("u", 10, 10, "/bin/opencode", "opencode", "2026-08-30T00:02:00Z"),
    ])
    monkeypatch.setattr(discovery, "native_path_for", lambda row: None)
    monkeypatch.setattr(discovery, "can_attach", lambda row, native: True)
    got = [(i.pid, i.started_at) for i in discovery.discover_instances()]
    assert got == sorted(got)
    assert got[0] == (10, "2026-08-30T00:01:00Z")
    assert got[1] == (10, "2026-08-30T00:02:00Z")
    assert got[-1] == (30, "2026-08-30T00:03:00Z")


def test_ps_command_failure_returns_empty_list(monkeypatch):
    def _boom():
        raise OSError("ps missing")
    monkeypatch.setattr(discovery, "enumerate_process_rows", _boom)
    assert discovery.discover_instances() == []
    assert discovery.last_discovery_error == discovery.DISCOVERY_ERROR_CODE


def test_non_agent_rows_are_filtered_out(monkeypatch):
    monkeypatch.setattr(discovery, "enumerate_process_rows", lambda: [
        discovery.ProcRow("root", 1, 1, "/sbin/launchd", "launchd", "2026-08-30T00:00:00Z"),
        discovery.ProcRow("root", 190, 190, "/usr/sbin/sshd", "sshd", "2026-08-30T00:00:00Z"),
    ])
    monkeypatch.setattr(discovery, "native_path_for", lambda row: None)
    monkeypatch.setattr(discovery, "can_attach", lambda row, native: True)
    assert discovery.discover_instances() == []


def test_family_classification_is_bounded_to_observable_types():
    assert set(discovery.OBSERVABLE_AGENT_TYPES) == {
        "codex", "claude_code", "hermes", "pi", "generic"}
    cases = {
        "/usr/local/bin/codex": "codex",
        "/opt/homebrew/bin/codex.js": "codex",
        "/usr/bin/claude": "claude_code",
        "/usr/local/bin/hermes": "hermes",
        "/opt/homebrew/bin/pi": "pi",
        "/usr/bin/opencode": "generic",
        "/tmp/fleetdoos/aider": "generic",
    }
    for path, expected in cases.items():
        assert discovery.classify_family(path, "ignored") == expected
    assert discovery.classify_family("/usr/bin/sshd", "sshd") is None


def test_pi_basename_never_matches_as_substring():
    # pi 只能走精确 basename:可执行名是 pip/pipenv/pil 时绝不分类为 pi。
    assert discovery.classify_family("/usr/bin/pip", "pip install x") is None
    assert discovery.classify_family("/usr/bin/pipenv", "pipenv run") is None
    assert discovery.classify_family("/usr/bin/pil", "pil") is None


def test_can_attach_requires_same_user_and_readable_native(monkeypatch):
    from tools.probe import discovery as d
    monkeypatch.setattr(d, "current_user", lambda: "mango")
    import os
    row = d.ProcRow("mango", 1, 1, "/bin/codex", "codex", "2026-08-30T00:00:00Z")
    assert d.can_attach(row, None) is True
    assert d.can_attach(row, "") is True
    row_other = d.ProcRow("root", 1, 1, "/bin/codex", "codex", "2026-08-30T00:00:00Z")
    assert d.can_attach(row_other, None) is False
    with monkeypatch.context() as m:
        m.setattr(os, "access", lambda p, mode: False)
        assert d.can_attach(row, "/no/read") is False


def test_cmdline_truncated_only_at_schema_boundary(monkeypatch):
    long_cmd = "codex" + " x" * 500
    monkeypatch.setattr(discovery, "enumerate_process_rows", lambda: [
        discovery.ProcRow("mango", 5, 5, "/bin/codex", long_cmd,
                          "2026-08-30T00:00:00Z")])
    monkeypatch.setattr(discovery, "native_path_for", lambda row: None)
    monkeypatch.setattr(discovery, "can_attach", lambda row, native: True)
    got = discovery.discover_instances()
    assert len(got) == 1
    assert len(got[0].cmdline) <= discovery.MAX_CMDLINE_CHARS

def test_codex_native_path_finds_nested_rollout(tmp_path, monkeypatch):
    """Modern codex rollouts live at YYYY/MM/DD/rollout-*.jsonl (3 levels);
    the scan depth must cover them or native_file_path is always None."""
    import os
    from tools.probe import discovery as d
    day = tmp_path / "2026" / "09" / "03"
    day.mkdir(parents=True)
    old = day / "rollout-2026-09-03T01-00-00-a.jsonl"
    old.write_text("{}\n")
    new = day / "rollout-2026-09-03T07-00-00-b.jsonl"
    new.write_text("{}\n")
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    monkeypatch.setattr(d.os.path, "expanduser",
                        lambda p: str(tmp_path) if p == "~/.codex/sessions"
                        else p)
    got = d._codex_native_path("codex app-server")
    assert got == str(new)
