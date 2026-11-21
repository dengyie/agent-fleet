import json
import os
from pathlib import Path

import pytest
import yaml

from tools.hermes_workspace import workspace_environment


def test_hermes_private_home_pins_task_workdir_without_modifying_user_config(tmp_path):
    home = tmp_path / 'home'
    source = home / '.hermes'
    source.mkdir(parents=True)
    value = {'model': {'default': 'configured-model', 'provider': 'configured-provider'},
             'terminal': {'cwd': '/unrelated/project', 'backend': 'ssh'}}
    config = source / 'config.yaml'
    config.write_text(yaml.safe_dump(value), encoding='utf-8')
    (source / '.env').write_text('OPENAI_API_KEY=fixture-secret\n', encoding='utf-8')
    (source / 'auth.json').write_text(json.dumps({'fixture': 'private-auth'}), encoding='utf-8')
    before = config.read_bytes()
    work = tmp_path / 'authorized'
    work.mkdir()
    env = workspace_environment({'HOME': str(home)}, work)
    scoped = Path(env['HERMES_HOME'])
    actual = yaml.safe_load((scoped / 'config.yaml').read_text(encoding='utf-8'))
    assert actual['terminal']['cwd'] == str(work.resolve())
    assert actual['terminal']['backend'] == 'local'
    assert actual['model'] == value['model']
    assert config.read_bytes() == before
    assert (scoped / '.env').read_bytes() == (source / '.env').read_bytes()
    if os.name != 'nt':
        assert (scoped / 'auth.json').stat().st_mode & 0o077 == 0
    config.write_text('model: changed', encoding='utf-8')
    assert workspace_environment({'HOME': str(home)}, work)['HERMES_HOME'] == str(scoped)
    assert yaml.safe_load((scoped / 'config.yaml').read_text())['model'] == value['model']


def test_malformed_hermes_config_fails_before_launch(tmp_path):
    source = tmp_path / '.hermes'
    source.mkdir()
    (source / 'config.yaml').write_text('- not a mapping', encoding='utf-8')
    with pytest.raises(ValueError, match='hermes_config_invalid'):
        workspace_environment({'HOME': str(tmp_path)}, tmp_path)
