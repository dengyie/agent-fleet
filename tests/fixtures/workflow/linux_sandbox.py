"""Compatibility entry for the shared Linux Workflow launcher.

Existing acceptance registrations retain their absolute fixture argv. Runtime
implementations now live in fleet_core and do not import tests.
"""
from pathlib import Path
import runpy


if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().parents[3] / "fleet_core/workflow/linux_launcher.py"), run_name="__main__")
