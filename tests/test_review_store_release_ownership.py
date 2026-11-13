"""Review regression: one ownership contract for release-managed paths.

fleet-gates.conf is live operator config: it must not be packaged, and the
overlay sync must neither overwrite nor delete it.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from deploy import auto_release as release

REPO_ROOT = Path(__file__).resolve().parents[1]


def _package_paths():
    text = (REPO_ROOT / "deploy/package-release.sh").read_text()
    block = re.search(r"git archive --format=tar HEAD \\\n(.*?)\n\s*> ", text, re.S).group(1)
    return set(block.replace("\\", " ").split())


def _bash_array(name):
    script = f'. "{REPO_ROOT}/deploy/hk-overlay-sync.sh"; printf "%s\\n" "${{{name}[@]}}"'
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout
    return out.split()


def test_package_overlay_and_auto_release_agree():
    managed = set(release.MANAGED)
    assert _package_paths() == managed - {"RELEASE_ORIGIN"}
    assert set(_bash_array("FLEET_RELEASE_MANAGED_PATHS")) == managed | {"frontend"}


def test_fleet_gates_is_runtime_owned_everywhere():
    assert "fleet-gates.conf" not in release.MANAGED
    assert "fleet-gates.conf" in release.RUNTIME_FILES
    assert "fleet-gates.conf" not in _package_paths()
    assert "fleet-gates.conf" not in _bash_array("FLEET_RELEASE_MANAGED_PATHS")
    assert "fleet-gates.conf" in _bash_array("FLEET_RUNTIME_FILES")
    assert "--exclude=fleet-gates.conf" in _bash_array("FLEET_RSYNC_RUNTIME_EXCLUDES")


@pytest.mark.parametrize("rsync", [True, False])
def test_overlay_sync_keeps_live_fleet_gates(tmp_path, rsync):
    if rsync and shutil.which("rsync") is None:
        pytest.skip("rsync unavailable")
    source, live = tmp_path / "release", tmp_path / "live"
    (source / "hub").mkdir(parents=True)
    (source / "hub/web.py").write_text("new release code")
    (live / "hub").mkdir(parents=True)
    (live / "hub/web.py").write_text("old")
    (live / "fleet-gates.conf").write_text("OPERATOR=1\n")
    path = "" if rsync else "command() { [[ $2 == rsync ]] && return 1; builtin command \"$@\"; }; "
    subprocess.run(
        ["bash", "-c", f'{path}. "{REPO_ROOT}/deploy/hk-overlay-sync.sh"; '
                       f'fleet_overlay_sync_tree "{source}" "{live}"'],
        check=True, capture_output=True, text=True)
    assert (live / "hub/web.py").read_text() == "new release code"
    assert (live / "fleet-gates.conf").read_text() == "OPERATOR=1\n"
