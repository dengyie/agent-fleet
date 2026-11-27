"""Actual storage layouts must reach the independent archive worker."""
import json
from pathlib import Path

import pytest

from tests.test_agy_native_history import _native, _events, _post
from tests.test_review_node_sync import _cfg, _client


@pytest.fixture(autouse=True)
def clean_roots(monkeypatch):
    for key in ('CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'PI_CODING_AGENT_DIR',
                'ZCODE_DATA_BASE_DIR', 'GEMINI_HOME', 'HERMES_HOME'):
        monkeypatch.delenv(key, raising=False)


def test_windows_agy_namespace_is_imported_by_default_worker(tmp_path, monkeypatch):
    from tools.session.sync import NativeArchiveSync
    _native(tmp_path, monkeypatch, namespace='antigravity')
    cfg, client = _cfg(tmp_path), _client(tmp_path)
    worker = NativeArchiveSync(cfg, send=_post(client))
    worker.scan_once()
    session, events = _events(client)
    assert session['native_title'] == '本机原生会话名'
    assert any(e['payload'].get('text') == 'native reply' for e in events)
    original = [e['event_id'] for e in events]
    worker.scan_once()
    assert [e['event_id'] for e in _events(client)[1]] == original


@pytest.mark.parametrize(('family', 'variable', 'suffix'), [
    ('codex', 'CODEX_HOME', 'sessions'),
    ('claude_code', 'CLAUDE_CONFIG_DIR', 'projects'),
    ('pi', 'PI_CODING_AGENT_DIR', 'sessions'),
    ('zcode', 'ZCODE_DATA_BASE_DIR', '.zcode/cli/rollout'),
    ('agy', 'GEMINI_HOME', 'antigravity/conversations'),
    ('agy', 'GEMINI_HOME', 'antigravity-cli/conversations'),
])
def test_explicit_native_roots_replace_defaults(tmp_path, monkeypatch, family, variable, suffix):
    from agent_profiles import PROFILES
    from tools.session.sync import default_sources
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    default = Path(PROFILES[family].native_session_root).expanduser()
    default.mkdir(parents=True)
    root = tmp_path / '用户 数据' / family
    expected = root / suffix
    expected.mkdir(parents=True)
    monkeypatch.setenv(variable, str(root))
    observed = [path for name, path in default_sources() if name == family]
    assert expected in observed
    assert default not in observed


def test_agy_both_observed_namespaces_have_independent_exact_sources(tmp_path, monkeypatch):
    from tools.session.sync import default_sources
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    roots = [tmp_path / '.gemini' / name / 'conversations'
             for name in ('antigravity', 'antigravity-cli')]
    for path in roots:
        path.mkdir(parents=True)
    assert {path for family, path in default_sources() if family == 'agy'} == set(roots)


def test_cli_agy_source_remains_compatible(tmp_path, monkeypatch):
    from tools.session import sync
    path = _native(tmp_path)
    cfg, client = _cfg(tmp_path), _client(tmp_path)
    real = sync.sync_file
    monkeypatch.setattr(sync, 'load_config', lambda _: cfg)
    monkeypatch.setattr(sync, 'post', _post(client))
    monkeypatch.setattr(sync, 'sync_file', lambda cfg, family, path: real(cfg, family, path, _post(client)))
    assert sync.main(['--config', 'fixture', '--source', 'agy=' + str(path.parent), '--once']) == 0
    assert json.dumps(_events(client)[0])


def test_codex_archive_move_preserves_existing_session_and_delivery_cursor(tmp_path, monkeypatch):
    from tools.session.sync import NativeArchiveSync, native_identity
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    home = tmp_path / '.codex'
    active = home / 'sessions/2026/11/28/rollout-2026-11-28T01-02-03-native_fixture.jsonl'
    active.parent.mkdir(parents=True)
    active.write_text(json.dumps({'type':'event_msg','payload':{'type':'user_message','message':'before archive'}})+'\n', encoding='utf-8')
    cfg, client = _cfg(tmp_path), _client(tmp_path)
    worker = NativeArchiveSync(cfg, send=_post(client));worker.scan_once()
    before = client.get('/api/sessions').get_json()['sessions']
    assert len(before)==1
    sid = before[0]['session_id']
    events = client.get('/api/sessions/'+sid+'/events').get_json()['events']
    archived = home / 'archived_sessions' / active.name
    archived.parent.mkdir()
    identity = native_identity('codex', active)
    active.rename(archived)
    assert native_identity('codex', archived)==identity
    with archived.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'type':'event_msg','payload':{'type':'agent_message','message':'after archive'}})+'\n')
    worker.scan_once()
    assert len(client.get('/api/sessions').get_json()['sessions'])==1
    after = client.get('/api/sessions/'+sid+'/events').get_json()['events']
    assert all(e in after for e in events)
    assert sum(e['payload'].get('text')=='before archive' for e in after)==1
    assert any(e['payload'].get('text')=='after archive' for e in after)
