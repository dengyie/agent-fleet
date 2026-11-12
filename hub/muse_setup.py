"""Inspect or explicitly provision an immutable local Muse journal identity."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from connectors.external_agents.muse import open_muse_bridge
from fleet_core.web_agent_contract import MuseBridgeError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", required=True, type=Path)
    parser.add_argument("--initialize", action="store_true")
    args = parser.parse_args(argv)
    try:
        path = args.journal.expanduser()
        if not path.is_absolute():
            raise ValueError("muse_setup_requires_absolute_journal")
        bridge = open_muse_bridge(path)
        identity = bridge.initialize() if args.initialize else bridge.journal_identity()
        report = {"ok": True, "mode": "initialize" if args.initialize else "preview",
                  "state": "initialized" if identity is not None else "uninitialized"}
        if identity is not None:
            report["journal_identity"] = identity
    except MuseBridgeError as exc:
        print(json.dumps({"ok": False, "error": exc.code}))
        return 2
    except (OSError, ValueError, TypeError, sqlite3.Error):
        print(json.dumps({"ok": False, "error": "muse_setup_invalid_journal"}))
        return 2
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
