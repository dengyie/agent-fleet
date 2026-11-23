"""Pure session authority projection shared by HTTP and assistant callers.

Inputs are repository snapshots and currently enabled actions. This module does
not query storage, authorize an operator, issue commands or inspect a process.
Family support never replaces native identity/capability evidence.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from agent_profiles import PROFILES, canonical_family
from session_schema import public_session_dto
from hub.domain.agent_session import SESSION_CONTROL_ACTIONS
from fleet_core.agent_evidence import project_agent_evidence

_SESSION_ACTIONS = frozenset((*SESSION_CONTROL_ACTIONS, 'append_user_turn', 'configure_model', 'answer_native_request'))


@dataclass(frozen=True)
class AdoptionAccess:
    machine_id: str
    agent_family: str
    status: str
    native_file_path: str | None


def project_session_access(row: Mapping[str, Any], adoption: AdoptionAccess | None,
                           actions: Iterable[str], *, adoption_available: bool) -> dict[str, Any]:
    """Capture provenance and current control authority are separate facts."""
    if adoption is not None:
        # Control identity/family belongs to the adoption, not the capture.
        row = {**row, 'machine_id': adoption.machine_id, 'agent_family': adoption.agent_family}
    dto = public_session_dto(row)
    allowed = set(actions) & _SESSION_ACTIONS
    if str(row['session_id']).startswith('adopt_'):
        dto['adoption_status'] = (adoption.status if adoption is not None else
                                  'missing' if adoption_available else 'unavailable')
        if dto['adoption_status'] != 'adopted':
            allowed.clear()
    elif not dto['managed']:
        allowed.clear()
    family = canonical_family(dto.get('agent_family'))
    profile = PROFILES.get(family)
    owned_runner = (adoption is None and str(row['session_id']).startswith('runner_')
                    and dto.get('managed'))
    delivery_reason = ''
    # ZCode/Hermes continuation is proven only for Runner-owned CLI
    # identities. Captured metadata and adoptions do not establish one.
    if (not profile or not profile.native_resume
            or (profile.runner_resume_only and not owned_runner)
            or (family == 'agy' and row.get('runtime_protocol') != 'acp')):
        delivery_reason = 'native_resume_unsupported'
    elif adoption is not None and not adoption.native_file_path:
        delivery_reason = 'native_session_unbound'
    elif owned_runner:
        if 'native_resume' not in row.get('capabilities', []):
            delivery_reason = 'native_session_unbound'
        elif row.get('current_state') in {'running', 'thinking', 'tool_running'}:
            delivery_reason = 'session_busy'
    if delivery_reason:
        allowed.discard('append_user_turn')
    if (not owned_runner or family != 'agy' or row.get('runtime_protocol') != 'acp'
            or 'interactive_questions' not in row.get('capabilities', [])
            or row.get('status') in {'closed', 'failed'}
            or row.get('current_state') in {'completed', 'failed', 'stopped'}):
        allowed.discard('answer_native_request')
    if (not profile or profile.model_protocol is None or adoption is not None
            or not str(row['session_id']).startswith('runner_')
            or 'model_takeover' not in row.get('capabilities', [])
            or row.get('status') not in {'closed', 'failed'}):
        allowed.discard('configure_model')
    if family == 'zcode' and adoption is not None and not adoption.native_file_path:
        # Legacy discovery could adopt a whole desktop host, which owns
        # multiple conversations. Do not signal that shared process as if
        # it were the selected conversation. Preserve its audit history.
        allowed.clear()
        dto['control_unavailable_reason'] = 'native_session_unbound'
    desktop = (family == 'zcode' and adoption is None
               and str(row['session_id']).startswith('runner_')
               and 'desktop_bridge' in row.get('capabilities', []) and dto.get('managed'))
    if desktop:
        allowed = {'append_user_turn'} & set(actions)
        delivery_reason = ''
    dto['message_delivery'] = {
        'available': 'append_user_turn' in allowed,
        'reason': delivery_reason or ('node_receipt_required' if 'append_user_turn' in allowed else 'control_unavailable'),
        'mechanism': 'zcode_desktop_bridge' if desktop else ('native_session_resume' if not delivery_reason else 'unavailable'),
    }
    if adoption is not None:
        dto['capture'] = {'native_source_bound': bool(adoption.native_file_path),
                          'history': 'retained_source_from_checkpoint' if adoption.native_file_path else 'unavailable',
                          'provider_profile_changes_capture': False}
    dto['allowed_actions'] = sorted(allowed)
    dto['control_capability'] = 'available' if allowed else 'unavailable'
    dto['agent_evidence'] = project_agent_evidence({**row, **dto})
    return dto
