"""Symlink-mode cutover must snapshot runtime state only after the old hub stops."""
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL = REPO_ROOT / 'deploy' / 'hk-container-install.sh'
HELPER = REPO_ROOT / 'deploy' / 'hk-overlay-sync.sh'


def _main_flow(source):
    return source[source.index('trap rollback ERR'):]


def test_symlink_snapshot_is_refreshed_after_old_hub_stop_and_before_start():
    flow = _main_flow(INSTALL.read_text())
    stop = flow.index('old_stopped=1')
    refresh = flow.index('refresh_symlink_runtime_snapshot')
    start = flow.index('start_hub "$live"')
    assert flow.index('switch_live') < stop < refresh < start
    # The refresh is limited to symlink mode; overlay keeps the live runtime tree.
    guard = flow.rindex('if [[ "$FLEET_LIVE_MODE" == symlink ]]; then', 0, refresh)
    assert stop < guard < refresh


def test_refresh_replaces_seed_with_quiesced_runtime_and_keeps_new_token(tmp_path):
    source = INSTALL.read_text()
    refresh = re.search(r'^refresh_symlink_runtime_snapshot\(\) \{.*?^\}', source, re.M | re.S).group()
    previous = tmp_path / 'previous-release'
    backup = tmp_path / 'agent-fleet.previous-r1'
    release = tmp_path / 'releases' / 'r1'
    (previous / 'state').mkdir(parents=True)
    (previous / 'credentials').mkdir()
    (previous / 'var').mkdir()
    backup.symlink_to(previous)
    # Writes committed by the old hub after the pre-switch seed copy.
    (previous / 'state' / 'meta.db').write_text('committed-after-seed')
    (previous / 'credentials' / 'operator-token').write_text('operator')
    (previous / 'credentials' / 'ingest-token').write_text('old-token')
    (previous / 'var' / 'log.jsonl').write_text('late')
    (previous / 'hosts.yaml').write_text('hosts: new')
    # Seed copied while the old hub was still live, plus the refreshed token.
    (release / 'state').mkdir(parents=True)
    (release / 'credentials').mkdir()
    (release / 'hub').mkdir()
    (release / 'hub' / 'web.py').write_text('code')
    (release / 'state' / 'meta.db').write_text('seed')
    (release / 'state' / 'meta.db-wal').write_text('stale-wal')
    (release / 'credentials' / 'ingest-token').write_text('new-token')
    (release / 'hosts.yaml').write_text('hosts: seed')
    script = '\n'.join((
        'set -Eeuo pipefail',
        f'. {str(HELPER)!r}',
        refresh,
        f'release={str(release)!r}; backup={str(backup)!r}; FLEET_USER=fixture',
        'chown() { :; }',
        'refresh_symlink_runtime_snapshot',
    ))
    proc = subprocess.run(['bash', '-c', script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert (release / 'state' / 'meta.db').read_text() == 'committed-after-seed'
    assert not (release / 'state' / 'meta.db-wal').exists()
    assert (release / 'var' / 'log.jsonl').read_text() == 'late'
    assert (release / 'hosts.yaml').read_text() == 'hosts: new'
    assert (release / 'credentials' / 'operator-token').read_text() == 'operator'
    assert (release / 'credentials' / 'ingest-token').read_text() == 'new-token'
    assert (release / 'credentials' / 'ingest-token').stat().st_mode & 0o777 == 0o600
    assert not (release / '.ingest-token.staged').exists()
    assert (release / 'hub' / 'web.py').read_text() == 'code'
    # The quiesced previous tree is the rollback source and stays untouched.
    assert (previous / 'credentials' / 'ingest-token').read_text() == 'old-token'
