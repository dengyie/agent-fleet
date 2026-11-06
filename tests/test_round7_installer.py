"""Round 7: 节点侧事务化更新器 / drain 协议 / 服务发现与迁移（全部在 tmp 中，不碰真实 plist）。"""
import os
import plistlib
import subprocess
from pathlib import Path

import pytest

from fleet_core import client_release as cr
from fleet_core.control import generate_ed25519_keypair
from fleet_core.identity import ProcessIdentity
from tools.client_update import installer as inst
from tools.client_update import runtime
from tools.client_update import services as svc
from tools.client_update.installer import Installer, Layout, UpdateError

NOW = 1_800_000_000
PRIVATE, PUBLIC = generate_ed25519_keypair()


def make_source(root: Path, body: str) -> Path:
    (root / "tools").mkdir(parents=True)
    (root / "tools" / "agent-runner.py").write_text(body)
    (root / "fleet_core").mkdir()
    (root / "fleet_core" / "__init__.py").write_text("")
    return root


class FakeFetcher:
    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.signed = None
        self.data = b""
        self.tamper = False

    def publish(self, version, sequence, body=None, **manifest_overrides):
        src = make_source(self.tmp_path / "src" / version, body or f"V = {version!r}\n")
        info = cr.release_info(version, sequence, "stable", cr.tree_files(src))
        self.data = cr.build_archive(src, info)
        manifest = {
            "schemaVersion": cr.SCHEMA_VERSION, "kind": cr.MANIFEST_KIND,
            "channel": "stable", "sequence": sequence, "version": version,
            "publishedAt": NOW, "issuedAt": NOW, "expiresAt": NOW + 3600,
            "artifact": {"size": len(self.data), "sha256": cr.sha256_bytes(self.data)},
            "keyId": cr.key_id(PUBLIC), **manifest_overrides,
        }
        self.signed = {"manifest": manifest, "signature": cr.sign_manifest(PRIVATE, manifest)}

    def manifest(self, channel):
        return self.signed

    def artifact(self, sha256, size):
        return self.data + b"x" if self.tamper else self.data


class FakeServices:
    """模拟 launchd：kickstart 时（可选地）以目标版本写回执，表示新进程已接管。"""

    def __init__(self, home: Path, *, takes_over=True):
        self.home = home
        self.takes_over = takes_over
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(cmd)
        if cmd[:2] == ["launchctl", "kickstart"] and self.takes_over:
            version = Layout(self.home).link_version(Layout(self.home).current)
            runtime._write_json(runtime.receipt_path("runner", self.home), {
                "role": "runner", "version": version,
                **ProcessIdentity.current().to_dict()})
        return subprocess.CompletedProcess(cmd, 0, "", "")


@pytest.fixture(autouse=True)
def tiny_smoke(monkeypatch):
    monkeypatch.setattr(inst, "_SMOKE_IMPORTS", ("fleet_core",))


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "client"
    fetcher = FakeFetcher(tmp_path)
    run = FakeServices(home)
    layout = Layout(home)
    service = svc.Service("runner", "launchd", "com.test.runner", tmp_path / "x.plist",
                          layout.current / "tools" / "agent-runner.py")

    def make(**kw):
        return Installer(layout=layout, public_keys=[PUBLIC], fetcher=fetcher,
                         services=[service], run=run, clock=lambda: NOW,
                         sleep=lambda s: None, grace_s=0, **kw)

    return {"home": home, "fetcher": fetcher, "run": run, "layout": layout, "make": make}


def phases(layout):
    return sorted(runtime.read_json(p)["phase"] for p in layout.transactions.glob("*.json"))


def test_install_switches_current_and_verifies(env):
    env["fetcher"].publish("v1", 1)
    result = env["make"]().update()
    lay = env["layout"]
    assert result["action"] == "installed" and result["verified"] == ["runner"]
    assert lay.link_version(lay.current) == "v1"
    assert os.readlink(lay.current) == "releases/v1"
    assert cr.read_release_info(lay.current)["sequence"] == 1
    assert runtime.read_json(runtime.drain_path("runner", lay.home))["targetVersion"] == "v1"
    assert phases(lay) == ["committed"]
    assert env["make"]().update()["action"] == "up_to_date"


def test_check_rejects_bad_signature_and_sequence_rollback(env):
    fetcher = env["fetcher"]
    fetcher.publish("v2", 2)
    fetcher.signed["manifest"]["sequence"] = 9
    with pytest.raises(UpdateError, match="bad_signature"):
        env["make"]().check()
    fetcher.publish("v5", 5)
    env["make"]().update()
    fetcher.publish("v3", 3)
    with pytest.raises(UpdateError, match="sequence_rollback"):
        env["make"]().update()
    with pytest.raises(UpdateError, match="no_trusted_key"):
        Installer(layout=env["layout"], public_keys=[], fetcher=fetcher).check()


def test_expired_manifest_rejected(env):
    env["fetcher"].publish("v1", 1, expiresAt=NOW - 1)
    with pytest.raises(UpdateError, match="manifest_expired"):
        env["make"]().update()


def test_artifact_mismatch_leaves_nothing_installed(env):
    env["fetcher"].publish("v1", 1)
    env["fetcher"].tamper = True
    with pytest.raises(UpdateError, match="artifact_mismatch"):
        env["make"]().update()
    assert env["layout"].link_version(env["layout"].current) is None


def test_smoke_failure_aborts_and_records_failed_sequence(env):
    env["fetcher"].publish("v1", 1, body="def broken(:\n")
    with pytest.raises(UpdateError, match="smoke_failed"):
        env["make"]().update()
    lay = env["layout"]
    assert lay.link_version(lay.current) is None
    assert not any(lay.staging.iterdir())
    assert phases(lay) == ["aborted"]
    assert env["make"]().observation()["failedSequence"] == 1
    assert env["make"]().update()["action"] == "skip_failed"


def test_verify_failure_rolls_back_to_previous(env):
    env["fetcher"].publish("v1", 1)
    env["make"]().update()
    env["run"].takes_over = False
    os.unlink(runtime.receipt_path("runner", env["home"]))
    env["fetcher"].publish("v2", 2)
    with pytest.raises(UpdateError, match="verify_failed"):
        env["make"]().update()
    lay = env["layout"]
    assert lay.link_version(lay.current) == "v1"
    assert phases(lay) == ["committed", "rolled_back"]
    assert env["make"]().observation()["failedSequence"] == 2
    # 回滚后让旧版本的服务回到 v1：drain 目标改回 v1
    assert runtime.read_json(runtime.drain_path("runner", lay.home))["targetVersion"] == "v1"


def test_old_process_still_busy_counts_as_draining(env):
    env["fetcher"].publish("v1", 1)
    env["make"]().update()
    env["run"].takes_over = False  # 旧 v1 进程（本进程 identity，存活）还在跑长任务
    env["fetcher"].publish("v2", 2)
    result = env["make"]().update()
    assert result["draining"] == ["runner"] and result["verified"] == []
    assert env["layout"].link_version(env["layout"].current) == "v2"


def test_crash_after_switch_recovers_by_rollback(env, monkeypatch):
    env["fetcher"].publish("v1", 1)
    env["make"]().update()
    env["fetcher"].publish("v2", 2)

    def crash(*a, **k):
        raise KeyboardInterrupt

    with monkeypatch.context() as m, pytest.raises(KeyboardInterrupt):
        m.setattr(Installer, "_drain", crash)
        env["make"]().update()
    lay = env["layout"]
    assert lay.link_version(lay.current) == "v2"
    assert len(env["make"]().status()["pendingTransactions"]) == 1
    done = env["make"]().recover()
    assert done[0]["to"] == "rolled_back"
    assert lay.link_version(lay.current) == "v1"
    assert env["make"]().status()["pendingTransactions"] == []


def test_crash_before_switch_recovers_by_abort(env, monkeypatch):
    env["fetcher"].publish("v1", 1)
    env["make"]().update()
    env["fetcher"].publish("v2", 2)
    with monkeypatch.context() as m, pytest.raises(KeyboardInterrupt):
        m.setattr(Installer, "_switch_and_verify",
                  lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
        env["make"]().update()
    # 下一次 update 先 recover（staged → aborted），再正常安装 v2
    result = env["make"]().update()
    assert result["recovered"][0]["to"] == "aborted"
    assert env["layout"].link_version(env["layout"].current) == "v2"


def test_manual_rollback_swaps_current_and_previous(env):
    env["fetcher"].publish("v1", 1)
    env["make"]().update()
    with pytest.raises(UpdateError, match="no_previous"):
        env["make"]().rollback()
    env["fetcher"].publish("v2", 2)
    env["make"]().update()
    assert env["make"]().rollback() == {"action": "rolled_back", "current": "v1",
                                         "previous": "v2"}


def test_gc_keeps_bounded_releases(env):
    for seq in range(1, 6):
        env["fetcher"].publish(f"v{seq}", seq)
        env["make"]().update()
    lay = env["layout"]
    kept = sorted(p.name for p in lay.releases.iterdir())
    assert len(kept) == inst.KEEP_RELEASES and {"v5", "v4"} <= set(kept)


def test_dry_run_installs_nothing(env):
    env["fetcher"].publish("v1", 1)
    result = env["make"]().update(dry_run=True)
    assert result["action"] == "install" and result["dryRun"] is True
    assert env["layout"].link_version(env["layout"].current) is None


# -- runtime / drain ------------------------------------------------------------

def _managed(monkeypatch, home, version):
    root = home / "releases" / version
    root.mkdir(parents=True)
    monkeypatch.setenv("AGENT_FLEET_CLIENT_HOME", str(home))
    monkeypatch.setattr(runtime, "CODE_ROOT", root.resolve())
    monkeypatch.setattr(runtime, "client_info", lambda: {
        "version": version, "sequence": 1, "channel": "stable", "managed": True})


def test_drain_requested_only_when_current_points_at_target(tmp_path, monkeypatch):
    home = tmp_path / "client"
    _managed(monkeypatch, home, "v1")
    (home / "releases" / "v2").mkdir()
    assert runtime.drain_requested("runner") is False
    runtime._write_json(runtime.drain_path("runner", home), {"targetVersion": "v2"})
    Layout(home).set_link(home / "current", "v1")
    assert runtime.drain_requested("runner") is False  # current 还没切：退出只会拉起旧码
    Layout(home).set_link(home / "current", "v2")
    assert runtime.drain_requested("runner") is True
    runtime._write_json(runtime.drain_path("runner", home), {"targetVersion": "v1"})
    assert runtime.drain_requested("runner") is False  # 已是目标版本


def test_legacy_tree_never_drains(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_FLEET_CLIENT_HOME", str(tmp_path))
    runtime._write_json(runtime.drain_path("runner", tmp_path), {"targetVersion": "v2"})
    assert runtime.client_info()["managed"] is False
    assert runtime.drain_requested("runner") is False


def test_announce_writes_identity_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_FLEET_CLIENT_HOME", str(tmp_path))
    runtime.announce("probe")
    receipt = runtime.read_json(runtime.receipt_path("probe", tmp_path))
    assert receipt["version"] == "legacy" and receipt["pid"] == os.getpid()
    assert ProcessIdentity.from_dict(receipt).alive() is True


# -- services ---------------------------------------------------------------------

def _plist(path: Path, label: str, script: str):
    path.write_bytes(plistlib.dumps({"Label": label, "KeepAlive": True,
                                     "ProgramArguments": ["/usr/bin/python3", script, "--x"]}))


def test_discover_and_migrate_launchd(tmp_path):
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    _plist(agents / "a.plist", "com.x.runner", "/old/tree/tools/agent-runner.py")
    _plist(agents / "b.plist", "com.x.cc", "/old/tree/tools/supervisor/control_client_cli.py")
    _plist(agents / "c.plist", "com.other", "/usr/bin/other.py")
    found = svc.discover(launch_dir=agents, systemd_dir=tmp_path / "none")
    assert [(s.role, s.label) for s in found] == [("runner", "com.x.runner"),
                                                  ("control-client", "com.x.cc")]
    current = tmp_path / "client" / "current"
    calls = []

    def run(cmd):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    backup = svc.migrate(found[1], current, stamp="20261107", run=run)
    assert backup.name == "b.plist.agent-fleet-backup-20261107-r7" and backup.exists()
    args = plistlib.loads((agents / "b.plist").read_bytes())["ProgramArguments"]
    assert args == ["/usr/bin/python3",
                    str(current / "tools" / "supervisor" / "control_client_cli.py"), "--x"]
    again = svc.discover(launch_dir=agents, systemd_dir=tmp_path / "none")[1]
    assert again.uses(current) and svc.migrate(again, current, run=run) is None
    assert all("-k" not in c for c in calls)


def test_discover_systemd_and_kickstart_never_kills(tmp_path):
    units = tmp_path / "systemd"
    units.mkdir()
    (units / "probe.service").write_text(
        "[Service]\nExecStart=/usr/bin/python3 /srv/af/tools/agent-self-report.py\n")
    found = svc.discover(launch_dir=tmp_path / "none", systemd_dir=units)
    assert [(s.role, s.kind) for s in found] == [("probe", "systemd")]
    calls = []
    svc.kickstart(found[0], lambda c: calls.append(c) or subprocess.CompletedProcess(c, 0))
    assert calls == [["systemctl", "--user", "start", "probe.service"]]
