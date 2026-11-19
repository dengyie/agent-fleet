"""Per-conversation model routing; never rewrites global Agent settings.

Claude settings precedence and model aliases follow Magpie's Agent wiring.
Codex resumes retain their original provider, so override that exact provider
for this invocation instead of creating a different, unused provider default.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from fleet_core.session_models import validate_model_delivery

_CLAUDE_CLOUD = ('BEDROCK', 'VERTEX', 'FOUNDRY', 'ANTHROPIC_AWS', 'ANTHROPIC_GOOGLE_CLOUD', 'MANTLE')
_CLAUDE_MODELS = ('ANTHROPIC_MODEL', 'ANTHROPIC_SMALL_FAST_MODEL', 'CLAUDE_CODE_SUBAGENT_MODEL',
                 *(f'ANTHROPIC_DEFAULT_{tier}_MODEL' for tier in ('OPUS', 'SONNET', 'HAIKU', 'FABLE')))


def _without_options(options, names):
    result, index = [], 0
    while index < len(options):
        option = options[index]
        if option.split('=', 1)[0] in names:
            index += 1 if '=' in option else 2
        else:
            result.append(option)
            index += 1
    return result


def _private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.model-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _claude_settings(options, cwd):
    """Preserve explicit settings while replacing routing keys only."""
    settings = {}
    for index, option in enumerate(options):
        if option == '--settings' and index + 1 < len(options):
            raw = options[index + 1]
        elif option.startswith('--settings='):
            raw = option.split('=', 1)[1]
        else:
            continue
        try:
            value = json.loads(raw if raw.lstrip().startswith('{') else (Path(cwd) / raw).read_text(encoding='utf-8'))
            if not isinstance(value, dict):
                raise ValueError
            if 'env' in value and (not isinstance(value['env'], dict)
                    or not all(isinstance(k, str) and isinstance(v, str) for k, v in value['env'].items())):
                raise ValueError
            settings.update(value)
        except (ValueError, OSError):
            raise ValueError('native_settings_unreadable') from None
    return settings


def _native_paths(entry):
    token = entry.resume_token
    if not token or not all(c.isalnum() or c in '_-' for c in token):
        return []
    if entry.native_file_path:
        return [Path(entry.native_file_path)]
    if entry.manifest.agent == 'codex':
        root = Path((entry.env or {}).get('CODEX_HOME') or Path.home() / '.codex')
        return [path for directory in ('sessions', 'archived_sessions')
                for path in (root / directory).glob(f'**/*-{token}.jsonl')]
    root = Path((entry.env or {}).get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')
    return list((root / 'projects').glob(f'**/{token}.jsonl'))


def original_model(entry):
    """Pin the pre-switch model too: native resume remembers the changed model."""
    if entry.restore_model:
        return entry.restore_model
    for path in _native_paths(entry):
        try:
            with path.open('rb') as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - (2 << 20)))
                lines = stream.read().decode('utf-8', errors='replace').splitlines()
            for line in reversed(lines):
                try:
                    row = json.loads(line)
                    value = (row.get('payload', {}).get('model') if row.get('type') == 'turn_context'
                             else row.get('message', {}).get('model') if row.get('type') == 'assistant' else None)
                    if (isinstance(value, str) and 0 < len(value) <= 256 and not value.startswith('<')
                            and not any(ord(c) < 32 for c in value)):
                        return value
                except (ValueError, AttributeError):
                    continue
        except OSError:
            continue
    options = entry.resume_options
    for index in range(len(options) - 1, -1, -1):
        option = options[index]
        if option in {'-m', '--model'} and index + 1 < len(options):
            return options[index + 1]
        if option.startswith(('--model=', '-m=')):
            return option.split('=', 1)[1]
    return (entry.env or {}).get('ANTHROPIC_MODEL')


def codex_provider(entry):
    """Resolve the exact saved conversation's provider; never guess openai."""
    for path in _native_paths(entry):
        try:
            with path.open(encoding='utf-8') as stream:
                row = json.loads(stream.readline(1 << 20))
            meta = row.get('payload', {})
            provider = meta.get('model_provider')
            if (row.get('type') == 'session_meta' and meta.get('id') == entry.resume_token
                    and isinstance(provider, str) and 0 < len(provider) <= 128
                    and all(c.isalnum() or c in '_-' for c in provider)):
                return provider
        except (OSError, ValueError, AttributeError):
            continue
    raise ValueError('native_provider_unbound')


def effective_model_config(entry, private_root):
    """Return invocation-only options/env, preserving the original snapshot."""
    env, options = dict(entry.env or {}), list(entry.resume_options)
    value = entry.model_config
    if value is None:
        if entry.restore_model:
            options = _without_options(options, {'-m', '--model'}) + ['--model', entry.restore_model]
        return options, env
    cfg = validate_model_delivery(value, entry.manifest.agent)
    if cfg['mode'] == 'original':
        return options, env
    model = cfg['model']
    if entry.manifest.agent == 'claude_code':
        routing = {'ANTHROPIC_BASE_URL': cfg['base_url'].rstrip('/'),
                   'ANTHROPIC_AUTH_TOKEN': cfg['api_key'], 'ANTHROPIC_API_KEY': '',
                   'CLAUDE_CODE_OAUTH_TOKEN': '', 'ANTHROPIC_CUSTOM_HEADERS':
                       '\n'.join(f'{k}: {v}' for k, v in cfg['headers'].items())}
        routing.update({key: model for key in _CLAUDE_MODELS})
        routing.update({f'CLAUDE_CODE_USE_{name}': '0' for name in _CLAUDE_CLOUD})
        settings = _claude_settings(options, entry.cwd)
        settings['env'] = {**settings.get('env', {}), **routing}
        settings.update(model=model, apiKeyHelper='')
        path = Path(private_root) / 'model-settings' / (entry.manifest.session_id + '.json')
        _private_json(path, settings)
        env.update(routing)
        options = _without_options(options, {'--model', '--fallback-model', '--settings'})
        options.extend(['--settings', str(path), '--model', model])
    elif entry.manifest.agent == 'codex':
        provider = codex_provider(entry)
        quote = lambda s: json.dumps(s, ensure_ascii=True)
        table = {'name': 'Agent Fleet', 'base_url': cfg['base_url'].rstrip('/'),
                 'env_key': 'AGENT_FLEET_MODEL_API_KEY', 'wire_api': 'responses',
                 'requires_openai_auth': False}
        fields = [f'{k} = {quote(v) if isinstance(v, str) else "false"}' for k, v in table.items()]
        fields.append('http_headers = {' + ', '.join(f'{quote(k)} = {quote(v)}' for k, v in cfg['headers'].items()) + '}')
        options = _without_options(options, {'-m', '--model'})
        options.extend(['-c', 'model_provider=' + quote(provider), '-c',
            'model_providers.' + quote(provider) + '={' + ', '.join(fields) + '}', '--model', model])
        env['AGENT_FLEET_MODEL_API_KEY'] = cfg['api_key']
    return options, env
