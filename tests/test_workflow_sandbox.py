"""Real platform confinement evidence with an unconfined control."""
from __future__ import annotations

import json
import os
import socket
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from test_workflow_repository import OWNER, WORKSPACE
from test_workflow_test_execution import ExecutionFixture
from fleet_core.backends.directory import DirectoryBackend
from fleet_core.backends.sandbox import SandboxBackend
from hub.platform.workflow_validators import FixedTestRunner


def _macos_test_profile(workdir: Path) -> str:
    roots = {Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve(), Path(pytest.__file__).resolve().parent.parent}
    ancestors = {parent for root in (*roots, workdir) for parent in root.parents}
    runtime_rules = ''.join('(subpath ' + json.dumps(str(root)) + ')' for root in sorted(roots))
    parent_rules = ''.join('(literal ' + json.dumps(str(parent)) + ')' for parent in sorted(ancestors))
    workspace_rule = '(subpath ' + json.dumps(str(workdir)) + ')'
    return ('(version 1)(deny default)(allow process*)(allow sysctl-read)(allow mach-lookup)'
        '(allow file-read-metadata)(allow file-read-data ' + runtime_rules + parent_rules + workspace_rule +
        '(subpath "/System/Library")(subpath "/usr/lib")(literal "/dev/null"))'
        '(allow file-write* ' + workspace_rule + '(literal "/dev/null"))')


def macos_test_backend(workdir: Path) -> SandboxBackend:
    return SandboxBackend(workdir, launcher=("/usr/bin/env",
        "PYTHONPATH=" + str(Path(pytest.__file__).resolve().parent.parent), "PYTHONNOUSERSITE=1",
        "/usr/bin/sandbox-exec", "-p", _macos_test_profile(workdir)))


def isolated_test_backend(workdir: Path) -> SandboxBackend:
    if sys.platform == "darwin":
        return macos_test_backend(workdir)
    if sys.platform == "linux":
        from workflow_sandbox_runtime import linux_test_backend
        return linux_test_backend(workdir)
    raise RuntimeError("workflow sandbox acceptance requires Linux or macOS")


@pytest.mark.parametrize("confined", [False, True])
def test_fixed_test_worker_file_network_and_credential_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, confined: bool,
) -> None:
    monkeypatch.setenv("FLEET_TEST_SECRET", "synthetic-hub-credential")
    secret = tmp_path / "host-secret"
    secret.write_text("synthetic host data", encoding="utf-8")
    host_write = tmp_path / "host-write"
    namespaces = {name: os.stat("/proc/self/ns/" + name).st_ino for name in ("user", "mnt", "pid", "net")} if sys.platform == "linux" else {}
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        content = (
            "import errno,os,socket,sys\nfrom pathlib import Path\nimport pytest\n"
            "def test_environment_and_scratch():\n"
            "    assert 'FLEET_TEST_SECRET' not in os.environ\n"
            "    (Path(os.environ.get('HOME','.'))/'scratch').write_text('allowed')\n"
            "    (Path(os.environ.get('TMPDIR','.'))/'temporary').write_text('allowed')\n"
            "    if sys.platform=='linux' and " + repr(confined) + ":\n"
            "        status=dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)\n"
            "        assert all(int(status[key],16)==0 for key in ('CapEff','CapPrm','CapBnd'))\n"
            "        assert status['NoNewPrivs'].strip()=='1' and status['Seccomp'].strip()=='2'\n"
            "        host_namespaces=" + repr(namespaces) + "\n"
            "        assert all(os.stat('/proc/self/ns/'+name).st_ino!=inode for name,inode in host_namespaces.items())\n"
            "        with pytest.raises(OSError) as denied:\n"
            "            Path(__file__).with_name('forbidden-source-write').write_text('x')\n"
            "        assert denied.value.errno in {errno.EPERM,errno.EACCES,errno.EROFS}\n"
            "def network():\n"
            "    with socket.create_connection(('127.0.0.1'," + str(listener.getsockname()[1]) + "),timeout=1) as connection:\n"
            "        connection.sendall(b'probe')\n"
            "@pytest.mark.parametrize('operation',['read','write','network'])\n"
            "def test_host_boundary(operation):\n"
            "    operations={'read':lambda:Path(" + repr(str(secret)) + ").read_bytes(),\n"
            "                'write':lambda:Path(" + repr(str(host_write)) + ").write_text('synthetic probe'),\n"
            "                'network':network}\n"
            "    with pytest.raises(OSError) as denied:\n"
            "        operations[operation]()\n"
            "    allowed={errno.EPERM,errno.EACCES}\n"
            "    if sys.platform=='linux' and operation!='network':\n"
            "        allowed.update({errno.EROFS,errno.ENOENT})\n"
            "    assert denied.value.errno in allowed\n"
        )
        fixture = ExecutionFixture(tmp_path, content=content)
        fixture.dispatch()

        def backend(workdir: Path) -> DirectoryBackend:
            if not confined:
                return DirectoryBackend(workdir)
            return isolated_test_backend(workdir)

        result = fixture.worker(runner=FixedTestRunner(backend_factory=backend)).run_once(OWNER)
    assert result["state"] == "succeeded", result["result_text"]
    evidence = fixture.test_evidence()
    assert len(evidence) == 1 and evidence[0]["result"] == ("passed" if confined else "failed")
    outputs = fixture.outputs()
    report = json.loads(fixture.store.read(OWNER, WORKSPACE, outputs["test_report"]["artifact_id"]))
    assert report["passed"] is confined and report["exit_code"] == (0 if confined else 1)
    assert (report["environment"]["launcher_sha256"] is not None) is confined
    junit = ET.fromstring(fixture.store.read(OWNER, WORKSPACE, outputs["junit"]["artifact_id"]))
    cases = {case.get("name"): case for case in junit.iter("testcase")}
    assert cases["test_environment_and_scratch"].find("failure") is None
    for operation in ("read", "write", "network"):
        case = cases["test_host_boundary[" + operation + "]"]
        assert (case.find("failure") is None) is confined
        assert case.find("error") is None and case.find("skipped") is None
    assert secret.read_text(encoding="utf-8") == "synthetic host data"
    assert host_write.exists() is not confined
