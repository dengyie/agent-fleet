"""Bounded observable Agent facts. No I/O, permission grants or vendor guesses.

`observed_features` is the repository's monotonic projection of accepted public
events, not a vendor-supplied capability list. Unknown means not evidenced in the
retained projection; it never means the provider is incapable of producing it.
"""
from __future__ import annotations

from collections.abc import Mapping

from agent_profiles import canonical_family

RUNTIME_PROTOCOLS = ('acp', 'cli_json', 'native_transcript', 'desktop_bridge', 'unknown')
OBSERVED_FEATURES = frozenset({'visible_text', 'text_deltas', 'message_identity',
    'tool_calls', 'tool_results', 'interactive_questions', 'native_permissions', 'capture_gaps'})
EVIDENCE_FEATURES = ('visible_text', 'text_deltas', 'message_identity', 'tool_calls',
    'tool_results', 'interactive_questions', 'native_permissions', 'model', 'provider',
    'native_title', 'client_version', 'native_started_at', 'resume_identity', 'usage',
    'hidden_reasoning')
_FAMILIES = frozenset({'codex', 'claude_code', 'pi', 'zcode', 'hermes', 'agy'})
_SOURCES = frozenset({'acp', 'structured_stream', 'native_transcript', 'hooks', 'hook',
                      'pty', 'metadata', 'desktop_bridge'})


def _strings(value, allowed):
    return {item for item in value[:32] if isinstance(item, str) and item in allowed} if isinstance(value, list) else set()


def observed_event_features(kind, payload):
    """Extract fixed feature names from an already validated durable event."""
    if not isinstance(payload, Mapping):
        return set()
    features = set()
    if kind in {'assistant_message', 'user_message'}:
        if isinstance(payload.get('text'), str) and payload['text']:
            features.add('visible_text')
        if payload.get('content_mode') == 'delta':
            features.add('text_deltas')
        if isinstance(payload.get('message_id'), str) and payload['message_id']:
            features.add('message_identity')
    elif kind == 'tool_call':
        features.add('tool_calls')
    elif kind == 'tool_result':
        features.add('tool_results')
    elif kind == 'interaction_request':
        features.add('interactive_questions')
        if payload.get('delivery') == 'native_permission':
            features.add('native_permissions')
    elif kind == 'capture_gap' or (kind == 'capture_quality_changed'
            and payload.get('old_quality') != payload.get('new_quality')):
        features.add('capture_gaps')
    return features


def project_agent_evidence(session: Mapping) -> dict:
    """Describe bounded evidence independently from current control authority."""
    session = session if isinstance(session, Mapping) else {}
    family = canonical_family(session.get('agent_family'))
    family = family if isinstance(family, str) and family in _FAMILIES else 'unknown'
    protocol = session.get('runtime_protocol')
    protocol = protocol if isinstance(protocol, str) and protocol in RUNTIME_PROTOCOLS else 'unknown'
    protocol_source = 'session_metadata' if protocol != 'unknown' else 'none'
    source = session.get('adapter')
    source = source if isinstance(source, str) and source in _SOURCES else 'unknown'
    if source == 'acp':
        source = 'structured_stream'
    # Exact source labels establish a capture protocol, not a provider feature.
    if protocol == 'unknown' and source in {'native_transcript', 'desktop_bridge'}:
        protocol, protocol_source = source, 'capture_source'
    if protocol == 'unknown' and 'desktop_bridge' in _strings(session.get('capabilities'), {'desktop_bridge'}):
        protocol, protocol_source = 'desktop_bridge', 'session_metadata'
    observed = _strings(session.get('observed_features'), OBSERVED_FEATURES)
    features = {name: {'state': 'observed' if name in observed else 'unknown',
                       'source': 'session_event' if name in observed else 'none'}
                for name in EVIDENCE_FEATURES}
    for name, limit in [('model', 160), ('provider', 96), ('native_title', 160),
                        ('client_version', 96), ('native_started_at', 40)]:
        value = session.get(name)
        if (isinstance(value, str) and 0 < len(value.strip()) <= limit
                and not any(ord(char) < 32 or ord(char) == 127 for char in value)):
            features[name] = {'state': 'observed', 'source': 'session_metadata'}
    caps = _strings(session.get('capabilities'), {'native_resume'})
    if 'native_resume' in caps:
        features['resume_identity'] = {'state': 'observed', 'source': 'continuation_identity'}
    # The current public event contract intentionally has no usage or internal
    # reasoning fields. This describes Fleet's surface, not a model limitation.
    for name in ('usage', 'hidden_reasoning'):
        features[name] = {'state': 'unsupported', 'source': 'public_contract'}
    quality = session.get('capture_quality')
    quality = quality if (session.get('managed') is True and isinstance(quality, str)
                          and quality in {'exact', 'structured', 'best_effort'}) else 'best_effort'
    capture = session.get('capture')
    history = 'retained_source_from_checkpoint' if (isinstance(capture, Mapping)
        and capture.get('history') == 'retained_source_from_checkpoint') else 'unknown'
    if 'capture_gaps' in observed:
        history = 'gaps_observed'
    return {'family': family, 'adapter_support': 'supported' if family != 'unknown' else 'unknown',
        'installation': 'unknown',
        'protocol': {'name': protocol, 'state': 'observed' if protocol != 'unknown' else 'unknown',
                     'source': protocol_source},
        'capture': {'source': source, 'quality': quality, 'history': history},
        'features': features}
