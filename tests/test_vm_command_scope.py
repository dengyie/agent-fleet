"""Direct VM command lifecycle includes detached sessions on the Linux VM."""
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from fleet_core.backends.vm import VMBackend


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux cgroup lifecycle; Windows uses Job Object')
@pytest.mark.parametrize('ending', ['success', 'timeout', 'cancel'])
@pytest.mark.parametrize('double_fork', [False, True])
def test_detached_vm_descendant_cannot_write_after_receipt(tmp_path, ending, double_fork):
    child = ('import time; from pathlib import Path; '
             'time.sleep(1); Path("late").write_text("escaped"); time.sleep(5)')
    if double_fork:
        child = ('import os; from pathlib import Path; '
                 'pid=os.fork(); '
                 'os._exit(0) if pid else None; os.setsid(); '
                 'Path("detached-pid").write_text(str(os.getpid())); ' + child)
    parent = ('import subprocess,sys,time; from pathlib import Path; '
              'p=subprocess.Popen([sys.executable,"-c",' + repr(child) + '], '
              'start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); '
              'Path("child-pid").write_text(str(p.pid)); ' +
              ('\nwhile not Path("detached-pid").exists(): time.sleep(.005)\n' if double_fork else '') +
              ('time.sleep(5)' if ending != 'success' else 'sys.exit(0)'))
    start = time.monotonic()
    receipt = VMBackend(tmp_path).execute('detached', [sys.executable, '-c', parent],
        timeout_s=.4 if ending == 'timeout' else 5,
        should_cancel=(lambda: time.monotonic() - start > .4) if ending == 'cancel' else None)
    pid_file = tmp_path / 'child-pid'
    try:
        assert pid_file.exists(), receipt
        assert receipt.state == {'success': 'succeeded', 'timeout': 'failed', 'cancel': 'cancelled'}[ending]
        time.sleep(1.1)
        assert not (tmp_path / 'late').exists()
        if double_fork:
            assert (tmp_path / 'detached-pid').exists()
        raw = Path('/proc/' + pid_file.read_text() + '/stat')
        assert not raw.exists() or raw.read_text().split(')')[-1].split()[0] == 'Z'
    finally:
        # The pre-fix regression deliberately creates an escaping process.
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), 9)
            except ProcessLookupError:
                pass
        if (tmp_path / 'detached-pid').exists():
            try:
                os.kill(int((tmp_path / 'detached-pid').read_text()), 9)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux delegation contract')
def test_missing_delegation_never_spawns_vm_command(tmp_path, monkeypatch):
    monkeypatch.setenv('FLEET_COMMAND_CGROUP_ROOT', str(tmp_path / 'missing'))
    receipt = VMBackend(tmp_path).execute('no-scope', [sys.executable, '-c',
        'from pathlib import Path; Path("spawned").write_text("bad")'])
    assert receipt.state == 'failed' and receipt.error_code == 'command_scope_unavailable'
    assert not (tmp_path / 'spawned').exists()
    assert not VMBackend.command_available()


def test_scope_never_changes_unrelated_process_lifecycle(tmp_path):
    from fleet_core.process.local_process import popen
    if not VMBackend.command_available():
        pytest.skip('direct VM command ownership unavailable on this host')
    with popen([sys.executable, '-c', 'import time; time.sleep(5)'],
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as unrelated:
        try:
            receipt = VMBackend(tmp_path).execute('only-owned', [sys.executable, '-c', 'print("ok")'])
            assert receipt.state == 'succeeded'
            assert unrelated.poll() is None
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=2)
