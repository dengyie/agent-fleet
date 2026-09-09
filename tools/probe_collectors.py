"""Agent-side local collectors for push-only reporting."""

import json
import os
import platform
import shutil
import subprocess
import time

from connectors import create
from connectors.probe import ProbeContext
from report_schema import sanitize_agent_state, sanitize_instances, sanitize_local_profiles
from agent_profiles import OBSERVABLE_AGENT_TYPES
from tools.probe.discovery import discover_instances

DEFAULT_AGENT_TYPES = OBSERVABLE_AGENT_TYPES


def collect_system():
    out = {"platform": platform.system().lower()}
    try:
        out["load"] = f"{os.getloadavg()[0]:.2f}"
    except (AttributeError, OSError):
        out["load"] = "0"

    total_bytes = used_bytes = 0
    try:
        if platform.system() == "Darwin":
            total_bytes = int(subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True, check=True,
            ).stdout.strip())
            vm = subprocess.run(
                ["vm_stat"], capture_output=True, text=True, check=True,
            ).stdout
            page_size = 4096
            available_pages = 0
            for line in vm.splitlines():
                if "page size of" in line:
                    page_size = int(line.split("page size of", 1)[1].split("bytes", 1)[0].strip())
                elif line.startswith(("Pages free:", "Pages inactive:", "Pages speculative:")):
                    available_pages += int(line.split(":", 1)[1].strip().rstrip("."))
            used_bytes = max(0, total_bytes - available_pages * page_size)
        else:
            values = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    key, raw = line.split(":", 1)
                    values[key] = int(raw.strip().split()[0]) * 1024
            total_bytes = values.get("MemTotal", 0)
            used_bytes = max(0, total_bytes - values.get("MemAvailable", 0))
    except (OSError, ValueError, subprocess.CalledProcessError):
        pass
    if total_bytes:
        out["mem_total_mb"] = str(total_bytes // (1024 * 1024))
        out["mem_used_mb"] = str(used_bytes // (1024 * 1024))

    try:
        disk = shutil.disk_usage("/")
        out["disk_used_pct"] = f"{round(disk.used / disk.total * 100)}%"
    except OSError:
        out["disk_used_pct"] = "0"
    return out


def collect_all(machine, agent_types=None):
    selected = tuple(agent_types or DEFAULT_AGENT_TYPES)
    ctx = ProbeContext(machine)
    out = {}
    for agent_type in selected:
        try:
            connector = create(agent_type)
            if not connector.detect(ctx):
                out[agent_type] = {"installed": False}
                continue
            state = connector.collect(ctx)
            state = state or {"installed": False, "note": "no local state"}
            out[agent_type] = sanitize_agent_state(agent_type, state)
        except Exception as exc:
            out[agent_type] = {"error": type(exc).__name__}
    return out


def collect_local_profiles():
    """Secret-free local profile rows.  Never raises."""
    try:
        from tools.local_profile import list_local_profiles
        return sanitize_local_profiles(list_local_profiles())
    except Exception:
        return []


def build_payload(machine, agent_types=None):
    return {
        "machine": machine,
        "agents": collect_all(machine, agent_types),
        "instances": sanitize_instances(discover_instances()),
        "local_profiles": collect_local_profiles(),
        "system": collect_system(),
        "ts": int(time.time()),
    }


def redacted_json(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)
