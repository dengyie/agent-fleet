"""Tests for the frozen session event wire schema.

See Task 1 brief of the agent-session-supervision plan.  The shared contract
lives in ``session_schema.py`` at the repository root so both the Hub
(``hub/domain/session.py``) and dependency-light ``tools/`` scripts can import
it without Flask.  Validation must never leak a raw transcript, absolute path,
process PID, token/secret, or raw collector output into the public DTO.

Coverage (from the Task 1 brief):
- exactly the fifteen fixed event kinds and the three capture qualities;
- unknown event kind rejection;
- unknown top-level field removal;
- invalid capture quality rejection;
- missing identity fields rejection;
- oversized payload rejection;
- batch event-count and byte-limit enforcement;
- managed/unmanaged rendering and quality-transition validation;
- public DTO omission of path/PID/credential/raw collector fields;
- golden JSON fixtures for user/assistant/tool/capture-gap events plus
  redaction metadata (fixtures are synthetic; no real credentials or
  production paths).
"""

import json
import unittest
from pathlib import Path

from hub.domain.session import (
    normalize_managed,
    partition_batch,
    public_session_dto,
    try_validate_event,
    validate_quality_transition,
    validate_session_spec,
)
from session_schema import (
    CAPTURE_QUALITIES,
    EVENT_KINDS,
    MAX_BATCH_BYTES,
    MAX_BATCH_EVENTS,
    MAX_EVENT_BYTES,
    validate_batch,
    validate_event,
)

FIXTURES = Path(__file__).resolve().parents[1] / 'tests' / 'fixtures' / 'session'


def load_fixture(name):
    with open(FIXTURES / name, encoding='utf-8') as handle:
        return json.load(handle)


class EventKindTests(unittest.TestCase):
    def test_fifteen_event_kinds(self):
        self.assertEqual(len(EVENT_KINDS), 15)

    def test_spec_event_kinds_present(self):
        expected = {
            'session_start', 'session_metadata', 'user_message',
            'assistant_message', 'tool_call', 'tool_result',
            'process_spawn', 'process_exit', 'step_start', 'step_end',
            'session_close', 'capture_quality_changed', 'capture_gap',
            'policy_signal', 'supervisor_state_changed',
        }
        self.assertEqual(EVENT_KINDS, frozenset(expected))

    def test_capture_qualities(self):
        self.assertEqual(CAPTURE_QUALITIES, ('exact', 'structured', 'best_effort'))


class BoundConstantTests(unittest.TestCase):
    def test_bounds(self):
        self.assertEqual(MAX_EVENT_BYTES, 65536)
        self.assertEqual(MAX_BATCH_EVENTS, 100)
        self.assertEqual(MAX_BATCH_BYTES, 262144)


def base_event(**overrides):
    event = {
        'schema_version': 1,
        'event_id': 'evt_0a1b2c3d',
        'stream_id': 'stream_abc',
        'machine_id': 'host-1',
        'session_id': 'sess_xyz',
        'attempt_id': 'att_1',
        'process_group_id': 'grp_1',
        'sequence': 1,
        'kind': 'assistant_message',
        'capture_quality': 'structured',
        'source': 'structured_stream',
        'emitted_at': '2026-08-26T00:00:00Z',
        'payload': {'text': 'hello', 'is_complete': True, 'digest': 'sha256:aa'},
        'redaction': {'state': 'redacted', 'uncertain': False, 'rules': []},
    }
    event.update(overrides)
    return event


class ValidateEventTests(unittest.TestCase):
    def test_valid_event_passes(self):
        result = validate_event(base_event())
        self.assertEqual(result['kind'], 'assistant_message')
        self.assertEqual(result['sequence'], 1)

    def test_unknown_event_kind_rejected(self):
        with self.assertRaises(ValueError):
            validate_event(base_event(kind='unknown_kind'))

    def test_unknown_top_level_field_removed(self):
        result = validate_event(base_event(extra_top='leak'))
        self.assertNotIn('extra_top', result)

    def test_invalid_capture_quality_rejected(self):
        for bad in ('perfect', 'none', ''):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    validate_event(base_event(capture_quality=bad))

    def test_missing_identity_fields_rejected(self):
        for field in ('schema_version', 'event_id', 'stream_id', 'machine_id',
                      'session_id', 'sequence', 'kind', 'capture_quality',
                      'emitted_at'):
            with self.subTest(field=field):
                event = base_event()
                del event[field]
                with self.assertRaises(ValueError):
                    validate_event(event)

    def test_oversized_payload_rejected(self):
        event = base_event(
            payload={'text': 'x' * MAX_EVENT_BYTES, 'is_complete': True}
        )
        with self.assertRaises(ValueError):
            validate_event(event)


class QualityChangeEnvelopeTests(unittest.TestCase):
    def test_capture_quality_changed_requires_valid_payload_qualities(self):
        event = base_event(
            kind='capture_quality_changed',
            payload={'old_quality': 'exact', 'new_quality': 'best_effort'},
        )
        result = validate_event(event)
        self.assertEqual(result['kind'], 'capture_quality_changed')

    def test_capture_quality_changed_rejects_bad_qualities(self):
        event = base_event(
            kind='capture_quality_changed',
            payload={'old_quality': 'exact', 'new_quality': 'nope'},
        )
        with self.assertRaises(ValueError):
            validate_event(event)


class ValidateBatchTests(unittest.TestCase):
    def test_valid_batch_passes(self):
        events = [base_event(sequence=i) for i in range(1, 4)]
        result = validate_batch(events)
        self.assertEqual(len(result), 3)

    def test_too_many_events_rejected(self):
        events = [base_event(sequence=i) for i in range(1, MAX_BATCH_EVENTS + 2)]
        with self.assertRaises(ValueError):
            validate_batch(events)

    def test_too_large_batch_rejected(self):
        # Each event individually stays under the single-event cap, but the
        # validated batch together exceeds the batch byte ceiling.
        # Each payload is ~60000 bytes, so 5 such events add to ~300 KiB,
        # comfortably over MAX_BATCH_BYTES (262144) and under 65536 apiece.
        payload = {'text': 'x' * (MAX_EVENT_BYTES - 5000), 'is_complete': True}
        events = [base_event(sequence=i, payload=payload) for i in range(1, 6)]
        with self.assertRaises(ValueError):
            validate_batch(events)


class PublicDtoTests(unittest.TestCase):
    def test_dto_omits_path_pid_collector_raw(self):
        session = {
            'machine_id': 'host-1',
            'session_id': 'sess_xyz',
            'managed': True,
            'agent_family': 'codex',
            'raw_transcript': '/home/user/.codex/sessions/xyz.jsonl',
            'collector_output': {'raw_lines': ['line']},
            'pid': 1234,
            'process_group_id': 'grp_1',
            'token': 'sekrit',
            'path': '/var/lib/fleet',
            'attempt_id': 'att_1',
            'capture_quality': 'exact',
        }
        dto = public_session_dto(session)
        for leaked in ('raw_transcript', 'collector_output', 'pid',
                       'path', 'token'):
            self.assertNotIn(leaked, dto)

    def test_dto_keeps_public_identity_fields(self):
        session = {
            'machine_id': 'host-1',
            'session_id': 'sess_xyz',
            'managed': True,
            'capture_quality': 'exact',
            'attempt_id': 'att_1',
            'process_group_id': 'grp_1',
        }
        dto = public_session_dto(session)
        for field in ('machine_id', 'session_id', 'managed',
                      'capture_quality', 'attempt_id', 'process_group_id'):
            self.assertIn(field, dto)
        self.assertTrue(dto['managed'])

    def test_unmanaged_session_shows_best_effort(self):
        session = {'machine_id': 'host-1', 'session_id': 'sess_xyz',
                   'managed': False}
        dto = public_session_dto(session)
        self.assertFalse(dto['managed'])
        self.assertEqual(dto['capture_quality'], 'best_effort')
        self.assertEqual(dto['control_capability'], 'unavailable')

    def test_dto_ignores_non_mapping(self):
        self.assertEqual(public_session_dto(None), {})
        self.assertEqual(public_session_dto('junk'), {})


class QualityTransitionTests(unittest.TestCase):
    def test_valid_equal_and_down_grades(self):
        validate_quality_transition('exact', 'exact')
        validate_quality_transition('exact', 'structured')
        validate_quality_transition('exact', 'best_effort')
        validate_quality_transition('structured', 'best_effort')
        validate_quality_transition('best_effort', 'best_effort')

    def test_invalid_quality_rejected(self):
        for old, new in (('exact', 'perfect'), ('fancy', 'best_effort')):
            with self.subTest(old=old, new=new):
                with self.assertRaises(ValueError):
                    validate_quality_transition(old, new)

    def test_best_effort_source_cannot_upgrade(self):
        with self.assertRaises(ValueError):
            validate_quality_transition('best_effort', 'exact')
        with self.assertRaises(ValueError):
            validate_quality_transition('best_effort', 'structured')


class ManagedPolicyTests(unittest.TestCase):
    def test_standalone_session_has_session_id_and_process_group(self):
        # A standalone (task-less) session must still bind session + group.
        session = {'machine_id': 'host-1', 'session_id': 'sess_xyz',
                   'process_group_id': 'grp_1'}
        dto = public_session_dto(session)
        self.assertEqual(dto['session_id'], 'sess_xyz')
        self.assertEqual(dto['process_group_id'], 'grp_1')


class GoldenFixtureTests(unittest.TestCase):
    def test_required_fixture_files_exist(self):
        for name in ('session_start.json', 'user_message.json',
                     'assistant_message.json', 'tool_call.json',
                     'tool_result.json', 'capture_quality_changed.json',
                     'capture_gap.json'):
            self.assertTrue((FIXTURES / name).is_file(), name)

    def test_fixtures_validate(self):
        for path in sorted(FIXTURES.glob('*.json')):
            with self.subTest(name=path.name):
                validate_event(load_fixture(path.name))

    def test_fixtures_contain_redaction_metadata(self):
        for name in ('user_message.json', 'assistant_message.json',
                     'tool_call.json', 'tool_result.json'):
            event = load_fixture(name)
            self.assertIn('redaction', event)
            self.assertIn('state', event['redaction'])
            self.assertIn('rules', event['redaction'])

    def test_fixtures_have_no_real_credentials_or_paths(self):
        for path in FIXTURES.glob('*.json'):
            source = path.read_text().lower()
            for token in ('sk-ant-', 'api_key=', 'password=', 'begin private key',
                          '/home/', '/users/', '/var/lib'):
                self.assertNotIn(token, source, f'{path.name} leaked {token}')

    def test_fixture_payloads_carry_no_pid_or_path(self):
        for path in FIXTURES.glob('*.json'):
            payload = json.loads(path.read_text()).get('payload', {})
            self.assertNotIn('pid', payload, path.name)
            self.assertNotIn('path', payload, path.name)
            self.assertNotIn('proc_path', payload, path.name)


class GenericPayloadAllowlistTests(unittest.TestCase):
    """Finding 1: generic event kinds must drop non-allowlisted payload keys."""

    def test_process_spawn_drops_pid_command_env_cwd(self):
        event = base_event(
            kind='process_spawn',
            payload={
                'process_family': 'codex_cli',
                'owner': 'operator',
                'process_group_id': 'grp_1',
                'pid': 9999,
                'command': '/usr/bin/codex',
                'argv': ['/usr/bin/codex', 'exec'],
                'env': {'HOME': '/root'},
                'cwd': '/var/lib/fleet',
                'path': '/usr/bin/codex',
            },
        )
        clean = validate_event(event)
        self.assertEqual(clean['payload']['process_family'], 'codex_cli')
        self.assertNotIn('pid', clean['payload'])
        self.assertNotIn('command', clean['payload'])
        self.assertNotIn('argv', clean['payload'])
        self.assertNotIn('env', clean['payload'])
        self.assertNotIn('cwd', clean['payload'])
        self.assertNotIn('path', clean['payload'])

    def test_session_start_drops_raw_collector_output(self):
        event = base_event(
            kind='session_start',
            payload={
                'agent_family': 'codex',
                'adapter': 'structured',
                'raw': '{"lines": 1}',
                'raw_output': 'tool invocation transcript',
                'collector_output': ['line1', 'line2'],
            },
        )
        clean = validate_event(event)
        self.assertEqual(clean['payload']['agent_family'], 'codex')
        self.assertNotIn('raw', clean['payload'])
        self.assertNotIn('raw_output', clean['payload'])
        self.assertNotIn('collector_output', clean['payload'])

    def test_session_metadata_drops_credential_keys(self):
        event = base_event(
            kind='session_metadata',
            payload={
                'agent_family': 'claude_code',
                'task_id': 't-1',
                'token': 'sk-secret',
                'password': 'pw',
                'api_key': 'k',
            },
        )
        clean = validate_event(event)
        self.assertEqual(clean['payload']['task_id'], 't-1')
        self.assertNotIn('token', clean['payload'])
        self.assertNotIn('password', clean['payload'])
        self.assertNotIn('api_key', clean['payload'])

    def test_policy_signal_allows_only_document_fields(self):
        event = base_event(
            kind='policy_signal',
            payload={
                'severity': 'high',
                'policy': 'block_unlisted_tool',
                'reason': 'tool not allowlisted',
                'pid': 12,
                'env': {'K': 'V'},
            },
        )
        clean = validate_event(event)
        self.assertEqual(clean['payload']['severity'], 'high')
        self.assertNotIn('pid', clean['payload'])
        self.assertNotIn('env', clean['payload'])

    def test_supervisor_state_changed_allowlist(self):
        event = base_event(
            kind='supervisor_state_changed',
            payload={'old_state': 'starting', 'new_state': 'running',
                     'credential': 'x'},
        )
        clean = validate_event(event)
        self.assertEqual(clean['payload']['new_state'], 'running')
        self.assertNotIn('credential', clean['payload'])


class CaptureGapDegradationTests(unittest.TestCase):
    """Finding 2: capture_gap must represent quality degradation."""

    def test_gap_degrading_passes(self):
        event = base_event(
            kind='capture_gap', capture_quality='structured',
            payload={'start_sequence': 2, 'end_sequence': 3,
                     'reason': 'wifi flap', 'quality': 'best_effort'},
        )
        clean = validate_event(event)
        self.assertEqual(clean['payload']['quality'], 'best_effort')

    def test_gap_same_quality_rejected(self):
        event = base_event(
            kind='capture_gap', capture_quality='structured',
            payload={'start_sequence': 2, 'end_sequence': 3, 'quality': 'structured'},
        )
        with self.assertRaises(ValueError):
            validate_event(event)

    def test_gap_elevated_quality_rejected(self):
        event = base_event(
            kind='capture_gap', capture_quality='structured',
            payload={'start_sequence': 2, 'end_sequence': 3, 'quality': 'exact'},
        )
        with self.assertRaises(ValueError):
            validate_event(event)

    def test_gap_missing_fields_rejected(self):
        for missing in ('start_sequence', 'end_sequence', 'quality'):
            with self.subTest(missing=missing):
                payload = {'start_sequence': 1, 'end_sequence': 2, 'quality': 'best_effort'}
                del payload[missing]
                event = base_event(kind='capture_gap', capture_quality='structured',
                                   payload=payload)
                with self.assertRaises(ValueError):
                    validate_event(event)


class Rfc3339EmittedAtTests(unittest.TestCase):
    """Finding 6: emitted_at must be RFC3339."""

    def test_valid_rfc3339_pass(self):
        for stamp in ('2026-08-26T00:00:00Z',
                      '2026-08-26T00:00:00+08:00',
                      '2026-08-26T00:00:00.123Z'):
            with self.subTest(stamp=stamp):
                validate_event(base_event(emitted_at=stamp))

    def test_invalid_emitted_at_rejected(self):
        for stamp in ('2026-08-26 00:00:00', '2026-08-26T00:00:00',
                      'yesterday', '', '2026-13-99T00:00:00Z',
                      '2026-08-26T00:00:00+0800'):
            with self.subTest(stamp=stamp):
                with self.assertRaises(ValueError):
                    validate_event(base_event(emitted_at=stamp))


class PublicDtoSanitizationTests(unittest.TestCase):
    """Findings 3+4: DTO clamps quality and sanitizes path/secret-shaped IDs."""

    def test_invalid_quality_clamped_to_best_effort(self):
        dto = public_session_dto({'machine_id': 'h1', 'session_id': 's1',
                                  'managed': True,
                                  'capture_quality': 'not_a_quality'})
        self.assertEqual(dto['capture_quality'], 'best_effort')

    def test_managed_bad_quality_clamped(self):
        dto = public_session_dto({'machine_id': 'h1', 'session_id': 's1',
                                  'managed': True,
                                  'capture_quality': 'illegal'})
        self.assertEqual(dto['capture_quality'], 'best_effort')

    def test_path_shaped_ids_redacted(self):
        dto = public_session_dto({
            'machine_id': '/etc/passwd',
            'session_id': '/home/user/codex',
            'process_group_id': 'grp_1',
            'attempt_id': 'att_1',
            'managed': True,
        })
        self.assertEqual(dto['machine_id'], '')
        self.assertEqual(dto['session_id'], '')
        self.assertEqual(dto['process_group_id'], 'grp_1')
        self.assertEqual(dto['attempt_id'], 'att_1')

    def test_token_shaped_ids_redacted(self):
        dto = public_session_dto({
            'machine_id': 'h1',
            'session_id': 'sess_token_abc',
            'managed': True,
        })
        self.assertEqual(dto['session_id'], '')
        self.assertEqual(dto['machine_id'], 'h1')

    def test_opaque_ids_preserved(self):
        dto = public_session_dto({
            'machine_id': 'host-1',
            'session_id': 'sess_xyz',
            'attempt_id': 'att_1',
            'process_group_id': 'grp_1',
            'managed': True,
        })
        self.assertEqual(dto['machine_id'], 'host-1')
        self.assertEqual(dto['session_id'], 'sess_xyz')
        self.assertEqual(dto['attempt_id'], 'att_1')
        self.assertEqual(dto['process_group_id'], 'grp_1')


class DtoTimestampTests(unittest.TestCase):
    """Finding 1: started_at/updated_at only emit bounded RFC3339 values."""

    def test_valid_timestamps_pass_through(self):
        dto = public_session_dto({
            'machine_id': 'host-1',
            'session_id': 'sess_xyz',
            'managed': True,
            'started_at': '2026-08-26T01:02:03Z',
            'updated_at': '2026-08-26T04:05:06.500+08:00',
        })
        self.assertEqual(dto['started_at'], '2026-08-26T01:02:03Z')
        self.assertEqual(dto['updated_at'], '2026-08-26T04:05:06.500+08:00')

    def test_malformed_timestamps_omitted(self):
        dto = public_session_dto({
            'machine_id': 'host-1',
            'session_id': 'sess_xyz',
            'managed': True,
            'started_at': '/etc/passwd',
            'updated_at': '2026-08-26 00:00:00',
        })
        self.assertNotIn('started_at', dto)
        self.assertNotIn('updated_at', dto)

    def test_non_string_timestamps_omitted(self):
        dto = self._dto_with_timestamps(1724641200)
        self.assertNotIn('started_at', dto)
        self.assertNotIn('updated_at', dto)

    def test_secret_shaped_timestamp_omitted(self):
        dto = self._dto_with_timestamps('sk-ant-token-value')
        self.assertNotIn('started_at', dto)
        self.assertNotIn('updated_at', dto)

    def _dto_with_timestamps(self, stamp):
        return public_session_dto({
            'machine_id': 'host-1',
            'session_id': 'sess_xyz',
            'managed': True,
            'started_at': stamp,
            'updated_at': stamp,
        })


class UnmanagedQualityAlwaysBestEffortTests(unittest.TestCase):
    """Finding 2 (unmanaged): stored exact/structured must render best_effort."""

    def test_unmanaged_with_exact_stored_renders_best_effort(self):
        dto = public_session_dto({
            'machine_id': 'host-1',
            'session_id': 'sess_xyz',
            'managed': False,
            'capture_quality': 'exact',
        })
        self.assertEqual(dto['capture_quality'], 'best_effort')
        self.assertEqual(dto['control_capability'], 'unavailable')

    def test_unmanaged_with_structured_stored_renders_best_effort(self):
        dto = public_session_dto({
            'machine_id': 'host-1',
            'session_id': 'sess_xyz',
            'managed': False,
            'capture_quality': 'structured',
        })
        self.assertEqual(dto['capture_quality'], 'best_effort')

    def test_unmanaged_missing_quality_renders_best_effort(self):
        dto = public_session_dto({
            'machine_id': 'host-1',
            'session_id': 'sess_xyz',
            'managed': False,
        })
        self.assertEqual(dto['capture_quality'], 'best_effort')


class KeyIdRejectionTests(unittest.TestCase):
    """Finding 3: 'key' is restored as a forbidden opaque-ID marker."""

    def test_key_shaped_event_id_rejected(self):
        with self.assertRaises(ValueError):
            validate_event(base_event(event_id='evt_api_key_abc'))

    def test_key_shaped_session_id_rejected(self):
        with self.assertRaises(ValueError):
            validate_event(base_event(session_id='sess_mykey'))

    def test_key_shaped_stream_id_rejected(self):
        with self.assertRaises(ValueError):
            validate_event(base_event(stream_id='stream_key_store'))

    def test_key_shaped_attempt_id_rejected(self):
        with self.assertRaises(ValueError):
            validate_event(base_event(attempt_id='att_apikey_1'))

    def test_key_shaped_dto_session_id_redacted(self):
        dto = public_session_dto({
            'machine_id': 'host-1',
            'session_id': 'sess_apikey_xyz',
            'managed': True,
        })
        self.assertEqual(dto['session_id'], '')
        self.assertEqual(dto['machine_id'], 'host-1')

    def test_key_shaped_dto_attempt_id_redacted(self):
        dto = public_session_dto({
            'machine_id': 'host-1',
            'session_id': 'sess_xyz',
            'attempt_id': 'att_keyring_1',
            'managed': True,
        })
        self.assertNotIn('attempt_id', dto)

    def test_plain_ids_not_hit_by_substring(self):
        # 'key' is a substring marker; a legit opaque prefix like 'monkey'
        # is still redacted, but a clearly separate opaque id stays intact.
        dto = public_session_dto({
            'machine_id': 'host-1',
            'session_id': 'sess_xyz',
            'managed': True,
        })
        self.assertEqual(dto['session_id'], 'sess_xyz')


class DtoSurfaceShapeTests(unittest.TestCase):
    """Finding 5: agent_family/status/capabilities never leak values."""

    def test_path_shaped_agent_family_omitted(self):
        dto = public_session_dto({
            'machine_id': 'host-1',
            'session_id': 'sess_xyz',
            'managed': True,
            'agent_family': '/usr/bin/codex',
        })
        self.assertNotIn('agent_family', dto)

    def test_secret_shaped_status_omitted(self):
        for status in ('api_key=abc', 'token secret', 'BEGIN PRIVATE KEY'):
            with self.subTest(status=status):
                dto = public_session_dto({
                    'machine_id': 'host-1',
                    'session_id': 'sess_xyz',
                    'managed': True,
                    'status': status,
                })
                self.assertNotIn('status', dto)

    def test_credential_shaped_agent_family_omitted(self):
        dto = public_session_dto({
            'machine_id': 'host-1',
            'session_id': 'sess_xyz',
            'managed': True,
            'agent_family': 'codex_private_fork',
        })
        self.assertNotIn('agent_family', dto)

    def test_path_shaped_capabilities_filtered(self):
        dto = public_session_dto({
            'machine_id': 'host-1',
            'session_id': 'sess_xyz',
            'managed': True,
            'capabilities': ['/var/lib/fleet', 'git_sync', '/home/u/a.jsonl'],
        })
        self.assertEqual(dto['capabilities'], ['git_sync'])

    def test_secret_shaped_capabilities_filtered(self):
        dto = public_session_dto({
            'machine_id': 'host-1',
            'session_id': 'sess_xyz',
            'managed': True,
            'capabilities': ['token=abc', 'execute', 'password_hint'],
        })
        self.assertEqual(dto['capabilities'], ['execute'])

    def test_capabilities_omitted_when_all_unsafe(self):
        dto = public_session_dto({
            'machine_id': 'host-1',
            'session_id': 'sess_xyz',
            'managed': True,
            'capabilities': ['/bin/sh', 'api_key_1'],
        })
        self.assertNotIn('capabilities', dto)

    def test_surface_strings_bounded(self):
        dto = public_session_dto({
            'machine_id': 'host-1',
            'session_id': 'sess_xyz',
            'managed': True,
            'agent_family': 'x' * 200,
            'status': 'y' * 100,
        })
        self.assertEqual(len(dto['agent_family']), 64)
        self.assertEqual(len(dto['status']), 64)


class ManagedStringNormalizationTests(unittest.TestCase):
    """Round 3 finding 1: managed must normalize strictly."""

    def test_managed_true_is_managed(self):
        dto = public_session_dto({
            'machine_id': 'host-1', 'session_id': 'sess_xyz',
            'managed': True, 'capture_quality': 'exact',
        })
        self.assertTrue(dto['managed'])
        self.assertEqual(dto['capture_quality'], 'exact')
        self.assertEqual(dto['control_capability'], 'available')

    def test_managed_numeric_one_is_managed(self):
        dto = public_session_dto({
            'machine_id': 'host-1', 'session_id': 'sess_xyz',
            'managed': 1, 'capture_quality': 'structured',
        })
        self.assertTrue(dto['managed'])
        self.assertEqual(dto['capture_quality'], 'structured')
        self.assertEqual(dto['control_capability'], 'available')

    def test_managed_string_true_is_unmanaged(self):
        dto = public_session_dto({
            'machine_id': 'host-1', 'session_id': 'sess_xyz',
            'managed': 'true', 'capture_quality': 'exact',
        })
        self.assertFalse(dto['managed'])
        self.assertEqual(dto['capture_quality'], 'best_effort')
        self.assertEqual(dto['control_capability'], 'unavailable')

    def test_managed_false_strings_are_unmanaged(self):
        for value in ('false', 'False', '0', 'no'):
            with self.subTest(managed=value):
                dto = public_session_dto({
                    'machine_id': 'host-1', 'session_id': 'sess_xyz',
                    'managed': value, 'capture_quality': 'exact',
                })
                self.assertFalse(dto['managed'])
                self.assertEqual(dto['capture_quality'], 'best_effort')
                self.assertEqual(dto['control_capability'], 'unavailable')

    def test_managed_numeric_zero_is_unmanaged(self):
        dto = public_session_dto({
            'machine_id': 'host-1', 'session_id': 'sess_xyz',
            'managed': 0, 'capture_quality': 'structured',
        })
        self.assertFalse(dto['managed'])
        self.assertEqual(dto['capture_quality'], 'best_effort')

    def test_managed_none_is_unmanaged(self):
        dto = public_session_dto({
            'machine_id': 'host-1', 'session_id': 'sess_xyz',
            'managed': None, 'capture_quality': 'structured',
        })
        self.assertFalse(dto['managed'])
        self.assertEqual(dto['capture_quality'], 'best_effort')

    def test_managed_absent_is_unmanaged(self):
        dto = public_session_dto({
            'machine_id': 'host-1', 'session_id': 'sess_xyz',
        })
        self.assertFalse(dto['managed'])
        self.assertEqual(dto['capture_quality'], 'best_effort')

    def test_normalize_managed_helper(self):
        self.assertTrue(normalize_managed(True))
        self.assertTrue(normalize_managed(1))
        self.assertFalse(normalize_managed(False))
        self.assertFalse(normalize_managed(0))
        self.assertFalse(normalize_managed('true'))
        self.assertFalse(normalize_managed('false'))
        self.assertFalse(normalize_managed('0'))
        self.assertFalse(normalize_managed('no'))
        self.assertFalse(normalize_managed(None))

    def test_spec_accepts_string_false_as_unmanaged(self):
        clean = validate_session_spec({
            'machine_id': 'host-1',
            'session_id': 'sess_xyz',
            'process_group_id': 'grp_1',
            'managed': 'false',
            'capture_quality': 'best_effort',
        })
        self.assertFalse(clean['managed'])
        self.assertEqual(clean['capture_quality'], 'best_effort')

    def test_spec_accepts_numeric_zero_as_unmanaged(self):
        clean = validate_session_spec({
            'machine_id': 'host-1',
            'session_id': 'sess_xyz',
            'process_group_id': 'grp_1',
            'managed': 0,
            'capture_quality': 'best_effort',
        })
        self.assertFalse(clean['managed'])
        self.assertEqual(clean['capture_quality'], 'best_effort')

    def test_spec_rejects_unmanaged_exact_claim(self):
        with self.assertRaises(ValueError):
            validate_session_spec({
                'machine_id': 'host-1',
                'session_id': 'sess_xyz',
                'process_group_id': 'grp_1',
                'managed': 'false',
                'capture_quality': 'exact',
            })

    def test_spec_numeric_one_is_managed(self):
        clean = validate_session_spec({
            'machine_id': 'host-1',
            'session_id': 'sess_xyz',
            'process_group_id': 'grp_1',
            'managed': 1,
            'capture_quality': 'structured',
        })
        self.assertTrue(clean['managed'])
        self.assertEqual(clean['capture_quality'], 'structured')


class PayloadPrimitiveRejectionTests(unittest.TestCase):
    """Round 3 finding 2: nested containers must not be stringified."""

    def test_assistant_text_dict_rejected(self):
        ok, result = try_validate_event(
            base_event(payload={'text': {'secret': 'token-abc'}}))
        self.assertFalse(ok)
        self.assertEqual(result, 'invalid_payload_type')

    def test_assistant_text_list_rejected(self):
        ok, result = try_validate_event(
            base_event(payload={'text': ['tok', 'abc']}))
        self.assertFalse(ok)
        self.assertEqual(result, 'invalid_payload_type')

    def test_user_message_text_dict_rejected(self):
        ok, result = try_validate_event(
            base_event(kind='user_message',
                       payload={'text': {'path': '/etc/passwd'}}))
        self.assertFalse(ok)
        self.assertEqual(result, 'invalid_payload_type')

    def test_tool_call_arguments_dict_rejected(self):
        ok, result = try_validate_event(
            base_event(kind='tool_call',
                       payload={'tool_name': 't', 'call_id': 'c',
                                'status': 'started',
                                'arguments': {'token': 'leak'}}))
        self.assertFalse(ok)
        self.assertEqual(result, 'invalid_payload_type')

    def test_tool_call_result_dict_rejected(self):
        ok, result = try_validate_event(
            base_event(kind='tool_call',
                       payload={'tool_name': 't', 'call_id': 'c',
                                'status': 'started',
                                'result': {'raw': ['x']}}))
        self.assertFalse(ok)
        self.assertEqual(result, 'invalid_payload_type')

    def test_tool_result_nested_arguments_rejected(self):
        ok, result = try_validate_event(
            base_event(kind='tool_result',
                       payload={'tool_name': 't', 'call_id': 'c',
                                'status': 'ok',
                                'arguments': [('k', 'v')]}))
        self.assertFalse(ok)
        self.assertEqual(result, 'invalid_payload_type')

    def test_generic_reason_dict_rejected(self):
        ok, result = try_validate_event(
            base_event(kind='policy_signal',
                       payload={'severity': 'high',
                                'reason': {'detail': 'token-xyz'}}))
        self.assertFalse(ok)
        self.assertEqual(result, 'invalid_payload_type')

    def test_generic_agent_family_list_rejected(self):
        ok, result = try_validate_event(
            base_event(kind='session_start',
                       payload={'agent_family': ['codex', 'gpt']}))
        self.assertFalse(ok)
        self.assertEqual(result, 'invalid_payload_type')

    def test_quality_change_nested_rejected(self):
        ok, result = try_validate_event(
            base_event(kind='capture_quality_changed',
                       payload={'old_quality': ['best_effort'],
                                'new_quality': 'structured'}))
        self.assertFalse(ok)
        self.assertEqual(result, 'invalid_payload_type')

    def test_capture_gap_nested_quality_rejected(self):
        ok, result = try_validate_event(
            base_event(kind='capture_gap',
                       capture_quality='exact',
                       payload={'start_sequence': 1, 'end_sequence': 5,
                                'quality': {'degraded': True}}))
        self.assertFalse(ok)
        self.assertEqual(result, 'invalid_payload_type')

    def test_capture_gap_reason_nested_rejected(self):
        ok, result = try_validate_event(
            base_event(kind='capture_gap',
                       capture_quality='exact',
                       payload={'start_sequence': 1, 'end_sequence': 5,
                                'quality': 'best_effort',
                                'reason': ['degraded', 'path']}))
        self.assertFalse(ok)
        self.assertEqual(result, 'invalid_payload_type')

    def test_redaction_rules_nested_blanked_and_no_leak(self):
        # Nested structures inside redaction.rules are blanked, not leaked.
        redaction = {'state': 'redacted', 'uncertain': True,
                     'rules': [{'secret': 'tok-abc'}, 'credential_pattern']}
        clean = validate_event(base_event(redaction=redaction))
        self.assertEqual(clean['redaction']['rules'], ['', 'credential_pattern'])
        self.assertNotIn('tok-abc', str(clean['redaction']))

    def test_primitive_text_values_still_pass(self):
        clean = validate_event(base_event(
            payload={'text': 'plain text', 'is_complete': True}))
        self.assertEqual(clean['payload']['text'], 'plain text')

    def test_primitive_tool_values_still_pass(self):
        payload = {'tool_name': 'ls', 'call_id': 'c1', 'status': 'ok',
                   'arguments': '--flag', 'result': 'ok'}
        clean = validate_event(base_event(kind='tool_call', payload=payload))
        self.assertEqual(clean['payload']['arguments'], '--flag')
        self.assertEqual(clean['payload']['result'], 'ok')


class PreserveValidBatchTests(unittest.TestCase):
    """Finding 5: malformed events must not drop valid neighbors."""

    def test_partition_batch_keeps_valid_events(self):
        good1 = base_event(sequence=1)
        bad = base_event(sequence=2, kind='not_a_kind')
        good2 = base_event(sequence=3)
        valid, rejected = partition_batch([good1, bad, good2])
        self.assertEqual(len(valid), 2)
        self.assertEqual([e['sequence'] for e in valid], [1, 3])
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]['index'], 1)
        self.assertEqual(rejected[0]['code'], 'invalid_event_kind')

    def test_partition_batch_preserves_order(self):
        events = [base_event(sequence=i) for i in (1, 2, 3)]
        valid, rejected = partition_batch(events)
        self.assertEqual([e['sequence'] for e in valid], [1, 2, 3])
        self.assertEqual(rejected, [])

    def test_try_validate_event_returns_bound_result(self):
        ok, result = try_validate_event(base_event(sequence=5))
        self.assertTrue(ok)
        self.assertEqual(result['sequence'], 5)
        bad, code = try_validate_event(base_event(kind='bogus'))
        self.assertFalse(bad)
        self.assertEqual(code, 'invalid_event_kind')
        self.assertLessEqual(len(code), 200)

    def test_partition_non_list_rejected(self):
        with self.assertRaises(ValueError):
            partition_batch('not a list')

    def test_rejected_error_contains_no_secret(self):
        # Finding 4: a secret inside a malformed event must not appear in the
        # rejected result, and the code must be a stable bounded token, not
        # str(exc) or a raw value.
        secret = 'super-ultra-secret-abc'
        bad = base_event(kind='assistant_message',
                         payload={'text': ('x' * MAX_EVENT_BYTES),
                                  'is_complete': True,
                                  'secret': secret})
        valid, rejected = partition_batch([base_event(sequence=1), bad])
        self.assertEqual(len(valid), 1)
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]['code'], 'event_too_large')
        self.assertNotIn(secret, str(rejected))


if __name__ == '__main__':
    unittest.main()