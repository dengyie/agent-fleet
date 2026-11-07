import json
import os
from pathlib import Path
import time

import pytest

from tools.plugins.github_cli import GitHubCliPlugin
from fleet_core.locking import OwnedLock


from support.fake_gh import fake_gh  # noqa: F401


@pytest.fixture
def plugin(tmp_path, fake_gh, monkeypatch):
    for key in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN'):
        monkeypatch.delenv(key, raising=False)
    plugin = GitHubCliPlugin(tmp_path / 'state', {
        'executable': str(fake_gh),
        'contexts': [{'context_id': 'default', 'config_dir': str(tmp_path / 'gh-config')}],
    })
    yield plugin
    plugin.close()


def execute(plugin, action, **arguments):
    return plugin.execute(action, {'context_id': 'default', 'hostname': 'github.com', **arguments},
                          command_id='plugin_test')


def wait_job(plugin, login_id='plugin_test', states=('succeeded',), timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = execute(plugin, 'login_status', login_id=login_id)
        if value['state'] in states:
            return value
        time.sleep(0.05)
    raise AssertionError(value)


def test_status_accounts_and_no_cli_secrets(plugin):
    result = execute(plugin, 'status')
    auth = result['auth']
    assert auth['state'] == 'authenticated'
    assert auth['active_account'] == 'alice'
    assert len(auth['accounts']) == 2
    assert 'sensitive-value' not in json.dumps(result)
    assert result['context']['config_dir'].endswith('gh-config')


def test_json_exit_zero_with_auth_errors_is_not_success(plugin):
    root = Path(plugin.contexts['default'].config_dir)
    root.mkdir()
    (root / 'invalid').write_text('')
    assert execute(plugin, 'status')['auth']['state'] == 'authentication_failed'


@pytest.mark.parametrize('inherited_host', [None, 'github.com', 'other.example.com'])
def test_empty_context_has_no_phantom_account(plugin, monkeypatch, inherited_host):
    if inherited_host is None:
        monkeypatch.delenv('GH_HOST', raising=False)
    else:
        monkeypatch.setenv('GH_HOST', inherited_host)
    root = Path(plugin.contexts['default'].config_dir)
    root.mkdir()
    (root / 'empty-config').write_text('')
    auth = execute(plugin, 'status')['auth']
    assert auth['state'] == 'not_logged_in'
    assert auth['accounts'] == []
    assert auth['active_account'] is None


def test_switch_logout_and_repository_permissions(plugin):
    assert execute(plugin, 'switch', username='bob')['auth']['active_account'] == 'bob'
    after_logout = execute(plugin, 'logout', username='bob')['auth']
    assert after_logout['active_account'] == 'alice'
    assert not any(a['login'] == 'bob' for a in after_logout['accounts'])
    result = execute(plugin, 'repo_check', repository='owner/repo')
    assert result['repository']['permissions']['push'] is True
    assert 'do-not-export' not in json.dumps(result)


def test_environment_override_is_explicit_and_prevents_ineffective_switch(plugin, monkeypatch):
    monkeypatch.setenv('GH_TOKEN', 'not-a-real-token')
    assert execute(plugin, 'status')['auth']['environment_source'] == 'GH_TOKEN'
    for action, args in [('switch', {'username': 'bob'}), ('logout', {'username': 'bob'}), ('login_start', {})]:
        with pytest.raises(Exception) as exc:
            execute(plugin, action, **args)
        assert exc.value.code == 'environment_override'


def test_missing_cli_is_dependency_state(tmp_path):
    plugin = GitHubCliPlugin(tmp_path, {'executable': str(tmp_path / 'not-installed')})
    assert plugin.manifest()['available'] is False
    with pytest.raises(Exception) as exc:
        execute(plugin, 'status')
    assert exc.value.code == 'dependency_missing'
    plugin.close()


def test_login_device_progress_completion_and_restart(plugin):
    result = execute(plugin, 'login_start')
    assert result['login_id'] == 'plugin_test'
    waiting = wait_job(plugin, states=('waiting_authorization',))
    assert waiting['user_code'] == 'ABCD-1234'
    assert waiting['verification_uri'] == 'https://github.com/login/device'
    with pytest.raises(Exception) as exc:
        execute(plugin, 'switch', username='bob')
    assert exc.value.code == 'context_busy'
    Path(plugin.contexts['default'].config_dir, 'authorize').write_text('')
    assert wait_job(plugin)['state'] == 'succeeded'
    plugin.close()
    restarted = GitHubCliPlugin(plugin.state_directory, plugin.config)
    try:
        assert execute(restarted, 'login_status', login_id='plugin_test')['state'] == 'succeeded'
        assert execute(restarted, 'status')['auth']['active_account'] == 'new-user'
    finally:
        restarted.close()


def test_login_preserves_existing_git_protocol(plugin):
    root = Path(plugin.contexts['default'].config_dir)
    root.mkdir()
    (root / 'git-protocol').write_text('ssh')
    execute(plugin, 'login_start')
    wait_job(plugin, states=('waiting_authorization',))
    assert (root / 'login-protocol').read_text() == 'ssh'


def test_login_cancel_and_parent_loss_reap_owned_process(plugin):
    execute(plugin, 'login_start')
    wait_job(plugin, states=('waiting_authorization',))
    execute(plugin, 'login_cancel', login_id='plugin_test')
    assert wait_job(plugin, states=('cancelled',))['state'] == 'cancelled'
    pid = int(Path(plugin.contexts['default'].config_dir, 'login-started').read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_login_worker_exits_cleanly_while_parent_keeps_stdin_open(plugin):
    execute(plugin, 'login_start')
    wait_job(plugin, states=('waiting_authorization',))
    worker = plugin._workers['plugin_test']
    Path(plugin.contexts['default'].config_dir, 'authorize').write_text('')
    assert wait_job(plugin)['state'] == 'succeeded'
    deadline = time.monotonic() + 8
    while worker.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    # stdin is never closed by the parent here; a daemon thread parked in
    # BufferedReader.read() used to hold its lock through interpreter shutdown
    # and abort the worker with SIGABRT (-6 / exit 134).
    assert worker.poll() == 0, f'login worker exited {worker.poll()}'


def test_closing_node_ends_pending_login_and_preserves_result(plugin):
    execute(plugin, 'login_start')
    wait_job(plugin, states=('waiting_authorization',))
    plugin.close()
    assert wait_job(plugin, states=('unknown',))['error_code'] == 'node_stopped'
    pid = int(Path(plugin.contexts['default'].config_dir, 'login-started').read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_context_mutations_wait_for_previous_worker_cleanup(plugin):
    # A restarted node may have marked the old job unknown while its worker
    # still owns gh. No new auth mutation may race that cleanup.
    lock = OwnedLock(plugin.state_directory / 'context-default.lock', purpose='test-cleanup').acquire()
    try:
        for action, args in [('login_start', {}), ('switch', {'username': 'bob'}), ('logout', {'username': 'bob'})]:
            with pytest.raises(Exception) as exc:
                execute(plugin, action, **args)
            assert exc.value.code == 'context_busy'
        assert execute(plugin, 'status')['auth']['active_account'] == 'alice'
    finally:
        lock.release()


@pytest.mark.parametrize('marker,expected', [('unsupported', 'cli_upgrade_required'),
    ('unreachable', 'network_unreachable'), ('json-network-error', 'network_unreachable'),
    ('malformed', 'invalid_cli_response')])
def test_cli_failure_modes_are_distinct(plugin, marker, expected):
    root = Path(plugin.contexts['default'].config_dir)
    root.mkdir()
    (root / marker).write_text('fixture')
    with pytest.raises(Exception) as exc:
        execute(plugin, 'status')
    assert exc.value.code == expected


def test_repository_invalid_json_shape_is_a_typed_error(plugin):
    root = Path(plugin.contexts['default'].config_dir)
    root.mkdir()
    (root / 'invalid-repo').write_text('fixture')
    with pytest.raises(Exception) as exc:
        execute(plugin, 'repo_check', repository='owner/repo')
    assert exc.value.code == 'invalid_cli_response'


def test_not_logged_in_is_a_valid_observation(plugin):
    root = Path(plugin.contexts['default'].config_dir)
    root.mkdir()
    (root / 'not-logged-in').write_text('fixture')
    assert execute(plugin, 'status')['auth']['state'] == 'not_logged_in'


def test_contexts_are_distinct_and_unknown_context_rejected(tmp_path, fake_gh):
    plugin = GitHubCliPlugin(tmp_path / 'state', {'executable': str(fake_gh), 'contexts': [
        {'context_id': 'a', 'config_dir': str(tmp_path / 'a')},
        {'context_id': 'b', 'config_dir': str(tmp_path / 'b')}]})
    try:
        plugin.execute('switch', {'context_id': 'a', 'username': 'bob'}, command_id='a')
        assert plugin.execute('status', {'context_id': 'b'}, command_id='b')['auth']['active_account'] == 'alice'
        with pytest.raises(Exception):
            execute(plugin, 'status')
    finally:
        plugin.close()
