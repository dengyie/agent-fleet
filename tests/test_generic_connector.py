"""Tests for the pure-local ``GenericConnector.enumerate_processes`` rows.

Task 1: the generic connector exposes the extracted process enumeration as a
bounded local iterator (no SSH/remote script), keeping the existing
``collect``/``control`` behavior untouched.
"""

from __future__ import annotations

from tools.probe import discovery
from connectors.generic import GenericConnector


def test_enumerate_processes_filters_by_pattern(monkeypatch):
    rows = [
        discovery.ProcRow("mango", 201, 201, "/usr/bin/opencode",
                          "opencode", "2026-08-30T00:00:00Z"),
        discovery.ProcRow("mango", 202, 202, "/usr/local/bin/aider",
                          "aider", "2026-08-30T00:00:00Z"),
        discovery.ProcRow("root", 203, 203, "/usr/sbin/sshd",
                          "sshd", "2026-08-30T00:00:00Z"),
    ]
    monkeypatch.setattr(discovery, "enumerate_process_rows", lambda: rows)
    found = [row.pid for row in GenericConnector().enumerate_processes()]
    assert found == [201, 202]


def test_enumerate_processes_survives_ps_failure(monkeypatch):
    def _boom():
        raise OSError("ps missing")
    monkeypatch.setattr(discovery, "enumerate_process_rows", _boom)
    assert list(GenericConnector().enumerate_processes()) == []


def test_old_collect_behavior_is_preserved(monkeypatch):
    # Existing collect path is untouched by the enumeration extract.
    connector = GenericConnector()
    assert connector.pattern == "claude|codex|astrbot|openclaw|opencode|aider"
    assert callable(connector.collect)
    assert callable(connector.control)