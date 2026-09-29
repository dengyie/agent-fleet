"""Code rollback must preserve state written since the code snapshot."""
from pathlib import Path
import os
import shutil
import sqlite3
import subprocess

import pytest

HELPER = Path(__file__).resolve().parents[1] / 'deploy/hk-overlay-sync.sh'


@pytest.mark.parametrize('fallback', [False, True])
def test_code_rollback_preserves_live_database_and_credentials(tmp_path, fallback):
    if not fallback and not shutil.which('rsync'):
        pytest.skip('rsync unavailable')
    live, backup = tmp_path / 'live', tmp_path / 'backup'
    for dirname in ('state', 'var', 'credentials', 'hub'):
        (live / dirname).mkdir(parents=True)
    (live / 'hub/version.py').write_text('old-code')
    (live / 'hosts.yaml').write_text('old-host')
    (live / 'credentials/token').write_text('old-token')
    database = live / 'state/platform.db'
    conn = sqlite3.connect(database)
    conn.execute('CREATE TABLE data(value TEXT)')
    conn.execute("INSERT INTO data VALUES ('before')")
    conn.commit()
    override = '''command() {
      if [[ "$1" == '-v' && "$2" == rsync ]]; then return 1; fi
      builtin command "$@"
    }
''' if fallback else ''

    def run(function):
        subprocess.run(['bash', '-euc', override + '. "$1"; "$2" "$3" "$4"',
                        'rollback-test', str(HELPER), function,
                        str(live if function.endswith('backup_tree') else backup),
                        str(backup if function.endswith('backup_tree') else live)],
                       check=True, capture_output=True, text=True)

    run('fleet_overlay_backup_tree')
    conn.execute("INSERT INTO data VALUES ('after')")
    conn.commit()
    old_mtime = database.stat().st_mtime
    os.utime(database, (old_mtime + 3, old_mtime + 3))
    (live / 'hub/version.py').write_text('new-code-version')
    (live / 'hub/new-file.py').write_text('new')
    (live / 'hosts.yaml').write_text('current-host')
    (live / 'credentials/token').write_text('rotated-token')
    (live / 'var/runtime.log').write_text('new runtime log')
    live_inode = live.stat().st_ino
    database_inode = database.stat().st_ino
    run('fleet_overlay_restore_tree')
    conn.close()
    with sqlite3.connect(database) as reader:
        assert reader.execute('SELECT value FROM data').fetchall() == [('before',), ('after',)]
    assert database.stat().st_ino == database_inode
    assert (live / 'credentials/token').read_text() == 'rotated-token'
    assert (live / 'hosts.yaml').read_text() == 'current-host'
    assert (live / 'var/runtime.log').read_text() == 'new runtime log'
    assert (live / 'hub/version.py').read_text() == 'old-code'
    assert not (live / 'hub/new-file.py').exists()
    assert live.stat().st_ino == live_inode


def test_rollback_starts_previous_cli_with_captured_argv(tmp_path):
    """The old parser rejects the new flag; restoration must use its real argv."""
    import re
    import sys
    import time
    import urllib.request
    import socket
    import shlex

    source = (HELPER.parent / 'hk-container-install.sh').read_text()
    start = re.search(r'^start_hub\(\) \{.*?^\}', source, re.M | re.S).group()
    rollback = re.search(r'^rollback\(\) \{.*?^\}', source, re.M | re.S).group()
    live = tmp_path / 'live with spaces'
    (live / 'hub').mkdir(parents=True)
    # Legacy supported launch contract, plus a genuine HTTP listener.
    (live / 'hub/web.py').write_text('''import argparse, http.server, os
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('--host'); p.add_argument('--port', type=int)
p.add_argument('--frontend-cutover', action='store_true')
a = p.parse_args()
Path('listener.pid').write_text(str(os.getpid()))
http.server.HTTPServer((a.host, a.port), http.server.SimpleHTTPRequestHandler).serve_forever()
''')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    argv = [sys.executable, 'hub/web.py', '--host', '127.0.0.1', '--port', str(port), '--frontend-cutover']
    old = subprocess.Popen(argv, cwd=live, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            if (live / 'listener.pid').exists(): break
            time.sleep(.02)
        old.terminate(); old.wait(timeout=5)
        (live / 'listener.pid').unlink()
        failed = subprocess.run([*argv[:-1], '--no-serve-frontend'], cwd=live, capture_output=True)
        assert failed.returncode == 2
        script = start + '\n' + rollback + '''
# Replace privilege/proc adapters only; use the production launch and rollback.
su() { bash -c "$4" >/dev/null 2>&1; }
chown() { :; }
hub_pid_for_cwd() { [[ -s "$1/listener.pid" ]] && cat "$1/listener.pid"; }
rollback_live() { :; }
wait_for_status() { :; }
old_stopped=1; switched=1; probe_stopped=0; new_pid=""
false
rollback
'''
        settings = {'live':str(live), 'log_file':str(tmp_path/'web.log'),
                    'pid_file':str(tmp_path/'hub.pid'), 'web_host':'127.0.0.1',
                    'web_port':str(port), 'FLEET_USER':'fixture', 'old_pid':str(old.pid)}
        prelude = '\n'.join(k+'='+shlex.quote(v) for k,v in settings.items())
        prelude += '\nold_argv=(' + ' '.join(map(shlex.quote, argv)) + ')\n'
        result = subprocess.run(['bash','-uc',prelude+script], capture_output=True, text=True, timeout=15)
        assert result.returncode == 1, result.stderr
        for _ in range(100):
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=.2) as response:
                    assert response.status == 200
                break
            except OSError: time.sleep(.02)
        else: pytest.fail('restored legacy listener never became healthy')
        assert (tmp_path/'hub.pid').read_text().strip() == (live/'listener.pid').read_text()
    finally:
        if old.poll() is None: old.terminate(); old.wait(timeout=5)
        if (live/'listener.pid').exists():
            import signal
            try: os.kill(int((live/'listener.pid').read_text()), signal.SIGTERM)
            except ProcessLookupError: pass
