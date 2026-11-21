"""Behavior and dependency direction for the Agent capability boundaries."""
import ast
from copy import deepcopy
from pathlib import Path
import subprocess
import sys

import pytest

from agent_profiles import PROFILES, canonical_family
from fleet_core.session_models import AGENT_PROTOCOLS
from hub.domain.session_access import AdoptionAccess, project_session_access
from tools.adapters.native_resume import build_resume_argv, continuation_options

ROOT = Path(__file__).resolve().parents[1]
ACTIONS = {'append_user_turn', 'pause_session', 'configure_model', 'arbitrary_shell'}


def project(family='codex', *, adoption=None, actions=ACTIONS, **changes):
    row = {'session_id':'runner_fixture', 'machine_id':'m1', 'managed':True,
           'agent_family':family, 'status':'closed', 'current_state':'completed',
           'capabilities':['native_resume', 'model_takeover']}
    row.update(changes)
    before = deepcopy(row)
    result = project_session_access(row, adoption, actions, adoption_available=True)
    assert row == before
    return result


@pytest.mark.parametrize('family', list(PROFILES) + ['claude', 'unknown'])
def test_owned_runner_capabilities_are_registry_driven_but_not_inferred(family):
    profile = PROFILES.get(canonical_family(family))
    value = project(family)
    assert ('append_user_turn' in value['allowed_actions']) == bool(profile and profile.native_resume)
    assert ('configure_model' in value['allowed_actions']) == bool(profile and profile.model_protocol)
    assert 'arbitrary_shell' not in value['allowed_actions']
    assert 'append_user_turn' not in project(family, capabilities=[])['allowed_actions']
    assert project(family, actions=set())['allowed_actions'] == []
    assert project(family, managed=False)['allowed_actions'] == []


@pytest.mark.parametrize('state', ['running', 'thinking', 'tool_running'])
def test_runner_busy_state_blocks_delivery(state):
    value = project(current_state=state)
    assert 'append_user_turn' not in value['allowed_actions']
    assert value['message_delivery']['reason'] == 'session_busy'


@pytest.mark.parametrize('family', list(PROFILES))
def test_adoption_requires_proven_source_and_family_admission(family):
    adoption = AdoptionAccess('authoritative-machine', family, 'adopted', '/private/native.jsonl')
    value = project('wrong-captured-family', adoption=adoption, session_id='adopt_fixture')
    assert value['machine_id'] == 'authoritative-machine'
    assert value['agent_family'] == family
    assert ('append_user_turn' in value['allowed_actions']) == (family in {'codex', 'claude_code', 'pi'})
    assert 'configure_model' not in value['allowed_actions']
    assert value['capture']['native_source_bound'] is True
    assert '/private' not in str(value)
    assert 'append_user_turn' not in project(family, adoption=AdoptionAccess(
        'm1', family, 'adopted', None), session_id='adopt_fixture')['allowed_actions']


@pytest.mark.parametrize('state', ['pending', 'revoked', 'terminal'])
def test_nonadmitted_adoption_cannot_be_revived_by_capture_claims(state):
    adoption = AdoptionAccess('m1', 'codex', state, '/native.jsonl')
    value = project(adoption=adoption, session_id='adopt_fixture')
    assert value['allowed_actions'] == []
    assert value['message_delivery']['available'] is False


def test_missing_adoption_and_desktop_bridge_remain_separate():
    assert project(session_id='adopt_fixture')['adoption_status'] == 'missing'
    assert project(session_id='adopt_fixture')['allowed_actions'] == []
    desktop = project('zcode', capabilities=['desktop_bridge'])
    assert desktop['allowed_actions'] == ['append_user_turn']
    assert desktop['message_delivery']['mechanism'] == 'zcode_desktop_bridge'
    assert project('zcode', capabilities=['desktop_bridge'], actions=set())['allowed_actions'] == []


def test_model_protocols_and_alias_have_one_declarative_source():
    assert AGENT_PROTOCOLS == {'codex':'responses', 'claude_code':'anthropic'}
    assert AGENT_PROTOCOLS == {name:p.model_protocol for name,p in PROFILES.items() if p.model_protocol}
    assert canonical_family('claude') == 'claude_code'
    assert canonical_family('future-agent') == 'future-agent'
    assert canonical_family(None) is None


@pytest.mark.parametrize('family', ['codex', 'claude_code', 'pi', 'zcode', 'hermes'])
def test_resume_composition_preserves_node_script_options_and_prompt(family):
    command = [r'C:\Program Files\node.exe', r'C:\Agent CLI\cli.cjs', '--model', 'original', 'FIRST']
    options = continuation_options(family, command)
    before = list(options)
    prompt = 'FOLLOW 🌲 ; $(literal) & "quoted"'
    argv = build_resume_argv(family, command[0], 'exact-native-id', prompt, options)
    assert argv[:2] == command[:2]
    assert argv[-1] == prompt
    assert argv.count('exact-native-id') == 1
    assert 'FIRST' not in argv
    assert options == before
    assert build_resume_argv(family, command[0], '-unsafe-token', prompt, options) == []


def test_layer_modules_keep_io_and_orchestration_out_of_pure_rules():
    allowed = {
        'hub/domain/session_access.py': {'__future__', 'collections.abc', 'dataclasses', 'typing', 'agent_profiles', 'session_schema', 'hub.domain.agent_session'},
        'hub/domain/session_ports.py': {'__future__', 'collections.abc', 'dataclasses', 'typing'},
        'tools/adapters/native_resume.py': {'__future__'},
        'tools/session/normalization.py': {'__future__', 'json', 'typing'},
    }
    for name, permitted in allowed.items():
        tree = ast.parse((ROOT / name).read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert {item.name for item in node.names} <= permitted, name
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0 and node.module in permitted, name
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {'open', 'eval', 'exec', '__import__'}, name


def test_pure_rules_import_and_execute_with_external_layers_blocked():
    script = """
import sys
for name in ('flask', 'sqlite3', 'hub.infrastructure', 'hub.application', 'hub.http'):
    sys.modules[name] = None
from hub.domain.session_access import project_session_access
from tools.adapters.native_resume import build_resume_argv
from tools.session.normalization import normalize_native_row
assert build_resume_argv('pi','pi','native','literal')[-1] == 'literal'
assert normalize_native_row({'type':'message_end','message':{'role':'user','content':'literal'}})[0][0] == 'user_message'
assert project_session_access({'session_id':'s','managed':False},None,[],adoption_available=False)['allowed_actions'] == []
"""
    result = subprocess.run([sys.executable, '-c', script], cwd=ROOT,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_sqlite_adapters_implement_ports_and_preserve_error_result_imports(tmp_path):
    from hub.domain import session_ports as ports
    from hub.infrastructure import adoption_repository, session_repository, transcript_repository
    assert session_repository.SessionError is ports.SessionError
    assert transcript_repository.TranscriptError is ports.TranscriptError
    assert transcript_repository.IngestResult is ports.IngestResult
    assert adoption_repository.AdoptionRepositoryError is ports.AdoptionRepositoryError
    assert isinstance(session_repository.SessionRepository(tmp_path / 'sessions.db'), ports.SessionStore)
    assert isinstance(transcript_repository.TranscriptRepository(tmp_path / 'transcripts', key=b'a' * 32),
                      ports.TranscriptStore)
    source = (ROOT / 'hub/application/session_service.py').read_text(encoding='utf-8')
    assert 'hub.infrastructure' not in source
