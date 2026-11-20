"""Cross-platform per-conversation model configuration and original restoration."""
import json
import sys

from tools.supervisor.supervisor import Supervisor


def finished_claude(tmp_path, *, machine='win', session_id=None):
    sv = Supervisor(tmp_path / 'manifests', machine)
    m = sv.launch(agent='claude_code', command=[sys.executable, '-c',
        'print(\'{"type":"system","session_id":"original-native-id"}\')'],
        cwd=str(tmp_path), env_allowlist={'ANTHROPIC_AUTH_TOKEN': 'old-secret', 'ANTHROPIC_MODEL': 'old-model'}, session_id=session_id)
    sv.wait_session(m.session_id, timeout_s=5)
    return sv, m.session_id


def route():
    return {'mode': 'catalog', 'profile_id': 'working', 'digest': 'a' * 64,
            'family': 'claude_code', 'model': 'new-model',
            'base_url': 'https://gateway.example', 'api_key': 'new-secret', 'headers': {}}


def test_takeover_persists_without_replacing_original_identity_or_secret(tmp_path):
    sv, sid = finished_claude(tmp_path)
    assert sv.configure_model(sid, route()) == 'model_configured'
    other = Supervisor(tmp_path / 'manifests', 'win')
    other.get(sid)
    entry = other._entries[sid]
    m, handle, argv, cwd, env = other._prepare_user_turn(sid, 'continue once', entry)
    assert cwd == str(tmp_path)
    assert argv[argv.index('--resume') + 1] == 'original-native-id'
    assert argv[argv.index('--model') + 1] == 'new-model'
    assert env['ANTHROPIC_AUTH_TOKEN'] == 'new-secret'
    assert env['ANTHROPIC_BASE_URL'] == 'https://gateway.example'
    assert entry.env['ANTHROPIC_AUTH_TOKEN'] == 'old-secret'
    assert 'new-secret' not in ' '.join(argv)
    assert 'new-secret' not in (tmp_path / 'manifests' / (sid + '.json')).read_text()
    assert other.configure_model(sid, {'mode': 'original'}) == 'model_restored'
    assert other._prepare_user_turn(sid, 'continue', entry)[-1]['ANTHROPIC_AUTH_TOKEN'] == 'old-secret'
    restored = other._prepare_user_turn(sid, 'continue', entry)[2]
    assert restored[restored.index('--model') + 1] == 'old-model'


def test_configure_busy_and_failed_store_preserve_original(tmp_path, monkeypatch):
    sv, sid = finished_claude(tmp_path)
    entry = sv._entries[sid]
    entry.resume_pending = object()
    assert sv.configure_model(sid, route()) == 'session_busy'
    entry.resume_pending = None
    before = sv._continuations.path(sid).read_bytes()
    monkeypatch.setattr(sv._continuations, 'save', lambda _: False)
    assert sv.configure_model(sid, route()) == 'model_config_unavailable'
    assert entry.model_config is None and sv._continuations.path(sid).read_bytes() == before


def test_claude_override_clears_old_routing_but_retains_user_settings(tmp_path):
    sv, sid = finished_claude(tmp_path)
    entry = sv._entries[sid]
    entry.resume_options = ['--model', 'old-model', '--fallback-model=old-fallback', '--settings',
        json.dumps({'permissions': {'deny': ['Bash(rm *)']}, 'env': {'CUSTOM': 'kept', 'CLAUDE_CODE_USE_VERTEX': '1'}})]
    sv._continuations.save(entry)
    assert sv.configure_model(sid, route()) == 'model_configured'
    _, _, argv, _, env = sv._prepare_user_turn(sid, 'continue', entry)
    settings = json.loads(__import__('pathlib').Path(argv[argv.index('--settings') + 1]).read_text())
    assert settings['permissions'] == {'deny': ['Bash(rm *)']}
    assert settings['env']['CUSTOM'] == 'kept'
    assert settings['env']['CLAUDE_CODE_USE_VERTEX'] == '0'
    assert settings['env']['ANTHROPIC_DEFAULT_HAIKU_MODEL'] == 'new-model'
    assert env['ANTHROPIC_API_KEY'] == '' and '--fallback-model=old-fallback' not in argv


def test_codex_overrides_saved_provider_without_changing_thread(tmp_path):
    from tools.supervisor.model_config import effective_model_config
    sv, sid = finished_claude(tmp_path)
    entry = sv._entries[sid]
    entry.manifest.agent = 'codex'
    native = tmp_path / 'rollout-original-native-id.jsonl'
    native.write_text(json.dumps({'type': 'session_meta', 'payload': {
        'id': entry.resume_token, 'model_provider': 'original-provider'}}) + '\n')
    entry.native_file_path = str(native)
    entry.model_config = {**route(), 'family': 'codex', 'base_url': 'https://api.example/v1'}
    entry.resume_options = ['-c', 'approval_policy="never"', '-m', 'old-model']
    options, env = effective_model_config(entry, sv._continuations.root)
    assert 'model_provider="original-provider"' in options
    assert any('model_providers."original-provider"=' in s and 'requires_openai_auth = false' in s for s in options)
    assert 'approval_policy="never"' in options and 'old-model' not in options
    assert env['AGENT_FLEET_MODEL_API_KEY'] == 'new-secret'
    assert 'new-secret' not in json.dumps(options)


def test_multiple_switches_keep_the_original_model_and_fail_closed_on_corruption(tmp_path):
    sv, sid = finished_claude(tmp_path)
    assert sv.configure_model(sid, route()) == 'model_configured'
    assert sv.configure_model(sid, {**route(), 'model': 'second-model'}) == 'model_configured'
    entry = sv._entries[sid]
    assert entry.restore_model == 'old-model'
    assert sv.configure_model(sid, {'mode': 'original'}) == 'model_restored'
    assert entry.restore_model == 'old-model'
    path = sv._continuations.path(sid)
    data = json.loads(path.read_text())
    data['model_config'] = {'mode': 'catalog', 'api_key': 'corrupted'}
    path.write_text(json.dumps(data), encoding='utf-8')
    assert sv._prepare_user_turn(sid, 'continue', entry) == 'model_config_unavailable'
